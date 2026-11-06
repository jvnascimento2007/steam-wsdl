"""Workshop metadata and the independent result of a download attempt."""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class WorkshopItem:
    id: str
    title: str = ''
    app_id: str = ''
    file_url: str = ''
    filename: str = ''
    size: int = 0
    file_type: int = 0
    api_result: int = 1

    @property
    def url(self) -> str:
        return f'https://steamcommunity.com/sharedfiles/filedetails/?id={self.id}'

    @property
    def display(self) -> str:
        return self.title or f'item {self.id}'


@dataclass
class DownloadResult:
    status: Literal['pending', 'ok', 'skipped', 'failed', 'cancelled'] = 'pending'
    method: str = '-'
    message: str = ''
    direct_error: str = ''
    path: Path | None = None
    bytes: int = 0


@dataclass
class DownloadJob:
    # A job belongs to one worker at a time, including across the fallback stages.
    item: WorkshopItem
    result: DownloadResult = field(default_factory=DownloadResult)
