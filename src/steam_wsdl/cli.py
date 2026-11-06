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
            raise argparse.ArgumentTypeError(f"'{value}' is not an integer")
        if not lo <= number <= hi:
            raise argparse.ArgumentTypeError(f"must be between {lo} and {hi}")
        return number
    return convert


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="steam-wsdl",
        description="Download Steam Workshop mods concurrently.",
        epilog=(
            "examples:\n"
            "  steam-wsdl -u \"https://steamcommunity.com/sharedfiles/filedetails/?id=123456789\"\n"
            "  steam-wsdl -f mods.txt -o ./my_mods -w 8\n"
            "  steam-wsdl -f mods.txt -u 987654321 --failed-file failed.txt\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = p.add_argument_group("input (at least one required)")
    src.add_argument("-u", "--url", action="append", default=[], metavar="URL",
                     help="Workshop item URL or ID; may be repeated")
    src.add_argument("-f", "--file", metavar="FILE.txt",
                     help="text file with one URL per line (lines starting with # or ; are comments)")

    p.add_argument("-o", "--output", default="workshop_downloads", metavar="DIR",
                   help="output directory (default: %(default)s)")
    p.add_argument("-w", "--workers", type=bounded_int(1, 32), default=4, metavar="N",
                   help="concurrent downloads, 1-32 (default: %(default)s)")
    p.add_argument("-r", "--retries", type=bounded_int(0, 20), default=3, metavar="N",
                   help="extra retries after network failures or SteamCMD session crashes/timeouts (default: %(default)s)")
    p.add_argument("-t", "--timeout", type=bounded_int(5, 600), default=30, metavar="SEC",
                   help="network timeout in seconds (default: %(default)s)")
    p.add_argument("--overwrite", action="store_true", help="overwrite previously downloaded items")
    p.add_argument("--failed-file", metavar="FILE.txt",
                   help="save failed URLs to this file for retrying later")

    sc = p.add_argument_group("steamcmd (fallback for items without a direct download)")
    sc.add_argument("--steamcmd", metavar="PATH", help="path to the steamcmd executable")
    sc.add_argument("--no-steamcmd", action="store_true", help="never use steamcmd")
    sc.add_argument("--force-steamcmd", action="store_true", help="always use steamcmd")
    sc.add_argument("--steam-user", metavar="USERNAME",
                    help="Steam account with a saved SteamCMD login (default: anonymous)")
    sc.add_argument("--steamcmd-sessions", type=bounded_int(1, 16), default=None, metavar="N",
                    help="maximum concurrent persistent steamcmd sessions; each logs in once and downloads "
                         "multiple items sequentially (default: same as -w; 1 with --steam-user)")
    sc.add_argument("--steamcmd-timeout", type=bounded_int(30, 7200), default=900, metavar="SEC",
                    help="stop a steamcmd session after SEC seconds of inactivity (default: %(default)s)")
    p.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")

    args = p.parse_args(argv)
    if not args.url and not args.file:
        p.error("provide at least one URL with -u/--url or a file with -f/--file")
    if args.no_steamcmd and args.force_steamcmd:
        p.error("--no-steamcmd and --force-steamcmd are mutually exclusive")
    return args


def run(args: DownloadConfig, console: Console) -> int:
    stop = threading.Event()

    ids, problems = collect_ids(args)
    for msg in problems[:10]:
        console.print(f"[yellow]⚠[/] {escape(msg)}")
    if len(problems) > 10:
        console.print(f"[yellow]⚠[/] … and {len(problems) - 10} problems")
    if not ids:
        raise UsageError("no valid Steam Workshop URLs found")

    out_dir = prepare_output(args.output)

    steamcmd = None if args.no_steamcmd else find_steamcmd(args.steamcmd)
    if args.force_steamcmd and not steamcmd:
        raise UsageError("--force-steamcmd requires steamcmd to be installed (or --steamcmd PATH)")

    console.print(f"[bold]Output:[/] {escape(str(out_dir))}   [bold]Workers:[/] {args.workers}   "
                  f"[bold]steamcmd:[/] {'yes' if steamcmd else 'no'}")

    observer = TerminalObserver(console)
    try:
        with console.status("[cyan]Querying the Steam API...[/]"):
            items = resolve_items(ids, args, stop, observer)
    except Cancelled:
        console.print("[yellow]Interrupted.[/]")
        return EXIT_INTERRUPTED
    except WorkshopError as exc:
        console.print(Panel(f"[red]{escape(str(exc))}[/]", title="Error querying the Workshop", border_style="red"))
        return EXIT_PARTIAL

    console.print(f"[bold]{len(items)}[/] items queued\n")

    observer.start(len(items))

    interrupted = False
    started = time.monotonic()
    with observer:
        try:
            execute_downloads(items, args, out_dir, stop, observer, steamcmd)
        except KeyboardInterrupt:
            interrupted = True
            stop.set()
            console.print("[yellow]Interrupted by user — stopping active downloads...[/]")

    elapsed = time.monotonic() - started
    console.print()
    print_summary(items, elapsed, console)

    failed = [i for i in items if i.result.status == "failed"]
    if args.failed_file and failed:
        try:
            Path(args.failed_file).expanduser().write_text(
                "\n".join(i.item.url for i in failed) + "\n", encoding="utf-8")
            console.print(f"[dim]Failed URLs saved to {escape(args.failed_file)}[/]")
        except OSError as exc:
            console.print(f"[yellow]⚠ could not write {escape(args.failed_file)}: {escape(str(exc))}[/]")
    if interrupted:
        console.print("[dim]Partial downloads (.part) were preserved and will resume on the next run.[/]")
        return EXIT_INTERRUPTED
    return EXIT_PARTIAL if failed else EXIT_OK


def main(argv: Optional[list[str]] = None) -> int:
    args = DownloadConfig(**vars(parse_args(argv)))
    console = Console()
    print_banner(console)
    try:
        return run(args, console)
    except UsageError as exc:
        console.print(Panel(f"[red]{escape(str(exc))}[/]", title="Error", border_style="red"))
        return EXIT_USAGE
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/]")
        return EXIT_INTERRUPTED
    except Exception as exc:  # final safety net
        console.print(Panel(f"[red]{escape(type(exc).__name__)}: {escape(str(exc))}[/]",
                            title="Unexpected error", border_style="red"))
        return EXIT_PARTIAL

