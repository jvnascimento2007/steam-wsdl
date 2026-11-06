from __future__ import annotations


__version__ = "1.0.0"

API_DETAILS = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
API_COLLECTION = "https://api.steampowered.com/ISteamRemoteStorage/GetCollectionDetails/v1/"
RETRY_STATUS = {429, 500, 502, 503, 504}
USER_AGENT = f"steam-wsdl/{__version__}"
STEAMCMD_BATCH_MAX = 40  # items per steamcmd run (limits command-line length)
STEAMCMD_ITEM_OVERHEAD = 1_000_000  # estimated fixed cost per item (in "bytes") for balancing sessions
STEAMCMD_START_STAGGER = 1.0  # seconds between the start of one session and the next
CHUNK_SIZE = 64 * 1024

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130

API_RESULT_MESSAGES = {
    9: "item not found, removed, or private",
    15: "access to the item denied",
    8: "invalid parameter",
}


