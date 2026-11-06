"""Dependencies shared by a download session; no UI or CLI implementation types."""
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable
from .config import DownloadConfig
from .models import DownloadJob
from .ports import Observer


@dataclass(frozen=True)
class Context:
    config: DownloadConfig
    out_dir: Path
    stop: Event
    progress: Observer
    steamcmd: str | None
    on_complete: Callable[[DownloadJob], None]
