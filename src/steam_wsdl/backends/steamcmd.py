from __future__ import annotations

import codecs
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional
from ..models import DownloadJob
from ..runtime import Context
from ..errors import Cancelled, DownloadError, UsageError
from ..formatting import truncate
from ..storage import dir_size, steamcmd_target

RE_STEAMCMD_OK = re.compile(r"Success\.\s+Downloaded item (\d+)")
RE_STEAMCMD_FAIL = re.compile(r"ERROR!\s+Download item (\d+) failed\s*\(([^)]*)\)")
_RE_NEEDS_ACCOUNT = re.compile(r"No subscription|Access Denied|Invalid Password|Login Failure", re.IGNORECASE)
SHUTDOWN_TIMEOUT = 5


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


class SessionError(DownloadError):
    """The process must be replaced before attempting another download."""


class SteamcmdProcess:
    """One logged-in console. Only its owning worker sends commands.

    Steam's prompt has no newline: read available chunks rather than readline().
    There is exactly one command in flight, and its next prompt is the command
    boundary. A full-sized file alone is never evidence of success.
    """

    def __init__(self, ctx: Context, root: Path, item: DownloadJob):
        self.root = root
        self.interactive = sys.platform != "win32"
        self.events: queue.Queue[str | None] = queue.Queue()
        cmd = [ctx.steamcmd, "+@ShutdownOnFailedCommand", "0", "+@NoPromptForPassword", "1",
               "+force_install_dir", str(root), "+login", ctx.config.steam_user or "anonymous"]
        if self.interactive:
            # SteamCMD does not reliably consume an open stdin pipe. A PTY also
            # makes its console flush prompts/results immediately.
            import pty
            import termios
            master, slave = pty.openpty()
            try:
                attrs = termios.tcgetattr(slave)
                attrs[3] &= ~termios.ECHO
                termios.tcsetattr(slave, termios.TCSANOW, attrs)
                # Acquire a controlling terminal in a tiny exec helper. Avoid
                # preexec_fn: it can deadlock when Popen runs inside worker threads.
                bootstrap = ("import fcntl,os,sys,termios; "
                             "fcntl.ioctl(0,termios.TIOCSCTTY,0); "
                             "os.execvp(sys.argv[1],sys.argv[1:])")
                self.proc = subprocess.Popen([sys.executable, "-c", bootstrap, *cmd],
                                             stdin=slave, stdout=slave, stderr=slave,
                                             start_new_session=True)
            except OSError as exc:
                os.close(master)
                raise SessionError(f"could not run steamcmd: {exc}") from exc
            finally:
                os.close(slave)
            self.output = os.fdopen(master, "r+b", buffering=0)
        else:
            # Keep non-POSIX downloads working without adding a ConPTY library.
            # This fallback consumes the same queue, but logs in per item.
            cmd += ["+workshop_download_item", item.item.app_id, item.item.id, "+quit"]
            try:
                self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                             stderr=subprocess.STDOUT, bufsize=0)
            except OSError as exc:
                raise SessionError(f"could not run steamcmd: {exc}") from exc
            self.output = self.proc.stdout
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while chunk := self.output.read(4096):
                self.events.put(decoder.decode(chunk))
            self.events.put(decoder.decode(b"", final=True))
        except OSError:
            # POSIX PTYs report EIO when the slave closes; the consumer handles EOF.
            pass
        finally:
            self.events.put(None)

    def send(self, command: str) -> None:
        try:
            self.output.write((command + "\n").encode())
            self.output.flush()
        except OSError as exc:
            raise SessionError(f"could not send command to steamcmd: {exc}") from exc

    def wait_prompt(self, ctx: Context, observe=lambda: 0) -> str:
        output = ""
        last_activity = time.monotonic()
        peak = 0
        while True:
            if ctx.stop.is_set():
                raise Cancelled()
            eof = False
            try:
                while True:
                    chunk = self.events.get_nowait()
                    if chunk is None:
                        eof = True
                        break
                    if chunk:
                        output = (output + chunk)[-65536:]
                        last_activity = time.monotonic()
            except queue.Empty:
                pass
            clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output)
            size = observe()
            if size > peak:
                peak, last_activity = size, time.monotonic()
            if re.search(r"(?:^|[\r\n])Steam>", clean):
                return clean
            if eof:
                if not self.interactive:
                    return clean
                raise SessionError(parse_steamcmd_error(clean))
            if time.monotonic() - last_activity > ctx.config.steamcmd_timeout:
                raise SessionError(f"steamcmd was idle for {ctx.config.steamcmd_timeout} s")
            ctx.stop.wait(0.1)

    def login(self, ctx: Context) -> None:
        if not self.interactive:
            return
        output = self.wait_prompt(ctx)
        if _RE_NEEDS_ACCOUNT.search(output):
            raise SessionError(parse_steamcmd_error(output))

    def download(self, it: DownloadJob, ctx: Context, row: int, number: int) -> None:
        work = self.root / "steamapps" / "workshop"
        staging = work / "downloads" / it.item.app_id / it.item.id
        content = work / "content" / it.item.app_id / it.item.id
        peak = 0

        def progress() -> int:
            nonlocal peak
            # These are two locations for the same content, not additive bytes.
            peak = max(peak, dir_size(staging), dir_size(content))
            state = "Starting"
            if peak:
                state = "Awaiting confirmation" if it.item.size and peak >= it.item.size else "Downloading"
            ctx.progress.update(row, completed=min(peak, it.item.size) if it.item.size else peak,
                                description=f"#{number} {state}: {truncate(it.item.display, 24)}")
            return peak

        if self.interactive:
            self.send(f"workshop_download_item {it.item.app_id} {it.item.id}")
        output = self.wait_prompt(ctx, progress)
        failure = RE_STEAMCMD_FAIL.search(output)
        if failure and failure.group(1) == it.item.id:
            raise DownloadError(steamcmd_reason(failure.group(2)))
        success = any(m.group(1) == it.item.id for m in RE_STEAMCMD_OK.finditer(output))
        if not success:
            raise DownloadError(parse_steamcmd_error(output))
        if not content.is_dir():
            raise DownloadError("steamcmd did not create the item directory")
        ctx.progress.update(row, description=f"#{number} Finalizing: {truncate(it.item.display, 24)}")
        dest = steamcmd_target(it.item, ctx.out_dir)
        try:
            # Measure before publication so even the final scan stays in staging.
            size = dir_size(content)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                shutil.rmtree(dest)
            # Staging is inside the output filesystem. Never silently fall back
            # to a potentially slow cross-device copy.
            content.rename(dest)
        except OSError as exc:
            raise DownloadError(f"disk error: {exc}") from exc
        it.result.bytes, it.result.method, it.result.path = size, "steamcmd", dest
        it.result.status = "ok"

    def close(self, graceful: bool = False) -> None:
        try:
            if self.proc.poll() is None:
                if graceful and self.interactive:
                    try:
                        self.send("quit")
                        self.proc.wait(timeout=SHUTDOWN_TIMEOUT)
                    except (SessionError, subprocess.TimeoutExpired):
                        pass
            # Shell launchers can leave a live child holding the PTY open even
            # after the launcher exits. Kill the whole isolated process group.
            if self.interactive:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif self.proc.poll() is None:
                self.proc.kill()
            self.proc.wait()
        finally:
            self.reader.join()
            self.output.close()


