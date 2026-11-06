from __future__ import annotations


class WorkshopError(Exception):
    """Base application error."""


class UsageError(WorkshopError):
    """Usage error (invalid arguments or input files)."""


class DownloadError(WorkshopError):
    """Permanent failure while downloading an item."""


class TransientError(WorkshopError):
    """Transient failure that can be retried."""


class Cancelled(WorkshopError):
    """Operation cancelled by the user (Ctrl+C)."""


class NeedSteamcmd(Exception):
    """Internal signal: download the item through a persistent SteamCMD session."""

