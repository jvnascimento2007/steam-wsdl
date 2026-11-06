from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path
from typing import Optional
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from .constants import __version__, EXIT_OK, EXIT_PARTIAL, EXIT_USAGE, EXIT_INTERRUPTED
from .config import DownloadConfig
from .errors import UsageError, Cancelled, WorkshopError
from .inputs import collect_ids
from .storage import prepare_output
from .workshop import resolve_items
from .backends.steamcmd import find_steamcmd
from .orchestrator import execute_downloads
from .presentation.terminal import TerminalObserver, print_banner, print_summary

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
    sc.add_argument("--steamcmd-sessions", type=bounded_int(1, 16), default=None, metavar="N",
                    help="sessões simultâneas do steamcmd; cada uma faz um único login e baixa "
                         "vários itens em sequência (padrão: o mesmo valor de -w; 1 com --steam-user)")
    sc.add_argument("--steamcmd-timeout", type=bounded_int(30, 7200), default=900, metavar="SEG",
                    help="encerra uma sessão do steamcmd após SEG segundos sem atividade (padrão: %(default)s)")
    p.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")

    args = p.parse_args(argv)
    if not args.url and not args.file:
        p.error("informe ao menos um link com -u/--url ou um arquivo com -f/--file")
    if args.no_steamcmd and args.force_steamcmd:
        p.error("--no-steamcmd e --force-steamcmd são incompatíveis")
    return args


def run(args: DownloadConfig, console: Console) -> int:
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

    observer = TerminalObserver(console)
    try:
        with console.status("[cyan]Consultando a API da Steam...[/]"):
            items = resolve_items(ids, args, stop, observer)
    except Cancelled:
        console.print("[yellow]Interrompido.[/]")
        return EXIT_INTERRUPTED
    except WorkshopError as exc:
        console.print(Panel(f"[red]{escape(str(exc))}[/]", title="Erro ao consultar o Workshop", border_style="red"))
        return EXIT_PARTIAL

    console.print(f"[bold]{len(items)}[/] itens na fila\n")

    observer.start(len(items))

    interrupted = False
    started = time.monotonic()
    with observer:
        try:
            execute_downloads(items, args, out_dir, stop, observer, steamcmd)
        except KeyboardInterrupt:
            interrupted = True
            stop.set()
            console.print("[yellow]Interrompido pelo usuário — encerrando downloads em andamento...[/]")

    elapsed = time.monotonic() - started
    console.print()
    print_summary(items, elapsed, console)

    failed = [i for i in items if i.result.status == "failed"]
    if args.failed_file and failed:
        try:
            Path(args.failed_file).expanduser().write_text(
                "\n".join(i.item.url for i in failed) + "\n", encoding="utf-8")
            console.print(f"[dim]Links com falha salvos em {escape(args.failed_file)}[/]")
        except OSError as exc:
            console.print(f"[yellow]⚠ não foi possível gravar {escape(args.failed_file)}: {escape(str(exc))}[/]")
    if interrupted:
        console.print("[dim]Downloads parciais (.part) foram mantidos e serão retomados na próxima execução.[/]")
        return EXIT_INTERRUPTED
    return EXIT_PARTIAL if failed else EXIT_OK


def main(argv: Optional[list[str]] = None) -> int:
    args = DownloadConfig(**vars(parse_args(argv)))
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
    except Exception as exc:  # final safety net
        console.print(Panel(f"[red]{escape(type(exc).__name__)}: {escape(str(exc))}[/]",
                            title="Erro inesperado", border_style="red"))
        return EXIT_PARTIAL

