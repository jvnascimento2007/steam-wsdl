from __future__ import annotations

from .config import DownloadConfig
import re
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse
from .errors import UsageError
from .formatting import truncate

def parse_workshop_id(text: str) -> Optional[str]:
    """Extract the ID from a Workshop URL or a numeric ID."""
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


def collect_ids(args: DownloadConfig) -> tuple[list[str], list[str]]:
    """Return unique IDs in input order and a list of input problems."""
    ids: list[str] = []
    problems: list[str] = []

    for raw in args.url:
        pid = parse_workshop_id(raw)
        if pid:
            ids.append(pid)
        else:
            problems.append(f"invalid URL in --url: {raw}")

    if args.file:
        path = Path(args.file).expanduser()
        try:
            content = path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            raise UsageError(f"file not found: {path}")
        except IsADirectoryError:
            raise UsageError(f"'{path}' is a directory, not a file")
        except PermissionError:
            raise UsageError(f"permission denied while reading: {path}")
        except UnicodeDecodeError:
            raise UsageError(f"'{path}' is not UTF-8; save the file as UTF-8 text")
        except OSError as exc:
            raise UsageError(f"could not read '{path}': {exc}")

        for lineno, line in enumerate(content.splitlines(), 1):
            line = line.strip()
            if not line or line[0] in "#;":
                continue
            pid = parse_workshop_id(line.split()[0])
            if pid:
                ids.append(pid)
            else:
                problems.append(f"{path.name}, line {lineno}: invalid URL → {truncate(line, 70)}")

    return list(dict.fromkeys(ids)), problems

