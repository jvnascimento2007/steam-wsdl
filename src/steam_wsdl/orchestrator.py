from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from queue import Queue
from pathlib import Path
from typing import Optional
from .models import DownloadJob
from .runtime import Context
from .config import DownloadConfig
from .ports import Observer
from threading import Event
from .constants import API_RESULT_MESSAGES, STEAMCMD_START_STAGGER
from .errors import Cancelled, DownloadError, NeedSteamcmd
from .storage import direct_target, steamcmd_target
from .formatting import truncate
from .backends.http import download_direct
from .backends.steamcmd import steamcmd_session

def _run_item(item: DownloadJob, ctx: Context, task: int) -> None:
    """Process an item. Direct downloads finish here; items requiring SteamCMD
    raise NeedSteamcmd and are queued for persistent sessions afterward (one login per process)."""
    if item.result.status == "failed":  # already failed during resolution (e.g. unreadable collection)
        raise DownloadError(item.result.message)
    if item.item.api_result != 1:
        raise DownloadError(API_RESULT_MESSAGES.get(item.item.api_result, f"Steam returned code {item.item.api_result}"))
    if item.item.file_type == 2:
        raise DownloadError("nested collections are not supported")

    if item.item.file_url and not ctx.config.force_steamcmd:
        final = direct_target(item.item, ctx.out_dir)
        if final.exists() and not ctx.config.overwrite:
            item.result.status, item.result.method, item.result.path, item.result.message = "skipped", "direct", final, "already exists"
            return
        try:
            item.result.bytes = download_direct(item, final, ctx.config, ctx.stop, ctx.progress, task)
            item.result.status, item.result.method, item.result.path = "ok", "direct", final
            return
        except DownloadError as exc:
            if not ctx.steamcmd:
                raise
            item.result.direct_error = str(exc)

    if not ctx.steamcmd:
        raise DownloadError(
            f"no direct download URL (App ID {item.item.app_id or '?'}); "
            "install steamcmd (or use --steamcmd PATH) to download this item"
        )
    if not item.item.app_id:
        raise DownloadError("unknown game App ID; cannot use steamcmd")

    final_dir = steamcmd_target(item.item, ctx.out_dir)
    if final_dir.exists() and not ctx.config.overwrite:
        item.result.status, item.result.method, item.result.path, item.result.message = "skipped", "steamcmd", final_dir, "already exists"
        return
    raise NeedSteamcmd()


def complete_item(item: DownloadJob, observer: Observer) -> None:
    """Finalize an item by combining errors and notifying the presentation layer."""
    if item.result.status == "failed" and item.result.direct_error:
        item.result.message = f"direct: {item.result.direct_error} | {item.result.message}"
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
        item.result.status, item.result.message = "cancelled", "cancelled"
    except DownloadError as exc:
        item.result.status, item.result.message = "failed", str(exc)
    except Exception as exc:  # never let a thread exit silently
        item.result.status, item.result.message = "failed", f"unexpected error ({type(exc).__name__}): {exc}"
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
        ctx.progress.message("⚠ --steam-user: using 1 steamcmd session (the same account cannot log in concurrently)")
    workers = max(1, min(wanted, len(items)))
    pending = Queue()
    # Start larger downloads early; idle workers immediately claim another item.
    for item in sorted(items, key=lambda job: -job.item.size):
        pending.put(item)
    ctx.progress.message(f"steamcmd: {len(items)} items, up to {workers} persistent session(s), shared queue")
    _run_pool(workers, [(steamcmd_session, (pending, ctx, n + 1, n * STEAMCMD_START_STAGGER))
                        for n in range(workers)], ctx)


def execute_downloads(items: list[DownloadJob], config: DownloadConfig, out_dir: Path,
                      stop: Event, observer: Observer, steamcmd: str | None) -> None:
    """Coordinate individual HTTP downloads and persistent SteamCMD fallback."""
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
                    job.result.status, job.result.message = 'cancelled', 'cancelled'
                    ctx.on_complete(job)
