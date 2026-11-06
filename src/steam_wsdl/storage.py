from __future__ import annotations

import os
import re
from pathlib import Path
from .errors import UsageError
from .models import WorkshopItem

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def sanitize(name: str, max_len: int = 80) -> str:
    name = _INVALID_CHARS.sub("_", name or "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    if name.upper() in _RESERVED:
        name = f"_{name}"
    return name[:max_len].rstrip(" .")


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


def steamcmd_target(item: WorkshopItem, out_dir: Path) -> Path:
    return out_dir / (item.app_id or "app_desconhecido") / f"{sanitize(item.title) or 'item'} [{item.id}]"


def direct_target(item: WorkshopItem, out_dir: Path) -> Path:
    raw_name = re.split(r"[\\/]", item.filename)[-1] if item.filename else ""
    suffix = re.sub(r"[^\w.]", "", Path(raw_name).suffix)[:12]
    target = steamcmd_target(item, out_dir)
    return target.with_name(target.name + suffix)
