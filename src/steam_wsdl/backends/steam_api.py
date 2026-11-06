from __future__ import annotations

import threading
from typing import Optional
import requests
from ..config import DownloadConfig
from ..constants import API_DETAILS, API_COLLECTION, RETRY_STATUS, USER_AGENT
from ..errors import Cancelled, TransientError, WorkshopError

_local = threading.local()

def get_session() -> requests.Session:
    """Uma Session por thread (requests.Session não é garantidamente thread-safe)."""
    session = getattr(_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
        _local.session = session
    return session


def api_post(url: str, data: dict, args: DownloadConfig, stop: threading.Event) -> dict:
    last: Optional[Exception] = None
    for attempt in range(args.retries + 1):
        if stop.is_set():
            raise Cancelled()
        try:
            resp = get_session().post(url, data=data, timeout=args.timeout)
            if resp.status_code in RETRY_STATUS:
                raise TransientError(f"HTTP {resp.status_code}")
            resp.raise_for_status()
            return resp.json()
        except (requests.ConnectionError, requests.Timeout, TransientError) as exc:
            last = exc
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else "?"
            raise WorkshopError(f"a API da Steam respondeu com HTTP {code}") from exc
        except ValueError as exc:
            raise WorkshopError("a API da Steam devolveu uma resposta inválida (JSON)") from exc
        except requests.RequestException as exc:
            raise WorkshopError(f"erro de rede ao consultar a API: {exc}") from exc
        if attempt < args.retries and stop.wait(min(2 ** attempt, 10)):
            raise Cancelled()
    raise WorkshopError(f"não foi possível contatar a API da Steam após {args.retries + 1} tentativas: {last}")


def fetch_details(ids: list[str], args: DownloadConfig, stop: threading.Event) -> dict[str, dict]:
    details: dict[str, dict] = {}
    for start in range(0, len(ids), 100):
        batch = ids[start:start + 100]
        data = {"itemcount": len(batch)}
        for i, pid in enumerate(batch):
            data[f"publishedfileids[{i}]"] = pid
        payload = api_post(API_DETAILS, data, args, stop)
        for entry in payload.get("response", {}).get("publishedfiledetails", []) or []:
            details[str(entry.get("publishedfileid"))] = entry
    return details


def fetch_collection_children(coll_ids: list[str], args: DownloadConfig, stop: threading.Event) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    if not coll_ids:
        return result
    data = {"collectioncount": len(coll_ids)}
    for i, pid in enumerate(coll_ids):
        data[f"publishedfileids[{i}]"] = pid
    payload = api_post(API_COLLECTION, data, args, stop)
    for entry in payload.get("response", {}).get("collectiondetails", []) or []:
        if int(entry.get("result", 0)) == 1:
            result[str(entry.get("publishedfileid"))] = [
                str(c.get("publishedfileid")) for c in entry.get("children", []) or [] if c.get("publishedfileid")
            ]
    return result

