#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Steam Workshop Downloader
=========================

Downloads Steam Workshop items from the command line, with concurrent downloads,
interrupted-download resumption, and a rich terminal interface.

How it works
-------------
1. Queries the public Steam API (no key required) to retrieve each item's title, game, and
   size (collections are expanded automatically).
2. If an item exposes a direct download URL, downloads the file using multiple
   parallel connections (with retries, backoff, and resumption via HTTP Range).
3. If no direct URL is available (common in modern games), uses `steamcmd`
   as a fallback, if it is installed.

Requirements: Python 3.9+  |  pip install requests rich
"""
from __future__ import annotations

import argparse
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

try:
    import requests
    from rich.console import Console, Group
    from rich.live import Live
    from rich.markup import escape
    from rich.panel import Panel
    from rich.progress import (
        BarColumn,
        DownloadColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
        TransferSpeedColumn,
    )
    from rich.table import Column, Table
    from rich.text import Text
except ImportError as exc:  # pragma: no cover
    sys.stderr.write(
        f"Dependência ausente: {exc.name}.\n"
        "Instale com: pip install requests rich\n"
    )
    sys.exit(2)

__version__ = "1.0.0"

API_DETAILS = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
API_COLLECTION = "https://api.steampowered.com/ISteamRemoteStorage/GetCollectionDetails/v1/"
RETRY_STATUS = {429, 500, 502, 503, 504}
USER_AGENT = f"SteamWorkshopDownloader/{__version__}"
STEAMCMD_BATCH_MAX = 40  # items per steamcmd run (limits command-line length)
STEAMCMD_ITEM_OVERHEAD = 1_000_000  # estimated fixed cost per item (in "bytes") for balancing sessions
STEAMCMD_START_STAGGER = 1.0  # seconds between the start of one session and the next
CHUNK_SIZE = 64 * 1024

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130

API_RESULT_MESSAGES = {
    9: "item não encontrado, removido ou privado",
    15: "acesso negado ao item",
    8: "parâmetro inválido",
}


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class WorkshopError(Exception):
    """Erro base da aplicação."""


class UsageError(WorkshopError):
    """Erro de uso (argumentos/arquivos de entrada inválidos)."""


class DownloadError(WorkshopError):
    """Falha definitiva ao baixar um item."""


class TransientError(WorkshopError):
    """Falha temporária; vale a pena tentar novamente."""


class Cancelled(WorkshopError):
    """Operação cancelada pelo usuário (Ctrl+C)."""


class NeedSteamcmd(Exception):
    """Sinal interno: o item deve ser baixado pelo steamcmd (em lote)."""


# --------------------------------------------------------------------------- #
# Data models
# --------------------------------------------------------------------------- #
@dataclass
class Item:
    id: str
    title: str = ""
    app_id: str = ""
    file_url: str = ""
    filename: str = ""
    size: int = 0
    file_type: int = 0
    api_result: int = 1
    status: str = "pending"  # pending | ok | skipped | failed | cancelled
    method: str = "-"
    message: str = ""
    direct_error: str = ""  # reason for direct-download failure (when fallback was used)
    path: Optional[Path] = None
    bytes: int = 0

    @property
    def url(self) -> str:
        return f"https://steamcommunity.com/sharedfiles/filedetails/?id={self.id}"

    @property
    def display(self) -> str:
        return self.title or f"item {self.id}"


@dataclass
class Context:
    args: argparse.Namespace
    out_dir: Path
    stop: threading.Event
    console: Console
    files: Progress
    steamcmd: Optional[str]
    lock: threading.Lock = field(default_factory=threading.Lock)
    steamcmd_queue: list = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Utilities
# --------------------------------------------------------------------------- #
_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def sanitize(name: str, max_len: int = 80) -> str:
    name = _INVALID_CHARS.sub("_", name or "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    if name.upper() in _RESERVED:
        name = f"_{name}"
    return name[:max_len].rstrip(" .")


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def parse_workshop_id(text: str) -> Optional[str]:
    """Extrai o ID de um link do Workshop (ou ID numérico puro)."""
    text = text.strip().strip("'\"")
    if not text:
        return None
    if re.fullmatch(r"\d{3,}", text):
        return text
    m = re.fullmatch(r"steam://url/CommunityFilePage/(\d+)", text, re.IGNORECASE)
    if m:
        return m.group(1)
    if not re.match(r"^[a-z][a-z0-9+.-]*://", text, re.IGNORECASE):
        text = "https://" + text
    try:
        parsed = urlparse(text)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if parsed.scheme in ("http", "https") and (host == "steamcommunity.com" or host.endswith(".steamcommunity.com")):
        value = parse_qs(parsed.query).get("id", [""])[0]
        if value.isdigit():
            return value
    return None


# --------------------------------------------------------------------------- #
# Network
# --------------------------------------------------------------------------- #
_local = threading.local()


def get_session() -> requests.Session:
    """Uma Session por thread (requests.Session não é garantidamente thread-safe)."""
    session = getattr(_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
        _local.session = session
    return session


def api_post(url: str, data: dict, args: argparse.Namespace, stop: threading.Event) -> dict:
    last: Optional[Exception] = None
    for attempt in range(args.retries + 1):
        if stop.is_set():
            raise Cancelled()
        try:
            resp = get_session().post(url, data=data, timeout=args.timeout)
            if resp.status_code in RETRY_STATUS:
                raise TransientError(f"HTTP {resp.status_code}")
            resp.raise_for_status()
            return resp.json()
        except (requests.ConnectionError, requests.Timeout, TransientError) as exc:
            last = exc
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else "?"
            raise WorkshopError(f"a API da Steam respondeu com HTTP {code}") from exc
        except ValueError as exc:
            raise WorkshopError("a API da Steam devolveu uma resposta inválida (JSON)") from exc
        except requests.RequestException as exc:
            raise WorkshopError(f"erro de rede ao consultar a API: {exc}") from exc
        if attempt < args.retries and stop.wait(min(2 ** attempt, 10)):
            raise Cancelled()
    raise WorkshopError(f"não foi possível contatar a API da Steam após {args.retries + 1} tentativas: {last}")


def fetch_details(ids: list[str], args: argparse.Namespace, stop: threading.Event) -> dict[str, dict]:
    details: dict[str, dict] = {}
    for start in range(0, len(ids), 100):
        batch = ids[start:start + 100]
        data = {"itemcount": len(batch)}
        for i, pid in enumerate(batch):
            data[f"publishedfileids[{i}]"] = pid
        payload = api_post(API_DETAILS, data, args, stop)
        for entry in payload.get("response", {}).get("publishedfiledetails", []) or []:
            details[str(entry.get("publishedfileid"))] = entry
    return details


def fetch_collection_children(coll_ids: list[str], args: argparse.Namespace, stop: threading.Event) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    if not coll_ids:
        return result
    data = {"collectioncount": len(coll_ids)}
    for i, pid in enumerate(coll_ids):
        data[f"publishedfileids[{i}]"] = pid
    payload = api_post(API_COLLECTION, data, args, stop)
    for entry in payload.get("response", {}).get("collectiondetails", []) or []:
        if int(entry.get("result", 0)) == 1:
            result[str(entry.get("publishedfileid"))] = [
                str(c.get("publishedfileid")) for c in entry.get("children", []) or [] if c.get("publishedfileid")
            ]
    return result


def item_from_detail(pid: str, detail: Optional[dict]) -> Item:
    item = Item(id=pid)
    if not detail:
        item.api_result = 0
        return item
    item.api_result = int(detail.get("result", 0) or 0)
    if item.api_result == 1:
        item.title = str(detail.get("title") or "")
        app = detail.get("consumer_app_id") or detail.get("creator_app_id") or ""
        item.app_id = str(app) if app else ""
        item.file_url = str(detail.get("file_url") or "")
        item.filename = str(detail.get("filename") or "")
        item.size = int(detail.get("file_size") or 0)
        item.file_type = int(detail.get("file_type") or 0)
    return item


def resolve_items(ids: list[str], args: argparse.Namespace, stop: threading.Event, console: Console) -> list[Item]:
    details = fetch_details(ids, args, stop)
    base = [item_from_detail(pid, details.get(pid)) for pid in ids]

    collections = [it for it in base if it.api_result == 1 and it.file_type == 2]
    children_map = fetch_collection_children([c.id for c in collections], args, stop) if collections else {}

    seen = set(ids)
    new_ids = [cid for children in children_map.values() for cid in children if cid not in seen]
    new_ids = list(dict.fromkeys(new_ids))
    child_details = fetch_details(new_ids, args, stop) if new_ids else {}

    final: list[Item] = []
    added: set[str] = set()

    def add(item: Item) -> None:
        if item.id not in added:
            added.add(item.id)
            final.append(item)

    for it in base:
        if it.file_type == 2 and it.api_result == 1:
            kids = children_map.get(it.id)
            if kids is None:
                it.status, it.message = "failed", "não foi possível ler os itens da coleção"
                add(it)
                continue
            console.print(f"[cyan]Coleção[/] [bold]{escape(it.title or it.id)}[/]: {len(kids)} itens")
            for kid in kids:
                add(item_from_detail(kid, details.get(kid) or child_details.get(kid)))
        else:
            add(it)
    return final


# --------------------------------------------------------------------------- #
# Direct download
# --------------------------------------------------------------------------- #
def download_direct(item: Item, final: Path, ctx: Context, task: int) -> int:
    args = ctx.args
    final.parent.mkdir(parents=True, exist_ok=True)
    part = final.with_name(final.name + ".part")
    last: Optional[Exception] = None

    for attempt in range(args.retries + 1):
        if ctx.stop.is_set():
            raise Cancelled()
        try:
            existing = part.stat().st_size if part.exists() else 0
            headers = {"Range": f"bytes={existing}-"} if existing else {}
            with get_session().get(item.file_url, stream=True, headers=headers, timeout=(10, args.timeout)) as resp:
                if resp.status_code == 416:  # invalid local part: restart from scratch
                    part.unlink(missing_ok=True)
                    raise TransientError("retomada inválida, reiniciando o download")
                if resp.status_code in RETRY_STATUS:
                    raise TransientError(f"HTTP {resp.status_code}")
                if resp.status_code in (401, 403, 404, 410):
                    raise DownloadError(f"o servidor recusou o download (HTTP {resp.status_code})")
                resp.raise_for_status()

                resumed = resp.status_code == 206 and existing > 0
                if not resumed:
                    existing = 0
                length = int(resp.headers.get("Content-Length") or 0)
                total = (length + existing) if length else (item.size or None)

                ctx.files.update(task, total=total, completed=existing)
                ctx.files.start_task(task)

                with open(part, "ab" if resumed else "wb") as fh:
                    for chunk in resp.iter_content(CHUNK_SIZE):
                        if ctx.stop.is_set():
                            raise Cancelled()
                        if chunk:
                            fh.write(chunk)
                            ctx.files.update(task, advance=len(chunk))

            size = part.stat().st_size
            if total and size != total:
                raise TransientError(f"download incompleto ({human_size(size)} de {human_size(total)})")
            os.replace(part, final)
            return size

        except Cancelled:
            raise
        except DownloadError:
            raise
        except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError, TransientError) as exc:
            last = exc
        except requests.RequestException as exc:  # includes HTTPError and invalid URLs
            raise DownloadError(f"erro de rede: {exc}") from exc
        except OSError as exc:
            raise DownloadError(f"erro de disco: {exc}") from exc

        if attempt < args.retries:
            ctx.files.update(task, description=f"[yellow]↻ tentativa {attempt + 2}/{args.retries + 1}[/] {escape(truncate(item.display, 26))}")
            if ctx.stop.wait(min(2 ** attempt, 15)):
                raise Cancelled()

    raise DownloadError(f"falhou após {args.retries + 1} tentativas: {last}")


# --------------------------------------------------------------------------- #
# Fallback: steamcmd
# --------------------------------------------------------------------------- #
def find_steamcmd(explicit: Optional[str]) -> Optional[str]:
    if explicit:
        path = shutil.which(explicit) or (explicit if Path(explicit).is_file() else None)
        if not path:
            raise UsageError(f"steamcmd não encontrado em: {explicit}")
        return path
    for name in ("steamcmd", "steamcmd.sh", "steamcmd.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


def dir_size(path: Path) -> int:
    """Soma o tamanho dos arquivos de uma pasta (tolerante a arquivos que somem no meio)."""
    total = 0
    try:
        for root, _dirs, names in os.walk(path):
            for name in names:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
    except OSError:
        pass
    return total


RE_STEAMCMD_OK = re.compile(r"Success\.\s+Downloaded item (\d+)")
RE_STEAMCMD_FAIL = re.compile(r"ERROR!\s+Download item (\d+) failed\s*\(([^)]*)\)")
_RE_NEEDS_ACCOUNT = re.compile(r"No subscription|Access Denied|Invalid Password|Login Failure", re.IGNORECASE)


def steamcmd_reason(reason: str) -> str:
    if _RE_NEEDS_ACCOUNT.search(reason):
        return ("a Steam exige uma conta que possua o jogo para baixar este item "
                "(use --steam-user com login já salvo no steamcmd)")
    return f"steamcmd: {truncate(reason.strip() or 'falha desconhecida', 160)}"


def parse_steamcmd_error(output: str) -> str:
    text = output or ""
    if _RE_NEEDS_ACCOUNT.search(text):
        return steamcmd_reason(text)
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("ERROR") or "failed" in line.lower():
            return f"steamcmd: {truncate(line, 160)}"
    return "o steamcmd terminou sem baixar o item"


def steamcmd_target(item: Item, ctx: Context) -> Path:
    return ctx.out_dir / (item.app_id or "app_desconhecido") / f"{sanitize(item.title) or 'item'} [{item.id}]"


def _steamcmd_batch(items: list[Item], ctx: Context, overall: Progress, overall_task: int,
                    number: int) -> None:
    """Uma única execução do steamcmd (um login) baixando vários itens em sequência.

    steamcmd does not report progress when its output is captured, so:
      * each item’s completion/error is determined from the "Success..."/"ERROR!..." output lines;
      * progress is determined by the size of files in the working directory;
      * items with no output event are checked at the end by verifying whether the item directory exists.
    """
    tmp = Path(tempfile.mkdtemp(prefix="swd_steamcmd_"))
    work = tmp / "steamapps" / "workshop"
    pending: dict[str, Item] = {it.id: it for it in items}
    lines: list[str] = []
    events: "queue.Queue[Optional[str]]" = queue.Queue()
    proc: Optional[subprocess.Popen] = None
    aborted = ""
    row: Optional[int] = None          # progress row for the item currently being processed
    current_id: Optional[str] = None
    peak = 0

    def drop_row() -> None:
        nonlocal row, current_id
        if row is not None:
            ctx.files.remove_task(row)
        row, current_id = None, None

    def staging(it: Item) -> Path:
        return work / "downloads" / it.app_id / it.id

    def content(it: Item) -> Path:
        return work / "content" / it.app_id / it.id

    def finish(it: Item, status: str, message: str = "") -> None:
        pending.pop(it.id, None)
        if it.id == current_id:
            drop_row()
        it.status, it.message = status, message
        complete_item(it, ctx, overall, overall_task)

    def finish_ok(it: Item) -> None:
        src = content(it)
        if not src.is_dir():
            return finish(it, "failed", "o steamcmd não gerou a pasta do item")
        dest = steamcmd_target(it, ctx)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(src), str(dest))
        except OSError as exc:
            return finish(it, "failed", f"erro de disco: {exc}")
        it.bytes, it.method, it.path = dir_size(dest), "steamcmd", dest
        finish(it, "ok")

    try:
        login = ctx.args.steam_user or "anonymous"
        # ShutdownOnFailedCommand 0: if an item fails, steamcmd continues to the next one.
        # NoPromptForPassword 1: never hangs while waiting for a password.
        cmd = [ctx.steamcmd, "+@ShutdownOnFailedCommand", "0", "+@NoPromptForPassword", "1",
               "+force_install_dir", str(tmp), "+login", login]
        for it in items:
            cmd += ["+workshop_download_item", it.app_id, it.id]
        cmd.append("+quit")

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, errors="replace", bufsize=1)
        except OSError as exc:
            for it in list(pending.values()):
                finish(it, "failed", f"não foi possível executar o steamcmd: {exc}")
            return

        def reader() -> None:
            try:
                for line in proc.stdout:  # type: ignore[union-attr]
                    events.put(line)
            finally:
                events.put(None)

        threading.Thread(target=reader, daemon=True).start()

        last_activity = time.monotonic()
        eof = False
        while not eof:
            try:
                while True:
                    line = events.get_nowait()
                    if line is None:
                        eof = True
                        break
                    line = line.rstrip()
                    if not line:
                        continue
                    lines.append(line)
                    del lines[:-200]
                    last_activity = time.monotonic()
                    m = RE_STEAMCMD_OK.search(line)
                    if m and m.group(1) in pending:
                        finish_ok(pending[m.group(1)])
                        continue
                    m = RE_STEAMCMD_FAIL.search(line)
                    if m and m.group(1) in pending:
                        finish(pending[m.group(1)], "failed", steamcmd_reason(m.group(2)))
            except queue.Empty:
                pass
            if eof:
                break
            if ctx.stop.is_set():
                aborted = "cancel"
                break

            cur = next(iter(pending.values()), None)  # steamcmd downloads in list order
            if cur is not None:
                if cur.id != current_id:
                    drop_row()
                    current_id, peak = cur.id, 0
                    row = ctx.files.add_task(
                        f"[magenta]#{number}[/] {escape(truncate(cur.display, 34))}", total=cur.size or None)
                size = dir_size(staging(cur)) + dir_size(content(cur))
                if size > peak:  # monotonic peak: the progress bar does not move backward when files are moved
                    peak = size
                    last_activity = time.monotonic()
                ctx.files.update(row, completed=min(peak, cur.size) if cur.size else peak)

            if time.monotonic() - last_activity > ctx.args.steamcmd_timeout:
                aborted = "timeout"
                break
            ctx.stop.wait(0.4)

        if aborted and proc.poll() is None:
            proc.kill()
        proc.wait()

        if aborted == "cancel":
            for it in list(pending.values()):
                finish(it, "cancelled", "cancelado")
        elif aborted == "timeout":
            for it in list(pending.values()):
                finish(it, "failed", f"steamcmd ficou {ctx.args.steamcmd_timeout}s sem atividade")
        else:
            output = "\n".join(lines)
            for it in list(pending.values()):
                if content(it).is_dir():
                    finish_ok(it)
                else:
                    finish(it, "failed", parse_steamcmd_error(output))
    finally:
        drop_row()
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        shutil.rmtree(tmp, ignore_errors=True)


def steamcmd_session(items: list[Item], ctx: Context, overall: Progress, overall_task: int,
                     number: int, delay: float = 0.0) -> None:
    """Uma sessão do steamcmd: processa seus itens em lotes (um login por lote)."""
    if delay and ctx.stop.wait(delay):  # stagger the start to avoid contention over steamcmd files
        return
    for start in range(0, len(items), STEAMCMD_BATCH_MAX):
        batch = items[start:start + STEAMCMD_BATCH_MAX]
        if ctx.stop.is_set():
            break
        try:
            _steamcmd_batch(batch, ctx, overall, overall_task, number)
        except Exception as exc:  # never let the thread exit without reporting the error
            for it in batch:
                if it.status == "pending":
                    it.status = "failed"
                    it.message = f"erro inesperado ({type(exc).__name__}): {exc}"
                    complete_item(it, ctx, overall, overall_task)


def balance_items(items: list[Item], n: int) -> list[list[Item]]:
    """Divide os itens em até `n` grupos de carga parecida (maiores primeiro, sempre no grupo
    mais leve). Assim cada sessão faz um único login e todas terminam quase juntas."""
    buckets: list[list[int]] = [[] for _ in range(n)]
    loads = [0] * n
    weight = [max(it.size, 0) + STEAMCMD_ITEM_OVERHEAD for it in items]
    for i in sorted(range(len(items)), key=lambda k: -weight[k]):
        k = loads.index(min(loads))
        buckets[k].append(i)
        loads[k] += weight[i]
    return [[items[i] for i in sorted(b)] for b in buckets if b]  # preserves the original order


# --------------------------------------------------------------------------- #
# Per-item orchestration
# --------------------------------------------------------------------------- #
def _run_item(item: Item, ctx: Context, task: int) -> None:
    """Processa um item. Downloads diretos terminam aqui; os que precisam do steamcmd
    raise NeedSteamcmd and are grouped into sessions afterward (one login for multiple items)."""
    if item.status == "failed":  # already failed during resolution (e.g. unreadable collection)
        raise DownloadError(item.message)
    if item.api_result != 1:
        raise DownloadError(API_RESULT_MESSAGES.get(item.api_result, f"a Steam retornou o código {item.api_result}"))
    if item.file_type == 2:
        raise DownloadError("coleções aninhadas não são suportadas")

    game_dir = ctx.out_dir / (item.app_id or "app_desconhecido")
    base = f"{sanitize(item.title) or 'item'} [{item.id}]"

    if item.file_url and not ctx.args.force_steamcmd:
        raw_name = re.split(r"[\\/]", item.filename)[-1] if item.filename else ""
        suffix = re.sub(r"[^\w.]", "", Path(raw_name).suffix)[:12]
        final = game_dir / f"{base}{suffix}"
        if final.exists() and not ctx.args.overwrite:
            item.status, item.method, item.path, item.message = "skipped", "direto", final, "já existe"
            return
        try:
            item.bytes = download_direct(item, final, ctx, task)
            item.status, item.method, item.path = "ok", "direto", final
            return
        except DownloadError as exc:
            if not ctx.steamcmd:
                raise
            item.direct_error = str(exc)

    if not ctx.steamcmd:
        raise DownloadError(
            f"sem URL de download direto (App ID {item.app_id or '?'}); "
            "instale o steamcmd (ou use --steamcmd CAMINHO) para baixar este item"
        )
    if not item.app_id:
        raise DownloadError("App ID do jogo desconhecido; não é possível usar o steamcmd")

    final_dir = steamcmd_target(item, ctx)
    if final_dir.exists() and not ctx.args.overwrite:
        item.status, item.method, item.path, item.message = "skipped", "steamcmd", final_dir, "já existe"
        return
    raise NeedSteamcmd()


def report_line(item: Item, console: Console) -> None:
    name = escape(truncate(item.display, 60))
    if item.status == "ok":
        console.print(f"[green]✓[/] {name} [dim]({human_size(item.bytes)} · {item.method})[/]")
    elif item.status == "skipped":
        console.print(f"[yellow]↷[/] {name} [dim]({item.message})[/]")
    elif item.status == "cancelled":
        console.print(f"[dim]■ {name} (cancelado)[/]")
    else:
        console.print(f"[red]✗[/] {name} [red]— {escape(item.message)}[/]")


def complete_item(item: Item, ctx: Context, overall: Progress, overall_task: int) -> None:
    """Finaliza um item: monta a mensagem de erro, avança a barra geral e imprime a linha."""
    if item.status == "failed" and item.direct_error:
        item.message = f"direto: {item.direct_error} | {item.message}"
    overall.advance(overall_task)
    report_line(item, ctx.console)


def process_item(item: Item, ctx: Context, overall: Progress, overall_task: int) -> None:
    task = ctx.files.add_task(f"[cyan]{escape(truncate(item.display, 36))}[/]", total=None, start=False)
    deferred = False
    try:
        if ctx.stop.is_set():
            raise Cancelled()
        _run_item(item, ctx, task)
    except NeedSteamcmd:
        deferred = True
        with ctx.lock:
            ctx.steamcmd_queue.append(item)
    except Cancelled:
        item.status, item.message = "cancelled", "cancelado"
    except DownloadError as exc:
        item.status, item.message = "failed", str(exc)
    except Exception as exc:  # never let a thread exit silently
        item.status, item.message = "failed", f"erro inesperado ({type(exc).__name__}): {exc}"
    finally:
        ctx.files.remove_task(task)
        if not deferred:
            complete_item(item, ctx, overall, overall_task)


def _run_pool(workers: int, jobs: list, ctx: Context) -> None:
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dl")
    try:
        futures = [executor.submit(fn, *args) for fn, args in jobs]
        for fut in as_completed(futures):
            fut.result()
        executor.shutdown(wait=True)
    except KeyboardInterrupt:
        ctx.stop.set()
        executor.shutdown(wait=True, cancel_futures=True)
        raise


def run_direct_stage(items: list[Item], ctx: Context, overall: Progress, overall_task: int) -> None:
    _run_pool(ctx.args.workers, [(process_item, (it, ctx, overall, overall_task)) for it in items], ctx)


def run_steamcmd_stage(items: list[Item], ctx: Context, overall: Progress, overall_task: int) -> None:
    wanted = ctx.args.steamcmd_sessions or ctx.args.workers
    if ctx.args.steam_user and wanted > 1:
        wanted = 1  # the same account cannot have multiple simultaneous sessions
        ctx.console.print("[yellow]⚠[/] --steam-user: usando 1 sessão do steamcmd (a conta não pode logar em paralelo)")
    buckets = balance_items(items, max(1, min(wanted, len(items))))
    ctx.console.print(f"[magenta]steamcmd:[/] {len(items)} itens em {len(buckets)} sessão(ões) simultâneas, "
                      "um login por sessão")
    _run_pool(len(buckets), [(steamcmd_session, (b, ctx, overall, overall_task, n + 1, n * STEAMCMD_START_STAGGER))
                             for n, b in enumerate(buckets)], ctx)


# --------------------------------------------------------------------------- #
# Input / CLI
# --------------------------------------------------------------------------- #
def bounded_int(lo: int, hi: int):
    def convert(value: str) -> int:
        try:
            number = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"'{value}' não é um número inteiro")
        if not lo <= number <= hi:
            raise argparse.ArgumentTypeError(f"deve estar entre {lo} e {hi}")
        return number
    return convert


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="steam_workshop_dl",
        description="Baixa mods do Steam Workshop com downloads concorrentes.",
        epilog=(
            "exemplos:\n"
            "  python steam_workshop_dl.py -u \"https://steamcommunity.com/sharedfiles/filedetails/?id=123456789\"\n"
            "  python steam_workshop_dl.py -f mods.txt -o ./meus_mods -w 8\n"
            "  python steam_workshop_dl.py -f mods.txt -u 987654321 --failed-file falhas.txt\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = p.add_argument_group("entrada (pelo menos uma)")
    src.add_argument("-u", "--url", action="append", default=[], metavar="LINK",
                     help="link (ou ID) de um item do Workshop; pode ser repetido")
    src.add_argument("-f", "--file", metavar="ARQUIVO.txt",
                     help="arquivo de texto com um link por linha (linhas com # ou ; são comentários)")

    p.add_argument("-o", "--output", default="workshop_downloads", metavar="PASTA",
                   help="pasta de saída (padrão: %(default)s)")
    p.add_argument("-w", "--workers", type=bounded_int(1, 32), default=4, metavar="N",
                   help="downloads simultâneos, 1-32 (padrão: %(default)s)")
    p.add_argument("-r", "--retries", type=bounded_int(0, 20), default=3, metavar="N",
                   help="tentativas extras em falhas de rede (padrão: %(default)s)")
    p.add_argument("-t", "--timeout", type=bounded_int(5, 600), default=30, metavar="SEG",
                   help="timeout de rede em segundos (padrão: %(default)s)")
    p.add_argument("--overwrite", action="store_true", help="sobrescreve itens já baixados")
    p.add_argument("--failed-file", metavar="ARQUIVO.txt",
                   help="grava os links que falharam neste arquivo (útil para tentar de novo)")

    sc = p.add_argument_group("steamcmd (fallback para itens sem download direto)")
    sc.add_argument("--steamcmd", metavar="CAMINHO", help="caminho do executável do steamcmd")
    sc.add_argument("--no-steamcmd", action="store_true", help="nunca usar o steamcmd")
    sc.add_argument("--force-steamcmd", action="store_true", help="usar sempre o steamcmd")
    sc.add_argument("--steam-user", metavar="USUARIO",
                    help="conta Steam (login já salvo no steamcmd); padrão: anonymous")
    sc.add_argument("--steamcmd-sessions", type=bounded_int(1, 16), default=None, metavar="N",
                    help="sessões simultâneas do steamcmd; cada uma faz um único login e baixa "
                         "vários itens em sequência (padrão: o mesmo valor de -w; 1 com --steam-user)")
    sc.add_argument("--steamcmd-timeout", type=bounded_int(30, 7200), default=900, metavar="SEG",
                    help="encerra uma sessão do steamcmd após SEG segundos sem atividade (padrão: %(default)s)")
    p.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")

    args = p.parse_args(argv)
    if not args.url and not args.file:
        p.error("informe ao menos um link com -u/--url ou um arquivo com -f/--file")
    if args.no_steamcmd and args.force_steamcmd:
        p.error("--no-steamcmd e --force-steamcmd são incompatíveis")
    return args


def collect_ids(args: argparse.Namespace) -> tuple[list[str], list[str]]:
    """Devolve (ids únicos em ordem, lista de problemas encontrados)."""
    ids: list[str] = []
    problems: list[str] = []

    for raw in args.url:
        pid = parse_workshop_id(raw)
        if pid:
            ids.append(pid)
        else:
            problems.append(f"link inválido em --url: {raw}")

    if args.file:
        path = Path(args.file).expanduser()
        try:
            content = path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            raise UsageError(f"arquivo não encontrado: {path}")
        except IsADirectoryError:
            raise UsageError(f"'{path}' é uma pasta, não um arquivo")
        except PermissionError:
            raise UsageError(f"sem permissão para ler: {path}")
        except UnicodeDecodeError:
            raise UsageError(f"'{path}' não está em UTF-8; salve o arquivo como texto UTF-8")
        except OSError as exc:
            raise UsageError(f"não foi possível ler '{path}': {exc}")

        for lineno, line in enumerate(content.splitlines(), 1):
            line = line.strip()
            if not line or line[0] in "#;":
                continue
            pid = parse_workshop_id(line.split()[0])
            if pid:
                ids.append(pid)
            else:
                problems.append(f"{path.name}, linha {lineno}: link inválido → {truncate(line, 70)}")

    return list(dict.fromkeys(ids)), problems


def prepare_output(path_str: str) -> Path:
    out = Path(path_str).expanduser()
    try:
        out.mkdir(parents=True, exist_ok=True)
        probe = out / ".write_test"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        raise UsageError(f"não foi possível usar a pasta de saída '{out}': {exc}")
    return out.resolve()


def print_banner(console: Console) -> None:
    title = Text("Steam Workshop Downloader", style="bold cyan", justify="center")
    title.append(f"\nv{__version__}", style="dim")
    console.print(Panel(title, border_style="cyan", padding=(0, 4)))


def print_summary(items: list[Item], elapsed: float, console: Console) -> None:
    table = Table(title="Resumo", title_style="bold", header_style="bold cyan", expand=False)
    table.add_column("ID", style="dim", no_wrap=True)
    table.add_column("Item", overflow="ellipsis", max_width=40, no_wrap=True)
    table.add_column("Método", no_wrap=True)
    table.add_column("Tamanho", justify="right", no_wrap=True)
    table.add_column("Status", overflow="fold")

    styles = {"ok": "[green]✓ baixado[/]", "skipped": "[yellow]↷ ignorado[/]",
              "cancelled": "[dim]■ cancelado[/]", "failed": "[red]✗ falhou[/]"}
    for it in items:
        status = styles.get(it.status, "[dim]■ cancelado[/]")
        if it.message and it.status in ("failed", "skipped"):
            status += f" [dim]{escape(it.message)}[/]"
        table.add_row(it.id, escape(it.display), it.method, human_size(it.bytes) if it.bytes else "-", status)
    console.print(table)

    ok = sum(i.status == "ok" for i in items)
    skipped = sum(i.status == "skipped" for i in items)
    failed = sum(i.status == "failed" for i in items)
    cancelled = len(items) - ok - skipped - failed
    total_bytes = sum(i.bytes for i in items if i.status == "ok")
    line = Text()
    line.append(f"{ok} baixados", style="green")
    line.append(f" · {skipped} ignorados", style="yellow")
    line.append(f" · {failed} falhas", style="red" if failed else "dim")
    if cancelled:
        line.append(f" · {cancelled} cancelados", style="dim")
    line.append(f" · {human_size(total_bytes)} em {elapsed:.1f}s", style="bold")
    console.print(Panel(line, border_style="green" if not failed and not cancelled else "yellow"))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def run(args: argparse.Namespace, console: Console) -> int:
    stop = threading.Event()

    ids, problems = collect_ids(args)
    for msg in problems[:10]:
        console.print(f"[yellow]⚠[/] {escape(msg)}")
    if len(problems) > 10:
        console.print(f"[yellow]⚠[/] … e mais {len(problems) - 10} problemas")
    if not ids:
        raise UsageError("nenhum link válido do Steam Workshop foi encontrado")

    out_dir = prepare_output(args.output)

    steamcmd = None if args.no_steamcmd else find_steamcmd(args.steamcmd)
    if args.force_steamcmd and not steamcmd:
        raise UsageError("--force-steamcmd exige o steamcmd instalado (ou --steamcmd CAMINHO)")

    console.print(f"[bold]Saída:[/] {escape(str(out_dir))}   [bold]Workers:[/] {args.workers}   "
                  f"[bold]steamcmd:[/] {'sim' if steamcmd else 'não'}")

    try:
        with console.status("[cyan]Consultando a API da Steam...[/]"):
            items = resolve_items(ids, args, stop, console)
    except Cancelled:
        console.print("[yellow]Interrompido.[/]")
        return EXIT_INTERRUPTED
    except WorkshopError as exc:
        console.print(Panel(f"[red]{escape(str(exc))}[/]", title="Erro ao consultar o Workshop", border_style="red"))
        return EXIT_PARTIAL

    console.print(f"[bold]{len(items)}[/] itens na fila\n")

    files = Progress(
        SpinnerColumn(),
        TextColumn("{task.description}", table_column=Column(width=40, no_wrap=True)),
        BarColumn(bar_width=None),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
        expand=True,
    )
    overall = Progress(
        TextColumn("[bold]Progresso geral", table_column=Column(width=40)),
        BarColumn(bar_width=None),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        expand=True,
    )
    overall_task = overall.add_task("geral", total=len(items))
    ctx = Context(args, out_dir, stop, console, files, steamcmd)

    interrupted = False
    started = time.monotonic()
    with Live(Group(files, overall), console=console, refresh_per_second=12):
        try:
            run_direct_stage(items, ctx, overall, overall_task)
            if ctx.steamcmd_queue and not stop.is_set():
                run_steamcmd_stage(ctx.steamcmd_queue, ctx, overall, overall_task)
        except KeyboardInterrupt:
            interrupted = True
            stop.set()
            console.print("[yellow]Interrompido pelo usuário — encerrando downloads em andamento...[/]")

    elapsed = time.monotonic() - started
    console.print()
    print_summary(items, elapsed, console)

    failed = [i for i in items if i.status == "failed"]
    if args.failed_file and failed:
        try:
            Path(args.failed_file).expanduser().write_text(
                "\n".join(i.url for i in failed) + "\n", encoding="utf-8")
            console.print(f"[dim]Links com falha salvos em {escape(args.failed_file)}[/]")
        except OSError as exc:
            console.print(f"[yellow]⚠ não foi possível gravar {escape(args.failed_file)}: {escape(str(exc))}[/]")
    if interrupted:
        console.print("[dim]Downloads parciais (.part) foram mantidos e serão retomados na próxima execução.[/]")
        return EXIT_INTERRUPTED
    return EXIT_PARTIAL if failed else EXIT_OK


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    console = Console()
    print_banner(console)
    try:
        return run(args, console)
    except UsageError as exc:
        console.print(Panel(f"[red]{escape(str(exc))}[/]", title="Erro", border_style="red"))
        return EXIT_USAGE
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrompido.[/]")
        return EXIT_INTERRUPTED
    except Exception as exc:  # final safety net
        console.print(Panel(f"[red]{escape(type(exc).__name__)}: {escape(str(exc))}[/]",
                            title="Erro inesperado", border_style="red"))
        return EXIT_PARTIAL


if __name__ == "__main__":
    sys.exit(main())
