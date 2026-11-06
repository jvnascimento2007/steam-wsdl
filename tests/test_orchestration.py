"""Method selection, overwrite, fallback and concurrent scheduling."""
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
                self.assertEqual((j.result.status, j.result.method, j.result.bytes), ('ok', 'direto', 3))
                self.assertEqual(target.read_bytes(), b'new')
                self.assertEqual(observer.results, [j])

    def test_rejected_items_report_specific_errors(self):
        cases = [(WorkshopItem('123', api_result=9), 'não encontrado'),
                 (WorkshopItem('123', api_result=999), '999'),
                 (WorkshopItem('123', file_type=2), 'aninhadas'),
                 (WorkshopItem('123'), 'App ID do jogo desconhecido')]
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
                    self.assertIn('http failed' if direct else 'instale o steamcmd', j.result.message)
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
            for j in batch:
                j.result.status, j.result.method = 'ok', 'steamcmd'
                ctx.on_complete(j)
        with tempfile.TemporaryDirectory() as root, patch.object(orchestrator, 'steamcmd_session', side_effect=session):
            orchestrator.execute_downloads(jobs, DownloadConfig(steamcmd_sessions=2), Path(root), threading.Event(), observer, 'steamcmd')
        self.assertEqual(len(thread_ids), 2)
        self.assertCountEqual(observer.results, jobs)
        self.assertTrue(all(j.result.status == 'ok' for j in jobs))
