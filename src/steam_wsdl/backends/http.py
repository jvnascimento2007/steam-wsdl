from __future__ import annotations

import os
from pathlib import Path
from typing import Optional
import requests
from ..models import DownloadJob
from ..config import DownloadConfig
from ..ports import Observer
from threading import Event
from ..constants import CHUNK_SIZE, RETRY_STATUS
from ..errors import Cancelled, DownloadError, TransientError
from ..formatting import human_size, truncate
from .steam_api import get_session

def download_direct(item: DownloadJob, final: Path, config: DownloadConfig, stop: Event, progress: Observer, task: int) -> int:
    args = config
    final.parent.mkdir(parents=True, exist_ok=True)
    part = final.with_name(final.name + ".part")
    last: Optional[Exception] = None

    for attempt in range(args.retries + 1):
        if stop.is_set():
            raise Cancelled()
        try:
            existing = part.stat().st_size if part.exists() else 0
            headers = {"Range": f"bytes={existing}-"} if existing else {}
            with get_session().get(item.item.file_url, stream=True, headers=headers, timeout=(10, args.timeout)) as resp:
                if resp.status_code == 416:  # invalid local part: restart from scratch
                    part.unlink(missing_ok=True)
                    raise TransientError("invalid resume range, restarting download")
                if resp.status_code in RETRY_STATUS:
                    raise TransientError(f"HTTP {resp.status_code}")
                if resp.status_code in (401, 403, 404, 410):
                    raise DownloadError(f"the server refused the download (HTTP {resp.status_code})")
                resp.raise_for_status()

                resumed = resp.status_code == 206 and existing > 0
                if not resumed:
                    existing = 0
                length = int(resp.headers.get("Content-Length") or 0)
                total = (length + existing) if length else (item.item.size or None)

                progress.update(task, total=total, completed=existing)
                progress.start_task(task)

                with open(part, "ab" if resumed else "wb") as fh:
                    for chunk in resp.iter_content(CHUNK_SIZE):
                        if stop.is_set():
                            raise Cancelled()
                        if chunk:
                            fh.write(chunk)
                            progress.update(task, advance=len(chunk))

            size = part.stat().st_size
            if total and size != total:
                raise TransientError(f"incomplete download ({human_size(size)} of {human_size(total)})")
            os.replace(part, final)
            return size

        except Cancelled:
            raise
        except DownloadError:
            raise
        except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError, TransientError) as exc:
            last = exc
        except requests.RequestException as exc:  # includes HTTPError and invalid URLs
            raise DownloadError(f"network error: {exc}") from exc
        except OSError as exc:
            raise DownloadError(f"disk error: {exc}") from exc

        if attempt < args.retries:
            progress.update(task, description=f"↻ attempt {attempt + 2}/{args.retries + 1} {truncate(item.item.display, 26)}")
            if stop.wait(min(2 ** attempt, 15)):
                raise Cancelled()

    raise DownloadError(f"failed after {args.retries + 1} attempts: {last}")

