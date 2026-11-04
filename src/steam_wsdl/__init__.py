#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Steam Workshop Downloader
=========================

Baixa itens do Steam Workshop pela linha de comando, com downloads
concorrentes, retomada de downloads interrompidos e interface rica no terminal.

Como funciona
-------------
1. Consulta a API pública da Steam (sem chave) para obter título, jogo e
   tamanho de cada item (coleções são expandidas automaticamente).
2. Se o item expõe uma URL de download direto, baixa o arquivo com várias
   conexões em paralelo (com retry, backoff e retomada via HTTP Range).
3. Se não houver URL direta (comum em jogos modernos), usa o `steamcmd`
   como fallback, caso esteja instalado.

Requisitos: Python 3.9+  |  pip install requests rich
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

try:
    import requests
    from rich.console import Console, Group
    from rich.live import Live
    from rich.markup import escape
    from rich.panel import Panel
    from rich.progress import (
        BarColumn,
        DownloadColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
        TransferSpeedColumn,
    )
    from rich.table import Column, Table
    from rich.text import Text
except ImportError as exc:  # pragma: no cover
    sys.stderr.write(
        f"Dependência ausente: {exc.name}.\n"
        "Instale com: pip install requests rich\n"
    )
    sys.exit(2)

__version__ = "1.0.0"

API_DETAILS = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
API_COLLECTION = "https://api.steampowered.com/ISteamRemoteStorage/GetCollectionDetails/v1/"
RETRY_STATUS = {429, 500, 502, 503, 504}
USER_AGENT = f"SteamWorkshopDownloader/{__version__}"
STEAMCMD_MAX_PARALLEL = 2  # instâncias simultâneas do steamcmd (mais que isso costuma dar conflito)
CHUNK_SIZE = 64 * 1024

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130

API_RESULT_MESSAGES = {
    9: "item não encontrado, removido ou privado",
    15: "acesso negado ao item",
    8: "parâmetro inválido",
}


# --------------------------------------------------------------------------- #
# Exceções
# --------------------------------------------------------------------------- #
class WorkshopError(Exception):
    """Erro base da aplicação."""


class UsageError(WorkshopError):
    """Erro de uso (argumentos/arquivos de entrada inválidos)."""


class DownloadError(WorkshopError):
    """Falha definitiva ao baixar um item."""


class TransientError(WorkshopError):
    """Falha temporária; vale a pena tentar novamente."""


class Cancelled(WorkshopError):
    """Operação cancelada pelo usuário (Ctrl+C)."""


# --------------------------------------------------------------------------- #
# Modelos
# --------------------------------------------------------------------------- #
@dataclass
class Item:
    id: str
    title: str = ""
    app_id: str = ""
    file_url: str = ""
    filename: str = ""
    size: int = 0
    file_type: int = 0
    api_result: int = 1
    status: str = "pending"  # pending | ok | skipped | failed | cancelled
    method: str = "-"
    message: str = ""
    path: Optional[Path] = None
    bytes: int = 0

    @property
    def url(self) -> str:
        return f"https://steamcommunity.com/sharedfiles/filedetails/?id={self.id}"

    @property
    def display(self) -> str:
        return self.title or f"item {self.id}"


