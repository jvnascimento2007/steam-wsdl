from __future__ import annotations

from .config import DownloadConfig
import re
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse
from .errors import UsageError
from .formatting import truncate

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


def collect_ids(args: DownloadConfig) -> tuple[list[str], list[str]]:
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

