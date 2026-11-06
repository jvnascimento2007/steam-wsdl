from __future__ import annotations

import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional
from ..models import DownloadJob
from ..runtime import Context
from ..constants import STEAMCMD_BATCH_MAX
from ..errors import UsageError
from ..formatting import truncate
from ..storage import dir_size, steamcmd_target

RE_STEAMCMD_OK = re.compile(r"Success\.\s+Downloaded item (\d+)")
RE_STEAMCMD_FAIL = re.compile(r"ERROR!\s+Download item (\d+) failed\s*\(([^)]*)\)")
_RE_NEEDS_ACCOUNT = re.compile(r"No subscription|Access Denied|Invalid Password|Login Failure", re.IGNORECASE)


def find_steamcmd(explicit: Optional[str]) -> Optional[str]:
    if explicit:
        path = shutil.which(explicit) or (explicit if Path(explicit).is_file() else None)
        if not path:
            raise UsageError(f"steamcmd not found at: {explicit}")
        return path
    for name in ("steamcmd", "steamcmd.sh", "steamcmd.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


def steamcmd_reason(reason: str) -> str:
    if _RE_NEEDS_ACCOUNT.search(reason):
        return ("Steam requires an account that owns the game to download this item "
                "(use --steam-user with a saved SteamCMD login)")
    return f"steamcmd: {truncate(reason.strip() or 'unknown failure', 160)}"


def parse_steamcmd_error(output: str) -> str:
    text = output or ""
    if _RE_NEEDS_ACCOUNT.search(text):
        return steamcmd_reason(text)
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("ERROR") or "failed" in line.lower():
            return f"steamcmd: {truncate(line, 160)}"
    return "steamcmd exited without downloading the item"


def _steamcmd_batch(items: list[DownloadJob], ctx: Context,
                    number: int) -> None:
    """One SteamCMD invocation (one login) downloading multiple items sequentially.

    steamcmd does not report progress when its output is captured, so:
      * each item’s completion/error is determined from the "Success..."/"ERROR!..." output lines;
      * progress is determined by the size of files in the working directory;
      * items with no output event are checked at the end by verifying whether the item directory exists.
    """
    tmp = Path(tempfile.mkdtemp(prefix="steam-wsdl-steamcmd-"))
    work = tmp / "steamapps" / "workshop"
    pending: dict[str, DownloadJob] = {it.item.id: it for it in items}
    lines: list[str] = []
    events: "queue.Queue[Optional[str]]" = queue.Queue()
    proc: Optional[subprocess.Popen] = None
    reader_thread: Optional[threading.Thread] = None
    aborted = ""
    row: Optional[int] = None          # progress row for the item currently being processed
    current_id: Optional[str] = None
    peak = 0

    def drop_row() -> None:
        nonlocal row, current_id
        if row is not None:
            ctx.progress.remove_task(row)
        row, current_id = None, None

    def staging(it: DownloadJob) -> Path:
        return work / "downloads" / it.item.app_id / it.item.id

    def content(it: DownloadJob) -> Path:
        return work / "content" / it.item.app_id / it.item.id

    def finish(it: DownloadJob, status: str, message: str = "") -> None:
        pending.pop(it.item.id, None)
        if it.item.id == current_id:
            drop_row()
        it.result.status, it.result.message = status, message
        ctx.on_complete(it)

    def finish_ok(it: DownloadJob) -> None:
        src = content(it)
        if not src.is_dir():
            return finish(it, "failed", "steamcmd did not create the item directory")
        dest = steamcmd_target(it.item, ctx.out_dir)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(src), str(dest))
        except OSError as exc:
            return finish(it, "failed", f"disk error: {exc}")
        it.result.bytes, it.result.method, it.result.path = dir_size(dest), "steamcmd", dest
        finish(it, "ok")

    try:
        login = ctx.config.steam_user or "anonymous"
        # ShutdownOnFailedCommand 0: if an item fails, steamcmd continues to the next one.
        # NoPromptForPassword 1: never hangs while waiting for a password.
        cmd = [ctx.steamcmd, "+@ShutdownOnFailedCommand", "0", "+@NoPromptForPassword", "1",
               "+force_install_dir", str(tmp), "+login", login]
        for it in items:
            cmd += ["+workshop_download_item", it.item.app_id, it.item.id]
        cmd.append("+quit")

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, errors="replace", bufsize=1)
        except OSError as exc:
            for it in list(pending.values()):
                finish(it, "failed", f"could not run steamcmd: {exc}")
            return

        def reader() -> None:
            try:
                for line in proc.stdout:  # type: ignore[union-attr]
                    events.put(line)
            finally:
                events.put(None)

        reader_thread = threading.Thread(target=reader, daemon=True)
        reader_thread.start()

        last_activity = time.monotonic()
        eof = False
        while True:
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
                if cur.item.id != current_id:
                    drop_row()
                    current_id, peak = cur.item.id, 0
                    row = ctx.progress.add_task(
                        f"#{number} {truncate(cur.item.display, 34)}", total=cur.item.size or None)
                size = dir_size(staging(cur)) + dir_size(content(cur))
                if size > peak:  # monotonic peak: the progress bar does not move backward when files are moved
                    peak = size
                    last_activity = time.monotonic()
                ctx.progress.update(row, completed=min(peak, cur.item.size) if cur.item.size else peak)

            if time.monotonic() - last_activity > ctx.config.steamcmd_timeout:
                aborted = "timeout"
                break
            ctx.stop.wait(0.4)

        if aborted and proc.poll() is None:
            proc.kill()
        proc.wait()

        if aborted == "cancel":
            for it in list(pending.values()):
                finish(it, "cancelled", "cancelled")
        elif aborted == "timeout":
            for it in list(pending.values()):
                finish(it, "failed", f"steamcmd was idle for {ctx.config.steamcmd_timeout} s")
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
        if reader_thread is not None:
            reader_thread.join()
        if proc is not None and proc.stdout is not None:
            proc.stdout.close()
        shutil.rmtree(tmp, ignore_errors=True)


def steamcmd_session(items: list[DownloadJob], ctx: Context,
                     number: int, delay: float = 0.0) -> None:
    """A SteamCMD session processes items in batches, with one login per batch."""
    if delay and ctx.stop.wait(delay):  # stagger the start to avoid contention over steamcmd files
        return
    for start in range(0, len(items), STEAMCMD_BATCH_MAX):
        batch = items[start:start + STEAMCMD_BATCH_MAX]
        if ctx.stop.is_set():
            break
        try:
            _steamcmd_batch(batch, ctx, number)
        except Exception as exc:  # never let the thread exit without reporting the error
            for it in batch:
                if it.result.status == "pending":
                    it.result.status = "failed"
                    it.result.message = f"unexpected error ({type(exc).__name__}): {exc}"
                    ctx.on_complete(it)