@dataclass
class Context:
    args: argparse.Namespace
    out_dir: Path
    stop: threading.Event
    console: Console
    files: Progress
    steamcmd: Optional[str]
    steamcmd_sem: threading.Semaphore


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def sanitize(name: str, max_len: int = 80) -> str:
    name = _INVALID_CHARS.sub("_", name or "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    if name.upper() in _RESERVED:
        name = f"_{name}"
    return name[:max_len].rstrip(" .")


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


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


# --------------------------------------------------------------------------- #
# Rede
# --------------------------------------------------------------------------- #
_local = threading.local()


def get_session() -> requests.Session:
    """Uma Session por thread (requests.Session não é garantidamente thread-safe)."""
    session = getattr(_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
        _local.session = session
    return session


def api_post(url: str, data: dict, args: argparse.Namespace, stop: threading.Event) -> dict:
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


def fetch_details(ids: list[str], args: argparse.Namespace, stop: threading.Event) -> dict[str, dict]:
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


def fetch_collection_children(coll_ids: list[str], args: argparse.Namespace, stop: threading.Event) -> dict[str, list[str]]:
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


def item_from_detail(pid: str, detail: Optional[dict]) -> Item:
    item = Item(id=pid)
    if not detail:
        item.api_result = 0
        return item
    item.api_result = int(detail.get("result", 0) or 0)
    if item.api_result == 1:
        item.title = str(detail.get("title") or "")
        app = detail.get("consumer_app_id") or detail.get("creator_app_id") or ""
        item.app_id = str(app) if app else ""
        item.file_url = str(detail.get("file_url") or "")
        item.filename = str(detail.get("filename") or "")
        item.size = int(detail.get("file_size") or 0)
        item.file_type = int(detail.get("file_type") or 0)
    return item


def resolve_items(ids: list[str], args: argparse.Namespace, stop: threading.Event, console: Console) -> list[Item]:
    details = fetch_details(ids, args, stop)
    base = [item_from_detail(pid, details.get(pid)) for pid in ids]

    collections = [it for it in base if it.api_result == 1 and it.file_type == 2]
    children_map = fetch_collection_children([c.id for c in collections], args, stop) if collections else {}

    seen = set(ids)
    new_ids = [cid for children in children_map.values() for cid in children if cid not in seen]
    new_ids = list(dict.fromkeys(new_ids))
    child_details = fetch_details(new_ids, args, stop) if new_ids else {}

    final: list[Item] = []
    added: set[str] = set()

    def add(item: Item) -> None:
        if item.id not in added:
            added.add(item.id)
            final.append(item)

    for it in base:
        if it.file_type == 2 and it.api_result == 1:
            kids = children_map.get(it.id)
            if kids is None:
                it.status, it.message = "failed", "não foi possível ler os itens da coleção"
                add(it)
                continue
            console.print(f"[cyan]Coleção[/] [bold]{escape(it.title or it.id)}[/]: {len(kids)} itens")
            for kid in kids:
                add(item_from_detail(kid, details.get(kid) or child_details.get(kid)))
        else:
            add(it)
    return final


# --------------------------------------------------------------------------- #
# Download direto
# --------------------------------------------------------------------------- #
def download_direct(item: Item, final: Path, ctx: Context, task: int) -> int:
    args = ctx.args
    final.parent.mkdir(parents=True, exist_ok=True)
    part = final.with_name(final.name + ".part")
    last: Optional[Exception] = None

    for attempt in range(args.retries + 1):
        if ctx.stop.is_set():
            raise Cancelled()
        try:
            existing = part.stat().st_size if part.exists() else 0
            headers = {"Range": f"bytes={existing}-"} if existing else {}
            with get_session().get(item.file_url, stream=True, headers=headers, timeout=(10, args.timeout)) as resp:
                if resp.status_code == 416:  # parte local inválida: recomeça do zero
                    part.unlink(missing_ok=True)
                    raise TransientError("retomada inválida, reiniciando o download")
                if resp.status_code in RETRY_STATUS:
                    raise TransientError(f"HTTP {resp.status_code}")
                if resp.status_code in (401, 403, 404, 410):
                    raise DownloadError(f"o servidor recusou o download (HTTP {resp.status_code})")
                resp.raise_for_status()

                resumed = resp.status_code == 206 and existing > 0
                if not resumed:
                    existing = 0
                length = int(resp.headers.get("Content-Length") or 0)
                total = (length + existing) if length else (item.size or None)

                ctx.files.update(task, total=total, completed=existing)
                ctx.files.start_task(task)

                with open(part, "ab" if resumed else "wb") as fh:
                    for chunk in resp.iter_content(CHUNK_SIZE):
                        if ctx.stop.is_set():
                            raise Cancelled()
                        if chunk:
                            fh.write(chunk)
                            ctx.files.update(task, advance=len(chunk))

            size = part.stat().st_size
            if total and size != total:
                raise TransientError(f"download incompleto ({human_size(size)} de {human_size(total)})")
            os.replace(part, final)
            return size

        except Cancelled:
            raise
        except DownloadError:
            raise
        except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError, TransientError) as exc:
            last = exc
        except requests.RequestException as exc:  # inclui HTTPError e URLs inválidas
            raise DownloadError(f"erro de rede: {exc}") from exc
        except OSError as exc:
            raise DownloadError(f"erro de disco: {exc}") from exc

        if attempt < args.retries:
            ctx.files.update(task, description=f"[yellow]↻ tentativa {attempt + 2}/{args.retries + 1}[/] {escape(truncate(item.display, 26))}")
            if ctx.stop.wait(min(2 ** attempt, 15)):
                raise Cancelled()

    raise DownloadError(f"falhou após {args.retries + 1} tentativas: {last}")


# --------------------------------------------------------------------------- #
# Fallback: steamcmd
# --------------------------------------------------------------------------- #
def find_steamcmd(explicit: Optional[str]) -> Optional[str]:
    if explicit:
        path = shutil.which(explicit) or (explicit if Path(explicit).is_file() else None)
        if not path:
            raise UsageError(f"steamcmd não encontrado em: {explicit}")
        return path
    for name in ("steamcmd", "steamcmd.sh", "steamcmd.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


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


def run_steamcmd(cmd: list[str], ctx: Context, tick=None) -> str:
    """Executa o steamcmd; `tick` (opcional) é chamado ~2x por segundo enquanto ele roda."""
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace"
        )
    except OSError as exc:
        raise DownloadError(f"não foi possível executar o steamcmd: {exc}") from exc

    started = time.monotonic()
    while True:
        try:
            out, _ = proc.communicate(timeout=0.5)
            return out or ""
        except subprocess.TimeoutExpired:
            if tick:
                tick()
            if ctx.stop.is_set():
                proc.kill()
                proc.communicate()
                raise Cancelled()
            if time.monotonic() - started > ctx.args.steamcmd_timeout:
                proc.kill()
                proc.communicate()
                raise DownloadError(f"steamcmd excedeu o tempo limite de {ctx.args.steamcmd_timeout}s")


def parse_steamcmd_error(output: str) -> str:
    text = output or ""
    if re.search(r"No subscription|Access Denied|Invalid Password|Login Failure", text, re.IGNORECASE):
        return ("a Steam exige uma conta que possua o jogo para baixar este item "
                "(use --steam-user com login já salvo no steamcmd)")
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("ERROR") or "failed" in line.lower():
            return f"steamcmd: {truncate(line, 160)}"
    return "o steamcmd terminou sem baixar o item"


def download_steamcmd(item: Item, final_dir: Path, ctx: Context, task: int) -> int:
    # O steamcmd não emite progresso utilizável quando a saída é capturada. Então medimos
    # o crescimento da pasta de trabalho e comparamos com o tamanho informado pela API.
    total = item.size or None
    ctx.files.update(task, total=total, completed=0,
                     description=f"[magenta]steamcmd[/] {escape(truncate(item.display, 28))}")
    ctx.files.start_task(task)
    with ctx.steamcmd_sem:
        if ctx.stop.is_set():
            raise Cancelled()
        tmp = Path(tempfile.mkdtemp(prefix=f"swd_{item.id}_"))
        try:
            login = ctx.args.steam_user or "anonymous"
            cmd = [ctx.steamcmd, "+force_install_dir", str(tmp), "+login", login,
                   "+workshop_download_item", item.app_id, item.id, "+quit"]
            watch = tmp / "steamapps" / "workshop"
            peak = [0]

            def tick() -> None:
                # Pico monotônico: evita a barra "voltar" quando o steamcmd move os arquivos
                # de downloads/ para content/ (e limita ao total para não passar de 100%).
                peak[0] = max(peak[0], dir_size(watch))
                done = min(peak[0], total) if total else peak[0]
                ctx.files.update(task, completed=done)

            output = run_steamcmd(cmd, ctx, tick)
            content =tmp / "steamapps" / "workshop" / "content" / item.app_id / item.id
            if not content.is_dir():
                raise DownloadError(parse_steamcmd_error(output))
            try:
                final_dir.parent.mkdir(parents=True, exist_ok=True)
                if final_dir.exists():
                    shutil.rmtree(final_dir)
                shutil.move(str(content), str(final_dir))
                size = dir_size(final_dir)
                ctx.files.update(task, total=size, completed=size)
                return size
            except OSError as exc:
                raise DownloadError(f"erro de disco: {exc}") from exc
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Orquestração por item
# --------------------------------------------------------------------------- #
def _run_item(item: Item, ctx: Context, task: int) -> None:
    if item.status == "failed":  # já falhou na resolução (ex.: coleção ilegível)
        raise DownloadError(item.message)
    if item.api_result != 1:
        raise DownloadError(API_RESULT_MESSAGES.get(item.api_result, f"a Steam retornou o código {item.api_result}"))
    if item.file_type == 2:
        raise DownloadError("coleções aninhadas não são suportadas")

    game_dir = ctx.out_dir / (item.app_id or "app_desconhecido")
    base = f"{sanitize(item.title) or 'item'} [{item.id}]"
    direct_error: Optional[DownloadError] = None

    if item.file_url and not ctx.args.force_steamcmd:
        raw_name = re.split(r"[\\/]", item.filename)[-1] if item.filename else ""
        suffix = re.sub(r"[^\w.]", "", Path(raw_name).suffix)[:12]
        final = game_dir / f"{base}{suffix}"
        if final.exists() and not ctx.args.overwrite:
            item.status, item.method, item.path, item.message = "skipped", "direto", final, "já existe"
            return
        try:
            item.bytes = download_direct(item, final, ctx, task)
            item.status, item.method, item.path = "ok", "direto", final
            return
        except DownloadError as exc:
            if not ctx.steamcmd:
                raise
            direct_error = exc
            ctx.files.update(task, description=f"[yellow]fallback[/] {escape(truncate(item.display, 30))}")

    if not ctx.steamcmd:
        raise DownloadError(
            f"sem URL de download direto (App ID {item.app_id or '?'}); "
            "instale o steamcmd (ou use --steamcmd CAMINHO) para baixar este item"
        )
    if not item.app_id:
        raise DownloadError("App ID do jogo desconhecido; não é possível usar o steamcmd")

    final_dir = game_dir / base
    if final_dir.exists() and not ctx.args.overwrite:
        item.status, item.method, item.path, item.message = "skipped", "steamcmd", final_dir, "já existe"
        return
    try:
        item.bytes = download_steamcmd(item, final_dir, ctx, task)
    except DownloadError as exc:
        if direct_error:
            raise DownloadError(f"direto: {direct_error} | steamcmd: {exc}") from exc
        raise
    item.status, item.method, item.path = "ok", "steamcmd", final_dir


def report_line(item: Item, console: Console) -> None:
    name = escape(truncate(item.display, 60))
    if item.status == "ok":
        console.print(f"[green]✓[/] {name} [dim]({human_size(item.bytes)} · {item.method})[/]")
    elif item.status == "skipped":
        console.print(f"[yellow]↷[/] {name} [dim]({item.message})[/]")
    elif item.status == "cancelled":
        console.print(f"[dim]■ {name} (cancelado)[/]")
    else:
        console.print(f"[red]✗[/] {name} [red]— {escape(item.message)}[/]")


def process_item(item: Item, ctx: Context, overall: Progress, overall_task: int) -> None:
    task = ctx.files.add_task(f"[cyan]{escape(truncate(item.display, 36))}[/]", total=None, start=False)
    try:
        if ctx.stop.is_set():
            raise Cancelled()
        _run_item(item, ctx, task)
    except Cancelled:
        item.status, item.message = "cancelled", "cancelado"
    except DownloadError as exc:
        item.status, item.message = "failed", str(exc)
    except Exception as exc:  # nunca deixa uma thread morrer silenciosamente
        item.status, item.message = "failed", f"erro inesperado ({type(exc).__name__}): {exc}"
    finally:
        ctx.files.remove_task(task)
        overall.advance(overall_task)
        report_line(item, ctx.console)


# --------------------------------------------------------------------------- #
# Entrada / CLI
# --------------------------------------------------------------------------- #
def bounded_int(lo: int, hi: int):
    def convert(value: str) -> int:
        try:
            number = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"'{value}' não é um número inteiro")
        if not lo <= number <= hi:
            raise argparse.ArgumentTypeError(f"deve estar entre {lo} e {hi}")
        return number
    return convert


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="steam_workshop_dl",
        description="Baixa mods do Steam Workshop com downloads concorrentes.",
        epilog=(
            "exemplos:\n"
            "  python steam_workshop_dl.py -u \"https://steamcommunity.com/sharedfiles/filedetails/?id=123456789\"\n"
            "  python steam_workshop_dl.py -f mods.txt -o ./meus_mods -w 8\n"
            "  python steam_workshop_dl.py -f mods.txt -u 987654321 --failed-file falhas.txt\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = p.add_argument_group("entrada (pelo menos uma)")
    src.add_argument("-u", "--url", action="append", default=[], metavar="LINK",
                     help="link (ou ID) de um item do Workshop; pode ser repetido")
    src.add_argument("-f", "--file", metavar="ARQUIVO.txt",
                     help="arquivo de texto com um link por linha (linhas com # ou ; são comentários)")

    p.add_argument("-o", "--output", default="workshop_downloads", metavar="PASTA",
                   help="pasta de saída (padrão: %(default)s)")
    p.add_argument("-w", "--workers", type=bounded_int(1, 32), default=4, metavar="N",
                   help="downloads simultâneos, 1-32 (padrão: %(default)s)")
    p.add_argument("-r", "--retries", type=bounded_int(0, 20), default=3, metavar="N",
                   help="tentativas extras em falhas de rede (padrão: %(default)s)")
    p.add_argument("-t", "--timeout", type=bounded_int(5, 600), default=30, metavar="SEG",
                   help="timeout de rede em segundos (padrão: %(default)s)")
    p.add_argument("--overwrite", action="store_true", help="sobrescreve itens já baixados")
    p.add_argument("--failed-file", metavar="ARQUIVO.txt",
                   help="grava os links que falharam neste arquivo (útil para tentar de novo)")

    sc = p.add_argument_group("steamcmd (fallback para itens sem download direto)")
    sc.add_argument("--steamcmd", metavar="CAMINHO", help="caminho do executável do steamcmd")
    sc.add_argument("--no-steamcmd", action="store_true", help="nunca usar o steamcmd")
    sc.add_argument("--force-steamcmd", action="store_true", help="usar sempre o steamcmd")
    sc.add_argument("--steam-user", metavar="USUARIO",
                    help="conta Steam (login já salvo no steamcmd); padrão: anonymous")
    sc.add_argument("--steamcmd-timeout", type=bounded_int(30, 7200), default=900, metavar="SEG",
                    help="timeout por item no steamcmd (padrão: %(default)s)")
    p.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")

    args = p.parse_args(argv)
    if not args.url and not args.file:
        p.error("informe ao menos um link com -u/--url ou um arquivo com -f/--file")
    if args.no_steamcmd and args.force_steamcmd:
        p.error("--no-steamcmd e --force-steamcmd são incompatíveis")
    return args


def collect_ids(args: argparse.Namespace) -> tuple[list[str], list[str]]:
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


def print_banner(console: Console) -> None:
    title = Text("Steam Workshop Downloader", style="bold cyan", justify="center")
    title.append(f"\nv{__version__}", style="dim")
    console.print(Panel(title, border_style="cyan", padding=(0, 4)))


def print_summary(items: list[Item], elapsed: float, console: Console) -> None:
    table = Table(title="Resumo", title_style="bold", header_style="bold cyan", expand=False)
    table.add_column("ID", style="dim", no_wrap=True)
    table.add_column("Item", overflow="ellipsis", max_width=40, no_wrap=True)
    table.add_column("Método", no_wrap=True)
    table.add_column("Tamanho", justify="right", no_wrap=True)
    table.add_column("Status", overflow="fold")

    styles = {"ok": "[green]✓ baixado[/]", "skipped": "[yellow]↷ ignorado[/]",
              "cancelled": "[dim]■ cancelado[/]", "failed": "[red]✗ falhou[/]"}
    for it in items:
        status = styles.get(it.status, "[dim]■ cancelado[/]")
        if it.message and it.status in ("failed", "skipped"):
            status += f" [dim]{escape(it.message)}[/]"
        table.add_row(it.id, escape(it.display), it.method, human_size(it.bytes) if it.bytes else "-", status)
    console.print(table)

    ok = sum(i.status == "ok" for i in items)
    skipped = sum(i.status == "skipped" for i in items)
    failed = sum(i.status == "failed" for i in items)
    cancelled = len(items) - ok - skipped - failed
    total_bytes = sum(i.bytes for i in items if i.status == "ok")
    line = Text()
    line.append(f"{ok} baixados", style="green")
    line.append(f" · {skipped} ignorados", style="yellow")
    line.append(f" · {failed} falhas", style="red" if failed else "dim")
    if cancelled:
        line.append(f" · {cancelled} cancelados", style="dim")
    line.append(f" · {human_size(total_bytes)} em {elapsed:.1f}s", style="bold")
    console.print(Panel(line, border_style="green" if not failed and not cancelled else "yellow"))


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def run(args: argparse.Namespace, console: Console) -> int:
    stop = threading.Event()

    ids, problems = collect_ids(args)
    for msg in problems[:10]:
        console.print(f"[yellow]⚠[/] {escape(msg)}")
    if len(problems) > 10:
        console.print(f"[yellow]⚠[/] … e mais {len(problems) - 10} problemas")
    if not ids:
        raise UsageError("nenhum link válido do Steam Workshop foi encontrado")

    out_dir = prepare_output(args.output)

    steamcmd = None if args.no_steamcmd else find_steamcmd(args.steamcmd)
    if args.force_steamcmd and not steamcmd:
        raise UsageError("--force-steamcmd exige o steamcmd instalado (ou --steamcmd CAMINHO)")

    console.print(f"[bold]Saída:[/] {escape(str(out_dir))}   [bold]Workers:[/] {args.workers}   "
                  f"[bold]steamcmd:[/] {'sim' if steamcmd else 'não'}")

    try:
        with console.status("[cyan]Consultando a API da Steam...[/]"):
            items = resolve_items(ids, args, stop, console)
    except Cancelled:
        console.print("[yellow]Interrompido.[/]")
        return EXIT_INTERRUPTED
    except WorkshopError as exc:
        console.print(Panel(f"[red]{escape(str(exc))}[/]", title="Erro ao consultar o Workshop", border_style="red"))
        return EXIT_PARTIAL

    console.print(f"[bold]{len(items)}[/] itens na fila\n")

    files = Progress(
        SpinnerColumn(),
        TextColumn("{task.description}", table_column=Column(width=40, no_wrap=True)),
        BarColumn(bar_width=None),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
        expand=True,
    )
    overall = Progress(
        TextColumn("[bold]Progresso geral", table_column=Column(width=40)),
        BarColumn(bar_width=None),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        expand=True,
    )
    overall_task = overall.add_task("geral", total=len(items))
    ctx = Context(args, out_dir, stop, console, files, steamcmd,
                  threading.Semaphore(min(args.workers, STEAMCMD_MAX_PARALLEL)))

    interrupted = False
    started = time.monotonic()
    with Live(Group(files, overall), console=console, refresh_per_second=12):
        executor = ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix="dl")
        try:
            futures = [executor.submit(process_item, it, ctx, overall, overall_task) for it in items]
            for fut in as_completed(futures):
                fut.result()
            executor.shutdown(wait=True)
        except KeyboardInterrupt:
            interrupted = True
            stop.set()
            console.print("[yellow]Interrompido pelo usuário — encerrando downloads em andamento...[/]")
            executor.shutdown(wait=True, cancel_futures=True)

    elapsed = time.monotonic() - started
    console.print()
    print_summary(items, elapsed, console)

    failed = [i for i in items if i.status == "failed"]
    if args.failed_file and failed:
        try:
            Path(args.failed_file).expanduser().write_text(
                "\n".join(i.url for i in failed) + "\n", encoding="utf-8")
            console.print(f"[dim]Links com falha salvos em {escape(args.failed_file)}[/]")
        except OSError as exc:
            console.print(f"[yellow]⚠ não foi possível gravar {escape(args.failed_file)}: {escape(str(exc))}[/]")
    if interrupted:
        console.print("[dim]Downloads parciais (.part) foram mantidos e serão retomados na próxima execução.[/]")
        return EXIT_INTERRUPTED
    return EXIT_PARTIAL if failed else EXIT_OK


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    console = Console()
    print_banner(console)
    try:
        return run(args, console)
    except UsageError as exc:
        console.print(Panel(f"[red]{escape(str(exc))}[/]", title="Erro", border_style="red"))
        return EXIT_USAGE
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrompido.[/]")
        return EXIT_INTERRUPTED
    except Exception as exc:  # última rede de segurança
        console.print(Panel(f"[red]{escape(type(exc).__name__)}: {escape(str(exc))}[/]",
                            title="Erro inesperado", border_style="red"))
        return EXIT_PARTIAL


if __name__ == "__main__":
    sys.exit(main())
