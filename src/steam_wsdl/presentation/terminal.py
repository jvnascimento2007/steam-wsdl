from __future__ import annotations

from rich.console import Console, Group
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.progress import (Progress, SpinnerColumn, TextColumn, BarColumn, DownloadColumn, TransferSpeedColumn, TimeRemainingColumn, MofNCompleteColumn, TimeElapsedColumn)
from rich.table import Column, Table
from rich.text import Text
from ..constants import __version__
from ..models import DownloadJob
from ..formatting import human_size, truncate

def report_line(item: DownloadJob, console: Console) -> None:
    name = escape(truncate(item.item.display, 60))
    if item.result.status == "ok":
        console.print(f"[green]✓[/] {name} [dim]({human_size(item.result.bytes)} · {item.result.method})[/]")
    elif item.result.status == "skipped":
        console.print(f"[yellow]↷[/] {name} [dim]({item.result.message})[/]")
    elif item.result.status == "cancelled":
        console.print(f"[dim]■ {name} (cancelled)[/]")
    else:
        console.print(f"[red]✗[/] {name} [red]— {escape(item.result.message)}[/]")


def print_banner(console: Console) -> None:
    title = Text("steam-wsdl", style="bold cyan", justify="center")
    title.append(f"\nv{__version__}", style="dim")
    console.print(Panel(title, border_style="cyan", padding=(0, 4)))


def print_summary(items: list[DownloadJob], elapsed: float, console: Console) -> None:
    table = Table(title="Summary", title_style="bold", header_style="bold cyan", expand=False)
    table.add_column("ID", style="dim", no_wrap=True)
    table.add_column("Item", overflow="ellipsis", max_width=40, no_wrap=True)
    table.add_column("Method", no_wrap=True)
    table.add_column("Size", justify="right", no_wrap=True)
    table.add_column("Status", overflow="fold")

    styles = {"ok": "[green]✓ downloaded[/]", "skipped": "[yellow]↷ skipped[/]",
              "cancelled": "[dim]■ cancelled[/]", "failed": "[red]✗ failed[/]"}
    for it in items:
        status = styles.get(it.result.status, "[dim]■ cancelled[/]")
        if it.result.message and it.result.status in ("failed", "skipped"):
            status += f" [dim]{escape(it.result.message)}[/]"
        table.add_row(it.item.id, escape(it.item.display), it.result.method, human_size(it.result.bytes) if it.result.bytes else "-", status)
    console.print(table)

    ok = sum(i.result.status == "ok" for i in items)
    skipped = sum(i.result.status == "skipped" for i in items)
    failed = sum(i.result.status == "failed" for i in items)
    cancelled = len(items) - ok - skipped - failed
    total_bytes = sum(i.result.bytes for i in items if i.result.status == "ok")
    line = Text()
    line.append(f"{ok} downloaded", style="green")
    line.append(f" · {skipped} skipped", style="yellow")
    line.append(f" · {failed} failed", style="red" if failed else "dim")
    if cancelled:
        line.append(f" · {cancelled} cancelled", style="dim")
    line.append(f" · {human_size(total_bytes)} in {elapsed:.1f}s", style="bold")
    console.print(Panel(line, border_style="green" if not failed and not cancelled else "yellow"))


class TerminalObserver:
    """Translate plain progress notifications into Rich terminal output."""

    def __init__(self, console: Console):
        self.console = console
        self.files = Progress(
            SpinnerColumn(),
            TextColumn('{task.description}', table_column=Column(width=40, no_wrap=True)),
            BarColumn(bar_width=None), DownloadColumn(), TransferSpeedColumn(),
            TimeRemainingColumn(), console=console, expand=True,
        )
        self.overall = Progress(
            TextColumn('[bold]Overall progress', table_column=Column(width=40)),
            BarColumn(bar_width=None), MofNCompleteColumn(), TimeElapsedColumn(),
            console=console, expand=True,
        )
        self.overall_task = None
        self.live = Live(Group(self.files, self.overall), console=console, refresh_per_second=12)

    def start(self, total: int) -> None:
        self.overall_task = self.overall.add_task('overall', total=total)

    def __enter__(self):
        self.live.__enter__()
        return self

    def __exit__(self, *args):
        return self.live.__exit__(*args)

    def add_task(self, description: str, *, total=None, start=True) -> int:
        return self.files.add_task(escape(description), total=total, start=start)

    def update(self, task: int, *, total=None, completed=None, advance=None, description=None) -> None:
        kwargs = dict(total=total, completed=completed, advance=advance)
        if description is not None:
            kwargs['description'] = escape(description)
        self.files.update(task, **kwargs)

    def start_task(self, task: int) -> None:
        self.files.start_task(task)

    def remove_task(self, task: int) -> None:
        self.files.remove_task(task)

    def message(self, text: str) -> None:
        self.console.print(escape(text))

    def completed(self, job: DownloadJob) -> None:
        if self.overall_task is not None:
            self.overall.advance(self.overall_task)
        report_line(job, self.console)
