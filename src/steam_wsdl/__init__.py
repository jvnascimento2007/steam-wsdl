"""Steam Workshop downloader."""

from .constants import __version__


def main(argv: list[str] | None = None) -> int:
    """Run the command line interface."""
    from .cli import main as cli_main
    return cli_main(argv)
