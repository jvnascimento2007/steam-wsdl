"""CLI argument boundaries, error reporting and plain-text terminal rendering."""
import argparse
import io
import runpy
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import steam_wsdl
from steam_wsdl import cli
from steam_wsdl.config import DownloadConfig
from steam_wsdl.errors import Cancelled, UsageError
from steam_wsdl.presentation.terminal import TerminalObserver, print_banner, print_summary, report_line
from rich.console import Console
from test_integration import job


class CliTerminalTests(unittest.TestCase):
    def test_numeric_argument_boundaries(self):
        for option, low, high in [('-w', 1, 32), ('-r', 0, 20), ('-t', 5, 600),
                                  ('--steamcmd-sessions', 1, 16), ('--steamcmd-timeout', 30, 7200)]:
            for value in (low, high):
                with self.subTest(option=option, value=value):
                    cli.parse_args(['-u', '123', option, str(value)])
            for value in (low - 1, high + 1, 'invalid'):
                with self.subTest(option=option, value=value), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        cli.parse_args(['-u', '123', option, str(value)])
                    self.assertEqual(error.exception.code, 2)
        with self.assertRaises(argparse.ArgumentTypeError): cli.bounded_int(1, 3)('oops')
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            cli.parse_args([])
        self.assertEqual(error.exception.code, 2)

    def test_help_version_and_module_entry_point(self):
        for flag in ('--help', '--version'):
            with redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as error:
                cli.parse_args([flag])
            self.assertEqual(error.exception.code, 0)
            self.assertIn('steam_workshop_dl', output.getvalue())
        with patch.object(cli, 'main', return_value=7) as main:
            self.assertEqual(steam_wsdl.main(['-u', '123']), 7)
            main.assert_called_once_with(['-u', '123'])
        with patch.object(cli, 'main', return_value=2), self.assertRaises(SystemExit) as error:
            runpy.run_module('steam_wsdl', run_name='__main__')
        self.assertEqual(error.exception.code, 2)

    def test_warns_about_more_than_ten_invalid_inputs(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as root, patch.object(cli, 'resolve_items', return_value=[]):
            code = cli.run(DownloadConfig(url=['invalid'] * 12 + ['123'], output=root, no_steamcmd=True), Console(file=output))
        self.assertEqual(code, 0)
        self.assertIn('mais 2 problemas', output.getvalue())

    def test_force_steamcmd_requires_executable(self):
        with tempfile.TemporaryDirectory() as root, patch.object(cli, 'find_steamcmd', return_value=None):
            with self.assertRaisesRegex(UsageError, 'exige o steamcmd'):
                cli.run(DownloadConfig(url=['123'], output=root, force_steamcmd=True), Console(file=io.StringIO()))

    def test_resolution_cancellation_returns_130(self):
        with tempfile.TemporaryDirectory() as root, patch.object(cli, 'resolve_items', side_effect=Cancelled):
            self.assertEqual(cli.run(DownloadConfig(url=['123'], output=root, no_steamcmd=True), Console(file=io.StringIO())), 130)

    def test_failed_links_write_error_does_not_hide_download_failure(self):
        j = job()
        j.result.status, j.result.message = 'failed', 'download error'
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as root, patch.object(cli, 'resolve_items', return_value=[j]), \
             patch.object(cli, 'execute_downloads'), patch.object(Path, 'write_text', side_effect=OSError('disk full')), \
             patch.object(cli, 'prepare_output', return_value=Path(root)):
            code = cli.run(DownloadConfig(url=['123'], output=root, failed_file='failed.txt', no_steamcmd=True), Console(file=output))
        self.assertEqual(code, 1)
        self.assertIn('não foi possível gravar', output.getvalue())

    def test_main_handles_interrupt_and_unexpected_error(self):
        for error, expected in [(KeyboardInterrupt(), 130), (RuntimeError('boom'), 1)]:
            with self.subTest(error=error), patch.object(cli, 'Console', return_value=Console(file=io.StringIO())), \
                 patch.object(cli, 'run', side_effect=error):
                self.assertEqual(cli.main(['-u', '123']), expected)

    def test_terminal_reports_all_statuses_and_summary_totals(self):
        output = io.StringIO()
        console = Console(file=output, width=180, color_system=None)
        jobs = []
        for index, status in enumerate(('ok', 'skipped', 'failed', 'cancelled')):
            j = job(str(index), title='[bold]literal[/bold]')
            j.result.status, j.result.message = status, 'reason'
            j.result.bytes = 1024 if status == 'ok' else 0
            report_line(j, console)
            jobs.append(j)
        print_banner(console)
        print_summary(jobs, 2.0, console)
        text = output.getvalue()
        for expected in ('[bold]literal[/bold]', '1 baixados', '1 ignorados', '1 falhas', '1 cancelados', '1.0 KB', '2.0s'):
            self.assertIn(expected, text)

    def test_observer_escapes_markup_and_tracks_progress(self):
        output = io.StringIO()
        observer = TerminalObserver(Console(file=output, width=160))
        observer.completed(job())  # Completion is also valid before the overall task is created.
        observer.start(1)
        task = observer.add_task('[bold]literal', total=10, start=False)
        observer.start_task(task)
        observer.update(task, completed=3, description='[red]literal')
        observer.update(task, advance=2)
        self.assertEqual(observer.files.tasks[0].completed, 5)
        self.assertIn('\\[red]', observer.files.tasks[0].description)
        observer.message('[green]literal')
        j = job()
        j.result.status = 'ok'
        observer.completed(j)
        self.assertEqual(observer.overall.tasks[0].completed, 1)
        observer.remove_task(task)
        self.assertEqual(observer.files.tasks, [])
        self.assertIn('[green]literal', output.getvalue())
