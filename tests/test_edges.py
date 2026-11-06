import io
import tempfile
import threading
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from steam_wsdl.backends import http, steam_api, steamcmd
from steam_wsdl.config import DownloadConfig
from steam_wsdl.errors import Cancelled
from steam_wsdl.inputs import collect_ids
from steam_wsdl.models import DownloadJob, WorkshopItem
from steam_wsdl.runtime import Context
from steam_wsdl.workshop import resolve_items
from test_integration import RecordingObserver


class Response:
    def __init__(self, code, data=b'fresh'):
        self.status_code = code
        self.data = data
        self.headers = {'Content-Length': str(len(data))}
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def raise_for_status(self): pass
    def iter_content(self, size): yield self.data


class EdgeTests(unittest.TestCase):
    def test_invalid_range_retries_without_partial_content(self):
        with tempfile.TemporaryDirectory() as root, patch.object(http, 'get_session') as session:
            final = Path(root) / 'mod'
            part = final.with_name('mod.part')
            part.write_bytes(b'invalid')
            observer = RecordingObserver()
            task = observer.add_task('mod')
            session.return_value.get.side_effect = [Response(416), Response(200)]
            stop = threading.Event()
            with patch.object(stop, 'wait', return_value=False):
                http.download_direct(DownloadJob(WorkshopItem('123', file_url='http://example.test')),
                                     final, DownloadConfig(retries=1), stop, observer, task)
            self.assertEqual(final.read_bytes(), b'fresh')
            self.assertEqual(session.return_value.get.call_args.kwargs['headers'], {})

    def test_cancellation_mid_stream_keeps_received_bytes(self):
        stop = threading.Event()
        class InterruptedResponse(Response):
            def iter_content(self, size):
                yield b'first'
                stop.set()
                yield b'second'
        with tempfile.TemporaryDirectory() as root, patch.object(http, 'get_session') as session:
            final = Path(root) / 'mod'
            observer = RecordingObserver()
            task = observer.add_task('mod')
            session.return_value.get.return_value = InterruptedResponse(200)
            with self.assertRaises(Cancelled):
                http.download_direct(DownloadJob(WorkshopItem('123')), final, DownloadConfig(), stop, observer, task)
            self.assertEqual(final.with_name('mod.part').read_bytes(), b'first')
            self.assertFalse(final.exists())

    def test_api_retry_and_cancellation_during_backoff(self):
        stop = threading.Event()
        with patch.object(steam_api, 'get_session') as session:
            response = session.return_value.post.return_value
            response.status_code = 503
            with patch.object(stop, 'wait', return_value=True):
                with self.assertRaises(Cancelled):
                    steam_api.api_post('http://example.test', {}, DownloadConfig(), stop)
            self.assertEqual(session.return_value.post.call_count, 1)

    def test_unreadable_collection_remains_an_explicit_failure(self):
        with patch('steam_wsdl.workshop.fetch_details', return_value={'123': {'result': 1, 'file_type': 2}}), \
             patch('steam_wsdl.workshop.fetch_collection_children', return_value={}):
            jobs = resolve_items(['123'], DownloadConfig(), threading.Event(), RecordingObserver())
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].result.status, 'failed')
        self.assertEqual(jobs[0].item.id, '123')
        with self.assertRaises(FrozenInstanceError): jobs[0].item.id = '456'

    def test_links_file_bom_comments_and_order(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / 'links.txt'
            source.write_text('\ufeff# comment\n123 annotation\n; comment\ninvalid\n456\n123\n', encoding='utf-8')
            ids, problems = collect_ids(DownloadConfig(url=['456'], file=str(source)))
            self.assertEqual(ids, ['456', '123'])
            self.assertEqual(len(problems), 1)
            self.assertIn('linha 4', problems[0])

    def test_steamcmd_timeout_kills_and_reaps_process(self):
        observer = RecordingObserver()
        j = DownloadJob(WorkshopItem('123', app_id='456'))
        with tempfile.TemporaryDirectory() as root, patch.object(steamcmd.subprocess, 'Popen') as popen:
            proc = popen.return_value
            # No EOF until the process is killed, matching a stalled subprocess.
            killed = threading.Event()
            class Output:
                def __iter__(self):
                    killed.wait(5)
                    return iter(())
                def close(self): pass
            proc.stdout = Output()
            proc.poll.return_value = None
            proc.kill.side_effect = killed.set
            ctx = Context(DownloadConfig(steamcmd_timeout=0), Path(root), threading.Event(),
                          observer, 'steamcmd', observer.completed)
            steamcmd._steamcmd_batch([j], ctx, 1)
            proc.kill.assert_called()
            proc.wait.assert_called()
        self.assertEqual(j.result.status, 'failed')
        self.assertIn('sem atividade', j.result.message)
        self.assertEqual(observer.results, [j])
        self.assertFalse(observer.tasks)


if __name__ == '__main__': unittest.main()
