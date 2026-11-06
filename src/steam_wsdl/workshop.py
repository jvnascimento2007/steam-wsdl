from __future__ import annotations

import threading
from typing import Optional
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
    details = fetch_details(ids, args, stop)
    base = [item_from_detail(pid, details.get(pid)) for pid in ids]

    collections = [it for it in base if it.item.api_result == 1 and it.item.file_type == 2]
    children_map = fetch_collection_children([c.item.id for c in collections], args, stop) if collections else {}

    seen = set(ids)
    new_ids = [cid for children in children_map.values() for cid in children if cid not in seen]
    new_ids = list(dict.fromkeys(new_ids))
    child_details = fetch_details(new_ids, args, stop) if new_ids else {}

    final: list[DownloadJob] = []
    added: set[str] = set()

    def add(item: DownloadJob) -> None:
        if item.item.id not in added:
            added.add(item.item.id)
            final.append(item)

    for it in base:
        if it.item.file_type == 2 and it.item.api_result == 1:
            kids = children_map.get(it.item.id)
            if kids is None:
                it.result.status, it.result.message = "failed", "não foi possível ler os itens da coleção"
                add(it)
                continue
            observer.message(f"Coleção {it.item.title or it.item.id}: {len(kids)} itens")
            for kid in kids:
                add(item_from_detail(kid, details.get(kid) or child_details.get(kid)))
        else:
            add(it)
    return final

