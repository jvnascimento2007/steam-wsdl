from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional
from .models import DownloadJob
from .runtime import Context
from .config import DownloadConfig
from .ports import Observer
from threading import Event
from .constants import API_RESULT_MESSAGES, STEAMCMD_ITEM_OVERHEAD, STEAMCMD_START_STAGGER
from .errors import Cancelled, DownloadError, NeedSteamcmd
from .storage import direct_target, steamcmd_target
from .formatting import truncate
from .backends.http import download_direct
from .backends.steamcmd import steamcmd_session

def balance_items(items: list[DownloadJob], n: int) -> list[list[DownloadJob]]:
    """Divide os itens em até `n` grupos de carga parecida (maiores primeiro, sempre no grupo
    mais leve). Assim cada sessão faz um único login e todas terminam quase juntas."""
    buckets: list[list[int]] = [[] for _ in range(n)]
    loads = [0] * n
    weight = [max(it.item.size, 0) + STEAMCMD_ITEM_OVERHEAD for it in items]
    for i in sorted(range(len(items)), key=lambda k: -weight[k]):
        k = loads.index(min(loads))
        buckets[k].append(i)
        loads[k] += weight[i]
    return [[items[i] for i in sorted(b)] for b in buckets if b]  # preserves the original order


def _run_item(item: DownloadJob, ctx: Context, task: int) -> None:
    """Processa um item. Downloads diretos terminam aqui; os que precisam do steamcmd
    raise NeedSteamcmd and are grouped into sessions afterward (one login for multiple items)."""
    if item.result.status == "failed":  # already failed during resolution (e.g. unreadable collection)
        raise DownloadError(item.result.message)
    if item.item.api_result != 1:
        raise DownloadError(API_RESULT_MESSAGES.get(item.item.api_result, f"a Steam retornou o código {item.item.api_result}"))
    if item.item.file_type == 2:
        raise DownloadError("coleções aninhadas não são suportadas")

    if item.item.file_url and not ctx.config.force_steamcmd:
        final = direct_target(item.item, ctx.out_dir)
        if final.exists() and not ctx.config.overwrite:
            item.result.status, item.result.method, item.result.path, item.result.message = "skipped", "direto", final, "já existe"
            return
        try:
            item.result.bytes = download_direct(item, final, ctx.config, ctx.stop, ctx.progress, task)
            item.result.status, item.result.method, item.result.path = "ok", "direto", final
            return
        except DownloadError as exc:
            if not ctx.steamcmd:
                raise
            item.result.direct_error = str(exc)

    if not ctx.steamcmd:
        raise DownloadError(
            f"sem URL de download direto (App ID {item.item.app_id or '?'}); "
            "instale o steamcmd (ou use --steamcmd CAMINHO) para baixar este item"
        )
    if not item.item.app_id:
        raise DownloadError("App ID do jogo desconhecido; não é possível usar o steamcmd")

    final_dir = steamcmd_target(item.item, ctx.out_dir)
    if final_dir.exists() and not ctx.config.overwrite:
        item.result.status, item.result.method, item.result.path, item.result.message = "skipped", "steamcmd", final_dir, "já existe"
        return
    raise NeedSteamcmd()


def complete_item(item: DownloadJob, observer: Observer) -> None:
    """Finaliza um item: monta a mensagem de erro, avança a barra geral e imprime a linha."""
    if item.result.status == "failed" and item.result.direct_error:
        item.result.message = f"direto: {item.result.direct_error} | {item.result.message}"
    observer.completed(item)


def process_item(item: DownloadJob, ctx: Context) -> Optional[DownloadJob]:
    task = ctx.progress.add_task(f"{truncate(item.item.display, 36)}", total=None, start=False)
    deferred = False
    try:
        if ctx.stop.is_set():
            raise Cancelled()
        _run_item(item, ctx, task)
    except NeedSteamcmd:
        deferred = True
        return item
    except Cancelled:
        item.result.status, item.result.message = "cancelled", "cancelado"
    except DownloadError as exc:
        item.result.status, item.result.message = "failed", str(exc)
    except Exception as exc:  # never let a thread exit silently
        item.result.status, item.result.message = "failed", f"erro inesperado ({type(exc).__name__}): {exc}"
    finally:
        ctx.progress.remove_task(task)
        if not deferred:
            ctx.on_complete(item)


def _run_pool(workers: int, jobs: list, ctx: Context) -> list:
    results = []
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dl")
    try:
        futures = [executor.submit(fn, *args) for fn, args in jobs]
        for fut in as_completed(futures):
            results.append(fut.result())
        return results
    except KeyboardInterrupt:
        ctx.stop.set()
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


def run_direct_stage(items: list[DownloadJob], ctx: Context) -> list[DownloadJob]:
    return [item for item in _run_pool(ctx.config.workers, [(process_item, (it, ctx)) for it in items], ctx) if item is not None]


def run_steamcmd_stage(items: list[DownloadJob], ctx: Context) -> None:
    if not items:
        return
    wanted = ctx.config.steamcmd_sessions or ctx.config.workers
    if ctx.config.steam_user and wanted > 1:
        wanted = 1  # the same account cannot have multiple simultaneous sessions
        ctx.progress.message("⚠ --steam-user: usando 1 sessão do steamcmd (a conta não pode logar em paralelo)")
    buckets = balance_items(items, max(1, min(wanted, len(items))))
    ctx.progress.message(f"steamcmd: {len(items)} itens em {len(buckets)} sessão(ões) simultâneas, "
                      "um login por sessão")
    _run_pool(len(buckets), [(steamcmd_session, (b, ctx, n + 1, n * STEAMCMD_START_STAGGER))
                             for n, b in enumerate(buckets)], ctx)


def execute_downloads(items: list[DownloadJob], config: DownloadConfig, out_dir: Path,
                      stop: Event, observer: Observer, steamcmd: str | None) -> None:
    """Coordinate individual HTTP downloads and batched SteamCMD fallback."""
    ctx = Context(config, out_dir, stop, observer, steamcmd,
                  lambda job: complete_item(job, observer))
    try:
        deferred = run_direct_stage(items, ctx)
        if deferred and not stop.is_set():
            run_steamcmd_stage(deferred, ctx)
    finally:
        if stop.is_set():
            for job in items:
                if job.result.status == 'pending':
                    job.result.status, job.result.message = 'cancelled', 'cancelado'
                    ctx.on_complete(job)
