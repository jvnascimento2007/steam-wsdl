import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from rich.console import Console
from steam_wsdl import workshop, orchestrator
from steam_wsdl.backends import http, steamcmd
from steam_wsdl.config import DownloadConfig
from steam_wsdl.models import WorkshopItem, DownloadJob
from steam_wsdl.runtime import Context
from steam_wsdl.errors import Cancelled, DownloadError, NeedSteamcmd
from steam_wsdl.presentation.terminal import TerminalObserver


def job(pid, **kwargs):
    return DownloadJob(WorkshopItem(pid, **kwargs))


class BehaviorTests(unittest.TestCase):
    def config(self, **kwargs):
        return DownloadConfig(retries=0, **kwargs)

    def context(self, root, **kwargs):
        observer = TerminalObserver(Console(file=io.StringIO()))
        return Context(self.config(), Path(root), threading.Event(), observer,
                       kwargs.get('steamcmd'), lambda item: orchestrator.complete_item(item, observer))

    def test_collection_expansion_preserves_order_and_deduplicates(self):
        details = {'100': {'result': 1, 'file_type': 2},
                   '200': {'result': 1, 'title': 'explicit'}}
        children = {'300': {'result': 1, 'title': 'child'}}
        with patch.object(workshop, 'fetch_details', side_effect=[details, children]), \
             patch.object(workshop, 'fetch_collection_children', return_value={'100': ['300', '200', '300']}):
            items = workshop.resolve_items(['100', '200'], self.config(), threading.Event(), TerminalObserver(Console(file=io.StringIO())))
        self.assertEqual([i.item.id for i in items], ['300', '200'])
        self.assertEqual(items[1].item.title, 'explicit')

    def test_http_resume_and_atomic_completion(self):
        class Response:
            status_code = 206
            headers = {'Content-Length': '3'}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def raise_for_status(self): pass
            def iter_content(self, size): yield b'def'
        with tempfile.TemporaryDirectory() as root:
            final = Path(root) / 'mod.bin'
            part = Path(root) / 'mod.bin.part'
            part.write_bytes(b'abc')
            ctx = self.context(root)
            task = ctx.progress.add_task('test')
            with patch.object(http, 'get_session') as session:
                session.return_value.get.return_value = Response()
                size = http.download_direct(job('123', file_url='http://example.test', size=6), final, ctx.config, ctx.stop, ctx.progress, task)
                self.assertEqual(session.return_value.get.call_args.kwargs['headers'], {'Range': 'bytes=3-'})
            self.assertEqual(size, 6)
            self.assertEqual(final.read_bytes(), b'abcdef')
            self.assertFalse(part.exists())

    def test_direct_failure_defers_to_steamcmd(self):
        with tempfile.TemporaryDirectory() as root:
            ctx = self.context(root, steamcmd='steamcmd')
            item = job('123', app_id='456', file_url='http://example.test')
            with patch.object(orchestrator, 'download_direct', side_effect=DownloadError('network')):
                with self.assertRaises(NeedSteamcmd):
                    orchestrator._run_item(item, ctx, 0)
            self.assertEqual(item.result.direct_error, 'network')

    def test_cancellation_preserves_partial_file(self):
        with tempfile.TemporaryDirectory() as root:
            ctx = self.context(root)
            ctx.stop.set()
            final = Path(root) / 'mod'
            part = Path(root) / 'mod.part'
            part.write_bytes(b'partial')
            with self.assertRaises(Cancelled):
                http.download_direct(job('123'), final, ctx.config, ctx.stop, ctx.progress, 0)
            self.assertEqual(part.read_bytes(), b'partial')
            self.assertFalse(final.exists())

    def test_steamcmd_output_and_balancing(self):
        self.assertEqual(steamcmd.RE_STEAMCMD_OK.search('Success. Downloaded item 123').group(1), '123')
        self.assertEqual(steamcmd.RE_STEAMCMD_FAIL.search('ERROR! Download item 123 failed (Failure)').groups(), ('123', 'Failure'))
        self.assertIn('--steam-user', steamcmd.parse_steamcmd_error('ERROR! No subscription'))
        items = [job(str(i), size=size) for i, size in enumerate([100, 50, 10, 1])]
        buckets = orchestrator.balance_items(items, 2)
        self.assertEqual(sorted(i.item.id for b in buckets for i in b), ['0', '1', '2', '3'])
        for bucket in buckets:
            self.assertEqual([i.item.id for i in bucket], sorted(i.item.id for i in bucket))


if __name__ == '__main__':
    unittest.main()
