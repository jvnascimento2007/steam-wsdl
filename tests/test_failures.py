"""Offline coverage of failure paths and cancellation with active workers."""
import io
import sys
import tempfile
import threading
import unittest
from collections import Counter
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from rich.console import Console

from steam_wsdl import cli, orchestrator
from steam_wsdl.backends import http, steam_api, steamcmd
from steam_wsdl.config import DownloadConfig
from steam_wsdl.errors import Cancelled, DownloadError, WorkshopError
from steam_wsdl.runtime import Context
from test_edges import Response
from test_integration import RecordingObserver, job


class HttpFailureTests(unittest.TestCase):
    def test_exhausted_retries_leave_destination_untouched(self):
        with tempfile.TemporaryDirectory() as root, patch.object(http, 'get_session') as session:
            final = Path(root) / 'mod'
            final.write_bytes(b'existing')
            observer = RecordingObserver()
            task = observer.add_task('mod')
            stop = threading.Event()
            session.return_value.get.side_effect = requests.Timeout('offline')
            with patch.object(stop, 'wait', return_value=False) as wait:
                with self.assertRaisesRegex(DownloadError, '3 attempts'):
                    http.download_direct(job(), final, DownloadConfig(retries=2), stop, observer, task)
            self.assertEqual(session.return_value.get.call_count, 3)
            self.assertEqual([call.args[0] for call in wait.call_args_list], [1, 2])
            self.assertEqual(final.read_bytes(), b'existing')

    def test_incomplete_response_preserves_partial_without_publishing(self):
        with tempfile.TemporaryDirectory() as root, patch.object(http, 'get_session') as session:
            response = Response(200, b'abc')
            response.headers['Content-Length'] = '6'
            session.return_value.get.return_value = response
            final = Path(root) / 'mod'
            observer = RecordingObserver()
            task = observer.add_task('mod')
            with self.assertRaisesRegex(DownloadError, 'incomplete download'):
                http.download_direct(job(), final, DownloadConfig(retries=0), threading.Event(), observer, task)
            self.assertFalse(final.exists())
            self.assertEqual(final.with_name('mod.part').read_bytes(), b'abc')

    def test_disk_write_failure_is_not_retried(self):
        with tempfile.TemporaryDirectory() as root, patch.object(http, 'get_session') as session:
            final = Path(root) / 'mod'
            final.write_bytes(b'existing')
            session.return_value.get.return_value = Response(200)
            observer = RecordingObserver()
            task = observer.add_task('mod')
            writer = Mock()
            writer.__enter__ = Mock(return_value=writer)
            writer.__exit__ = Mock(return_value=False)
            writer.write.side_effect = OSError('disk full')
            with patch('builtins.open', return_value=writer):
                with self.assertRaisesRegex(DownloadError, 'disk error: disk full'):
                    http.download_direct(job(), final, DownloadConfig(), threading.Event(), observer, task)
            self.assertEqual(session.return_value.get.call_count, 1)
            self.assertEqual(final.read_bytes(), b'existing')


class ApiFailureTests(unittest.TestCase):
    def test_invalid_json_is_reported_without_retry(self):
        with patch.object(steam_api, 'get_session') as session:
            response = session.return_value.post.return_value
            response.status_code = 200
            response.json.side_effect = ValueError('invalid JSON')
            with self.assertRaisesRegex(WorkshopError, 'JSON'):
                steam_api.api_post('http://example.test', {}, DownloadConfig(), threading.Event())
            self.assertEqual(session.return_value.post.call_count, 1)

    def test_definitive_http_errors_are_not_retried(self):
        for status in (400, 401, 403, 404):
            with self.subTest(status=status), patch.object(steam_api, 'get_session') as session:
                response = requests.Response()
                response.status_code = status
                session.return_value.post.return_value = response
                with self.assertRaisesRegex(WorkshopError, f'HTTP {status}'):
                    steam_api.api_post('http://example.test', {}, DownloadConfig(), threading.Event())
                self.assertEqual(session.return_value.post.call_count, 1)

    def test_more_than_100_ids_are_batched_and_all_results_returned(self):
        ids = [str(i) for i in range(1000, 1205)]
        def api_post(url, data, config, stop):
            batch = [data[f'publishedfileids[{i}]'] for i in range(data['itemcount'])]
            return {'response': {'publishedfiledetails': [
                {'publishedfileid': pid, 'result': 1, 'title': f'Mod {pid}'} for pid in batch
            ]}}
        with patch.object(steam_api, 'api_post', side_effect=api_post) as post:
            details = steam_api.fetch_details(ids, DownloadConfig(), threading.Event())
        self.assertEqual(list(details), ids)
        self.assertEqual([c.args[1]['itemcount'] for c in post.call_args_list], [100, 100, 5])
        for pid in ids:
            self.assertEqual(details[pid]['title'], f'Mod {pid}')


