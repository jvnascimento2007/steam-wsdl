"""Validated CLI options, independent of argparse."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class DownloadConfig:
    url: list[str] = field(default_factory=list)
    file: str | None = None
    output: str = 'workshop_downloads'
    workers: int = 4
    retries: int = 3
    timeout: int = 30
    overwrite: bool = False
    failed_file: str | None = None
    steamcmd: str | None = None
    no_steamcmd: bool = False
    force_steamcmd: bool = False
    steam_user: str | None = None
    steamcmd_sessions: int | None = None
    steamcmd_timeout: int = 900