def steamcmd_session(items: queue.Queue[DownloadJob], ctx: Context,
                     number: int, delay: float = 0.0) -> None:
    """Consume a shared queue using one persistent process, restarting on failure.

    Claim work after the stagger delay so a worker whose work was already taken
    never starts an unnecessary SteamCMD process. An active job has one owner;
    recovery retries only that job, and untouched jobs remain in the queue.
    """
    if delay and ctx.stop.wait(delay):
        return
    process = None
    root = None

    def close(graceful=False):
        nonlocal process, root
        try:
            if process is not None:
                process.close(graceful)
        finally:
            process = None
            if root is not None:
                shutil.rmtree(root, ignore_errors=True)
                root = None

    try:
        while not ctx.stop.is_set():
            try:
                it = items.get_nowait()
            except queue.Empty:
                break
            row = None
            try:
                attempt = 0
                while True:
                    if ctx.stop.is_set():
                        raise Cancelled()
                    if row is not None:
                        ctx.progress.remove_task(row)
                        row = None
                    row = ctx.progress.add_task(
                        f"#{number} Starting: {truncate(it.item.display, 24)}", total=it.item.size or None)
                    try:
                        if process is None:
                            staging = ctx.out_dir / ".staging"
                            staging.mkdir(parents=True, exist_ok=True)
                            root = Path(tempfile.mkdtemp(prefix="session-", dir=staging))
                            process = SteamcmdProcess(ctx, root, it)
                            process.login(ctx)
                        try:
                            process.download(it, ctx, row, number)
                        finally:
                            # The compatibility process exits after its item,
                            # including item errors; never reuse its consumed EOF.
                            if not process.interactive:
                                close()
                        break
                    except SessionError:
                        close()
                        if attempt >= ctx.config.retries:
                            raise
                        ctx.progress.message(f"steamcmd #{number}: restarting session, attempt {attempt + 2}/{ctx.config.retries + 1}")
                        attempt += 1
            except Cancelled:
                it.result.status, it.result.message = "cancelled", "cancelled"
            except DownloadError as exc:
                it.result.status, it.result.message = "failed", str(exc)
            except OSError as exc:
                it.result.status, it.result.message = "failed", f"disk error: {exc}"
            except Exception as exc:
                close()
                it.result.status = "failed"
                it.result.message = f"unexpected error ({type(exc).__name__}): {exc}"
            finally:
                if row is not None:
                    ctx.progress.remove_task(row)
                items.task_done()
                ctx.on_complete(it)
    finally:
        close(graceful=not ctx.stop.is_set())