class SteamcmdFailureTests(unittest.TestCase):
    def test_unavailable_executable_fails_every_item_once(self):
        observer = RecordingObserver()
        jobs = [job('123'), job('789')]
        with tempfile.TemporaryDirectory() as root, \
             patch.object(steamcmd.subprocess, 'Popen', side_effect=FileNotFoundError('missing')):
            ctx = Context(DownloadConfig(), Path(root), threading.Event(), observer, 'missing', observer.completed)
            steamcmd.steamcmd_session(jobs, ctx, 1)
        self.assertEqual([j.result.status for j in jobs], ['failed', 'failed'])
        self.assertTrue(all('could not run' in j.result.message for j in jobs))
        self.assertEqual(observer.results, jobs)
        self.assertFalse(observer.tasks)

    def test_process_exiting_without_result_marks_item_failed(self):
        observer = RecordingObserver()
        j = job()
        with tempfile.TemporaryDirectory() as root, patch.object(steamcmd.subprocess, 'Popen') as popen:
            proc = popen.return_value
            proc.stdout = io.StringIO('SteamCMD finished without a download event\n')
            proc.poll.return_value = 0
            ctx = Context(DownloadConfig(), Path(root), threading.Event(), observer, 'steamcmd', observer.completed)
            steamcmd.steamcmd_session([j], ctx, 1)
            proc.wait.assert_called()
            proc.kill.assert_not_called()
            self.assertTrue(proc.stdout.closed)
        self.assertEqual(j.result.status, 'failed')
        self.assertIn('exited without downloading', j.result.message)
        self.assertEqual(observer.results, [j])

    def test_cancellation_terminates_real_process_and_completes_items_once(self):
        stop = threading.Event()
        class CancellingObserver(RecordingObserver):
            def add_task(self, description, **kwargs):
                task = super().add_task(description, **kwargs)
                stop.set()
                return task
        observer = CancellingObserver()
        jobs = [job('123'), job('789')]
        processes = []
        popen = steamcmd.subprocess.Popen
        def start(*args, **kwargs):
            proc = popen(*args, **kwargs)
            processes.append(proc)
            return proc
        with tempfile.TemporaryDirectory() as root:
            executable = Path(root) / 'waiting-steamcmd'
            executable.write_text(f'#!{sys.executable}\nimport time\ntime.sleep(30)\n')
            executable.chmod(0o755)
            ctx = Context(DownloadConfig(), Path(root), stop, observer, str(executable), observer.completed)
            try:
                with patch.object(steamcmd.subprocess, 'Popen', side_effect=start):
                    steamcmd.steamcmd_session(jobs, ctx, 1)
            finally:
                for proc in processes:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait()
            self.assertEqual(len(processes), 1)
            self.assertIsNotNone(processes[0].returncode)
            self.assertTrue(processes[0].stdout.closed)
        self.assertEqual([j.result.status for j in jobs], ['cancelled', 'cancelled'])
        self.assertEqual(observer.results, jobs)
        self.assertFalse(observer.tasks)


class CliFailureTests(unittest.TestCase):
    def test_conflicting_options_exit_with_usage_error(self):
        with redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit) as error:
                cli.main(['-u', '123', '--no-steamcmd', '--force-steamcmd'])
        self.assertEqual(error.exception.code, 2)
        self.assertIn('mutually exclusive', stderr.getvalue())

    def test_unreadable_input_exits_before_api_calls(self):
        console = Console(file=io.StringIO())
        with patch.object(cli, 'Console', return_value=console), \
             patch.object(Path, 'read_text', side_effect=PermissionError('denied')), \
             patch.object(cli, 'resolve_items') as resolve:
            self.assertEqual(cli.main(['-f', 'private.txt']), 2)
        resolve.assert_not_called()
        self.assertIn('permission denied while reading', console.file.getvalue())

    def test_unwritable_output_exits_before_api_calls(self):
        console = Console(file=io.StringIO())
        with tempfile.TemporaryDirectory() as root, \
             patch.object(cli, 'Console', return_value=console), \
             patch.object(Path, 'write_text', side_effect=PermissionError('denied')), \
             patch.object(cli, 'resolve_items') as resolve:
            self.assertEqual(cli.main(['-u', '123', '-o', root]), 2)
        resolve.assert_not_called()
        self.assertIn('could not use the output directory', console.file.getvalue())


class ConcurrentCancellationTests(unittest.TestCase):
    def test_interrupt_waits_for_active_workers_and_completes_every_job_once(self):
        stop = threading.Event()
        started = threading.Barrier(3, timeout=5)
        exited = []
        lock = threading.Lock()
        observer = RecordingObserver()
        jobs = [job(str(i), file_url='http://example.test') for i in range(100, 106)]

        def download(j, final, config, worker_stop, progress, task):
            if worker_stop.is_set():
                raise Cancelled()
            try:
                started.wait()
                if not worker_stop.wait(5):
                    raise AssertionError('Worker did not receive cancellation')
                raise Cancelled()
            finally:
                with lock:
                    exited.append(j.item.id)

        def interrupted_completion(futures):
            # The real pool has two running workers before its wait is interrupted.
            started.wait()
            raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as root, \
             patch.object(orchestrator, 'download_direct', side_effect=download), \
             patch.object(orchestrator, 'as_completed', side_effect=interrupted_completion):
            with self.assertRaises(KeyboardInterrupt):
                orchestrator.execute_downloads(jobs, DownloadConfig(workers=2), Path(root), stop, observer, None)
        self.assertTrue(stop.is_set())
        self.assertEqual(len(exited), 2)
        self.assertEqual([j.result.status for j in jobs], ['cancelled'] * len(jobs))
        self.assertEqual(Counter(j.item.id for j in observer.results), Counter(j.item.id for j in jobs))
        self.assertFalse(observer.tasks)


if __name__ == '__main__':
    unittest.main()
