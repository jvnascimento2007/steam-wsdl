from __future__ import annotations

import threading
from typing import Optional
from .errors import Cancelled
from .config import DownloadConfig
from .models import DownloadJob, WorkshopItem
from .ports import Observer
from .backends.steam_api import fetch_details, fetch_collection_children

def item_from_detail(pid: str, detail: Optional[dict]) -> DownloadJob:
    detail = detail or {}
    result = int(detail.get("result", 0) or 0)
    if result != 1:
        return DownloadJob(WorkshopItem(id=pid, api_result=result))
    app = detail.get("consumer_app_id") or detail.get("creator_app_id") or ""
    return DownloadJob(WorkshopItem(
        id=pid, api_result=result,
        title=str(detail.get("title") or ""),
        app_id=str(app) if app else "",
        file_url=str(detail.get("file_url") or ""),
        filename=str(detail.get("filename") or ""),
        size=int(detail.get("file_size") or 0),
        file_type=int(detail.get("file_type") or 0),
    ))


def resolve_items(ids: list[str], args: DownloadConfig, stop: threading.Event, observer: Observer) -> list[DownloadJob]:
    # Older metadata responses omit file_type, even for collections whose
    # file_url points to their cover. Probe those IDs with the collection API.
    jobs: dict[str, DownloadJob] = {}
    children_map: dict[str, list[str]] = {}
    pending = list(dict.fromkeys(ids))
    while pending:
        if stop.is_set():
            raise Cancelled()
        details = fetch_details(pending, args, stop)
        jobs.update((pid, item_from_detail(pid, details.get(pid))) for pid in pending)
        candidates = [pid for pid in pending if jobs[pid].item.api_result == 1
                      and (jobs[pid].item.file_type == 2 or 'file_type' not in details[pid])]
        children = fetch_collection_children(candidates, args, stop) if candidates else {}
        for pid in candidates:
            job = jobs[pid]
            if pid in children:
                children_map[pid] = children[pid]
                observer.message(f"Collection {job.item.title or pid}: {len(children[pid])} items")
            elif job.item.file_type == 2:
                job.result.status, job.result.message = "failed", "could not read the collection items"
        pending = list(dict.fromkeys(child for pid in candidates
                                     for child in children.get(pid, []) if child not in jobs))

    # Iterative depth-first expansion preserves input/collection order and avoids
    # recursion limits, repeated downloads, and loops in nested collections.
    final: list[DownloadJob] = []
    seen: set[str] = set()
    stack = list(reversed(ids))
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        if pid in children_map:
            stack.extend(reversed(children_map[pid]))
        else:
            final.append(jobs[pid])
    return final
