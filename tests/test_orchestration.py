"""Method selection, overwrite, fallback and concurrent scheduling."""
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from steam_wsdl import orchestrator
from steam_wsdl.backends import http
from steam_wsdl.config import DownloadConfig
from steam_wsdl.errors import DownloadError
from steam_wsdl.models import DownloadJob, WorkshopItem
from steam_wsdl.runtime import Context
from test_edges import Response
from test_integration import RecordingObserver, job


class OrchestrationTests(unittest.TestCase):
    def test_direct_success_and_overwrite(self):
        for overwrite in (False, True):
            with self.subTest(overwrite=overwrite), tempfile.TemporaryDirectory() as root:
                target = Path(root) / '456/Mod [123].bin'
                if overwrite:
                    target.parent.mkdir()
                    target.write_bytes(b'old')
                j = job(title='Mod', filename='x.bin', file_url='http://example.test')
                observer = RecordingObserver()
                with patch.object(http, 'get_session') as session:
                    session.return_value.get.return_value = Response(200, b'new')
                    orchestrator.execute_downloads([j], DownloadConfig(overwrite=overwrite), Path(root), threading.Event(), observer, None)
                self.assertEqual((j.result.status, j.result.method, j.result.bytes), ('ok', 'direct', 3))
                self.assertEqual(target.read_bytes(), b'new')
                self.assertEqual(observer.results, [j])

    def test_rejected_items_report_specific_errors(self):
        cases = [(WorkshopItem('123', api_result=9), 'not found'),
                 (WorkshopItem('123', api_result=999), '999'),
                 (WorkshopItem('123', file_type=2), 'nested'),
                 (WorkshopItem('123'), 'unknown game App ID')]
        with tempfile.TemporaryDirectory() as root:
            for item, message in cases:
                with self.subTest(item=item):
                    j = DownloadJob(item)
                    observer = RecordingObserver()
                    orchestrator.execute_downloads([j], DownloadConfig(), Path(root), threading.Event(), observer, 'steamcmd')
                    self.assertEqual(j.result.status, 'failed')
                    self.assertIn(message, j.result.message)
                    self.assertEqual(observer.results, [j])

    def test_resolution_failure_is_preserved(self):
        j = job()
        j.result.status, j.result.message = 'failed', 'collection unreadable'
        with tempfile.TemporaryDirectory() as root:
            observer = RecordingObserver()
            orchestrator.execute_downloads([j], DownloadConfig(), Path(root), threading.Event(), observer, None)
        self.assertEqual(j.result.message, 'collection unreadable')
        self.assertEqual(observer.results, [j])

    def test_no_backend_and_direct_failure_without_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            for direct in (False, True):
                with self.subTest(direct=direct), patch.object(orchestrator, 'download_direct', side_effect=DownloadError('http failed')):
                    j = job(file_url='url' if direct else '')
                    observer = RecordingObserver()
                    orchestrator.execute_downloads([j], DownloadConfig(), Path(root), threading.Event(), observer, None)
                    self.assertEqual(j.result.status, 'failed')
                    self.assertIn('http failed' if direct else 'install steamcmd', j.result.message)
                    self.assertEqual(observer.results, [j])

    def test_force_steamcmd_and_existing_directory_skip(self):
        with tempfile.TemporaryDirectory() as root, patch.object(orchestrator, 'download_direct') as direct:
            j = job(file_url='url')
            target = Path(root) / '456/item [123]'
            target.mkdir(parents=True)
            observer = RecordingObserver()
            orchestrator.execute_downloads([j], DownloadConfig(force_steamcmd=True), Path(root), threading.Event(), observer, 'steamcmd')
            direct.assert_not_called()
            self.assertEqual((j.result.status, j.result.method, j.result.path), ('skipped', 'steamcmd', target))

    def test_worker_exception_is_reported_and_progress_removed(self):
        with tempfile.TemporaryDirectory() as root, patch.object(orchestrator, 'download_direct', side_effect=RuntimeError('boom')):
            j = job(file_url='url')
            observer = RecordingObserver()
            orchestrator.execute_downloads([j], DownloadConfig(), Path(root), threading.Event(), observer, None)
        self.assertEqual(j.result.status, 'failed')
        self.assertIn('RuntimeError', j.result.message)
        self.assertEqual(observer.results, [j])
        self.assertFalse(observer.tasks)

    def test_cancelled_before_start_and_empty_stages(self):
        stop = threading.Event()
        stop.set()
        observer = RecordingObserver()
        with tempfile.TemporaryDirectory() as root:
            j = job()
            orchestrator.execute_downloads([j], DownloadConfig(), Path(root), stop, observer, None)
            self.assertEqual(j.result.status, 'cancelled')
            ctx = Context(DownloadConfig(), Path(root), stop, observer, None, observer.completed)
            orchestrator.run_steamcmd_stage([], ctx)
            orchestrator.execute_downloads([], DownloadConfig(), Path(root), threading.Event(), observer, None)
        self.assertEqual(observer.results, [j])

    def test_cancel_between_stages_finalizes_deferred_item(self):
        stop = threading.Event()
        observer = RecordingObserver()
        j = job(file_url='url')
        def fail(*args):
            stop.set()
            raise DownloadError('offline')
        with tempfile.TemporaryDirectory() as root, patch.object(orchestrator, 'download_direct', side_effect=fail):
            orchestrator.execute_downloads([j], DownloadConfig(), Path(root), stop, observer, 'steamcmd')
        self.assertEqual(j.result.status, 'cancelled')
        self.assertEqual(observer.results, [j])

    def test_parallel_sessions_run_simultaneously(self):
        barrier = threading.Barrier(2, timeout=5)
        observer = RecordingObserver()
        jobs = [job('123'), job('789')]
        thread_ids = set()
        lock = threading.Lock()
        def session(batch, ctx, number, delay):
            with lock: thread_ids.add(threading.get_ident())
            barrier.wait()
            while True:
                try: j = batch.get_nowait()
                except queue.Empty: break
                j.result.status, j.result.method = 'ok', 'steamcmd'
                ctx.on_complete(j)
        with tempfile.TemporaryDirectory() as root, patch.object(orchestrator, 'steamcmd_session', side_effect=session):
            orchestrator.execute_downloads(jobs, DownloadConfig(steamcmd_sessions=2), Path(root), threading.Event(), observer, 'steamcmd')
        self.assertEqual(len(thread_ids), 2)
        self.assertCountEqual(observer.results, jobs)
        self.assertTrue(all(j.result.status == 'ok' for j in jobs))

    def test_faster_persistent_worker_claims_next_item_from_shared_queue(self):
        from steamcmd_fixture import executable, records
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            body = """marker = trace.parent / 'released'
if pid == '123':
    deadline = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(.02)
    assert marker.exists(), 'another worker did not claim the third item'
if pid == '456': marker.touch()
"""
            fake = executable(root, body)
            observer = RecordingObserver()
            jobs = [job('123'), job('789'), job('456')]
            with patch.object(orchestrator, 'STEAMCMD_START_STAGGER', 0):
                orchestrator.execute_downloads(jobs, DownloadConfig(steamcmd_sessions=2, retries=0, steamcmd_timeout=6),
                                               root/'out', threading.Event(), observer, str(fake))
            self.assertTrue(all(j.result.status == 'ok' for j in jobs))
            self.assertCountEqual(observer.results, jobs)
            self.assertEqual(len(observer.results), 3)
            trace = records(root)
            self.assertEqual(sum(r['event'] == 'start' for r in trace), 2)
            owners = {r['item']: r['process'] for r in trace if r['event'] == 'download'}
            self.assertEqual(owners['789'], owners['456'])
            self.assertNotEqual(owners['123'], owners['456'])
            self.assertFalse(observer.tasks)
