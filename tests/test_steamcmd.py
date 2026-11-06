"""SteamCMD discovery, process output, file publication and progress lifecycle."""
import io
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from steam_wsdl import orchestrator
from steam_wsdl.backends import steamcmd
from steam_wsdl.config import DownloadConfig
from steam_wsdl.errors import DownloadError, UsageError
from steam_wsdl.runtime import Context
from test_integration import RecordingObserver, job


class InlineReader:
    """Run a finite stdout reader synchronously for deterministic output tests."""
    def __init__(self, target, daemon): self.target = target
    def start(self): self.target()
    def join(self): pass


class SteamcmdTests(unittest.TestCase):
    def run_batch(self, root, jobs, output='', create_content=True, overwrite=False, move_error=False):
        staging = Path(root) / 'staging'
        staging.mkdir()
        if create_content:
            for j in jobs:
                path = staging / 'steamapps/workshop/content' / j.item.app_id / j.item.id
                path.mkdir(parents=True)
                (path / 'data').write_bytes(b'new')
        observer = RecordingObserver()
        process = Mock()
        process.stdout = io.StringIO(output)
        process.poll.return_value = 0
        ctx = Context(DownloadConfig(overwrite=overwrite), Path(root) / 'output', threading.Event(),
                      observer, 'steamcmd', observer.completed)
        with patch.object(steamcmd.tempfile, 'mkdtemp', return_value=str(staging)), \
             patch.object(steamcmd.subprocess, 'Popen', return_value=process), \
             patch.object(steamcmd.threading, 'Thread', InlineReader):
            if move_error:
                with patch.object(steamcmd.shutil, 'move', side_effect=OSError('move failed')):
                    steamcmd._steamcmd_batch(jobs, ctx, 1)
            else:
                steamcmd._steamcmd_batch(jobs, ctx, 1)
        self.assertFalse(staging.exists())
        self.assertTrue(process.stdout.closed)
        self.assertEqual(observer.results, jobs)
        self.assertFalse(observer.tasks)
        return process, observer

    def test_executable_discovery(self):
        with tempfile.TemporaryDirectory() as root:
            executable = Path(root) / 'steamcmd-custom'
            executable.touch()
            with patch.object(steamcmd.shutil, 'which', return_value=None):
                self.assertEqual(steamcmd.find_steamcmd(str(executable)), str(executable))
                with self.assertRaises(UsageError): steamcmd.find_steamcmd(str(executable) + '-missing')
                self.assertIsNone(steamcmd.find_steamcmd(None))
            with patch.object(steamcmd.shutil, 'which', return_value='/bin/custom'):
                self.assertEqual(steamcmd.find_steamcmd('custom'), '/bin/custom')
            with patch.object(steamcmd.shutil, 'which', side_effect=[None, '/bin/steamcmd.sh']) as which:
                self.assertEqual(steamcmd.find_steamcmd(None), '/bin/steamcmd.sh')
                self.assertEqual([c.args[0] for c in which.call_args_list], ['steamcmd', 'steamcmd.sh'])

    def test_error_output_messages(self):
        for text in ('No subscription', 'Access Denied', 'Invalid Password', 'Login Failure'):
            with self.subTest(text=text): self.assertIn('--steam-user', steamcmd.steamcmd_reason(text))
        self.assertIn('unknown failure', steamcmd.steamcmd_reason(''))
        self.assertEqual(steamcmd.parse_steamcmd_error('noise\nERROR one\noperation failed\nnoise'), 'steamcmd: operation failed')
        self.assertEqual(steamcmd.parse_steamcmd_error('noise\nERROR one\nnoise'), 'steamcmd: ERROR one')
        self.assertIn('exited without downloading', steamcmd.parse_steamcmd_error('noise'))

    def test_success_event_without_content_is_failure(self):
        with tempfile.TemporaryDirectory() as root:
            j = job()
            self.run_batch(root, [j], 'Success. Downloaded item 123\n', create_content=False)
            self.assertEqual(j.result.status, 'failed')
            self.assertIn('did not create the item directory', j.result.message)

    def test_existing_directory_is_replaced(self):
        with tempfile.TemporaryDirectory() as root:
            dest = Path(root) / 'output/456/item [123]'
            dest.mkdir(parents=True)
            (dest / 'old').write_bytes(b'old')
            j = job()
            self.run_batch(root, [j], 'Success. Downloaded item 123\n', overwrite=True)
            self.assertEqual(j.result.status, 'ok')
            self.assertEqual((dest / 'data').read_bytes(), b'new')
            self.assertFalse((dest / 'old').exists())

    def test_move_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as root:
            j = job()
            self.run_batch(root, [j], 'Success. Downloaded item 123\n', move_error=True)
            self.assertEqual(j.result.status, 'failed')
            self.assertIn('disk error: move failed', j.result.message)

    def test_directory_detected_even_without_success_event(self):
        with tempfile.TemporaryDirectory() as root:
            j = job()
            self.run_batch(root, [j], '\nnoise\nSuccess. Downloaded item 999\nERROR! Download item 999 failed (Failure)\n')
            self.assertEqual(j.result.status, 'ok')
            self.assertEqual(j.result.path.joinpath('data').read_bytes(), b'new')

    def test_http_failure_falls_back_to_successful_steamcmd_batch(self):
        with tempfile.TemporaryDirectory() as root:
            staging = Path(root) / 'staging'
            content = staging / 'steamapps/workshop/content/456/123'
            content.mkdir(parents=True)
            (content / 'data').write_bytes(b'new')
            process = Mock()
            process.stdout = io.StringIO('Success. Downloaded item 123\n')
            process.poll.return_value = 0
            observer = RecordingObserver()
            j = job(file_url='http://example.test')
            with patch.object(steamcmd.tempfile, 'mkdtemp', return_value=str(staging)), \
                 patch.object(steamcmd.subprocess, 'Popen', return_value=process), \
                 patch.object(steamcmd.threading, 'Thread', InlineReader), \
                 patch.object(orchestrator, 'download_direct', side_effect=DownloadError('http failed')):
                # Exercise the worker without replacing the global Thread class used by its pool.
                ctx = Context(DownloadConfig(), Path(root) / 'output', threading.Event(), observer,
                              'steamcmd', lambda j: orchestrator.complete_item(j, observer))
                deferred = orchestrator.process_item(j, ctx)
                steamcmd.steamcmd_session([deferred], ctx, 1)
            self.assertEqual((j.result.status, j.result.method), ('ok', 'steamcmd'))
            self.assertEqual(j.result.direct_error, 'http failed')
            self.assertEqual(j.result.message, '')
            self.assertEqual(j.result.path.joinpath('data').read_bytes(), b'new')
            self.assertEqual(observer.results, [j])

    def test_session_cancellation_before_batch_and_during_stagger(self):
        stop = threading.Event()
        observer = RecordingObserver()
        with tempfile.TemporaryDirectory() as root:
            ctx = Context(DownloadConfig(), Path(root), stop, observer, 'steamcmd', observer.completed)
            stop.set()
            with patch.object(steamcmd, '_steamcmd_batch') as batch:
                steamcmd.steamcmd_session([job()], ctx, 1, delay=0.1)
                steamcmd.steamcmd_session([job()], ctx, 1)
                batch.assert_not_called()
            stop.clear()
            with patch.object(stop, 'wait', return_value=False), patch.object(steamcmd, '_steamcmd_batch') as batch:
                steamcmd.steamcmd_session([job()], ctx, 1, delay=0.1)
                batch.assert_called_once()

    def test_unexpected_batch_exception_only_fails_pending_items(self):
        observer = RecordingObserver()
        jobs = [job('123'), job('789')]
        def fail(batch, ctx, number):
            batch[0].result.status = 'ok'
            ctx.on_complete(batch[0])
            raise RuntimeError('boom')
        with tempfile.TemporaryDirectory() as root:
            ctx = Context(DownloadConfig(), Path(root), threading.Event(), observer, 'steamcmd', observer.completed)
            with patch.object(steamcmd, '_steamcmd_batch', side_effect=fail):
                steamcmd.steamcmd_session(jobs, ctx, 1)
        self.assertEqual([j.result.status for j in jobs], ['ok', 'failed'])
        self.assertIn('RuntimeError', jobs[1].result.message)
        self.assertEqual(observer.results, jobs)

    def test_progress_tracks_growth_and_completion_during_output(self):
        with tempfile.TemporaryDirectory() as root:
            staging = Path(root) / 'staging'
            downloads = staging / 'steamapps/workshop/downloads/456/123'
            downloads.mkdir(parents=True)
            (downloads / 'data').write_bytes(b'abc')
            events = queue.Queue()
            process = Mock()
            process.stdout = io.StringIO()
            process.poll.return_value = 0
            snapshots = []
            class Observer(RecordingObserver):
                def update(self, task, **kwargs):
                    super().update(task, **kwargs)
                    snapshots.append((task, kwargs['completed']))
            observer = Observer()
            tick = 0
            class Stop:
                def is_set(self): return False
                def wait(self, delay):
                    nonlocal tick
                    tick += 1
                    if tick == 2:
                        content = staging / 'steamapps/workshop/content/456/123'
                        content.mkdir(parents=True)
                        (content / 'data').write_bytes(b'abcdefg')
                        events.put('Success. Downloaded item 123\n')
                    elif tick == 3:
                        content = staging / 'steamapps/workshop/content/456/789'
                        content.mkdir(parents=True)
                        (content / 'data').write_bytes(b'xyz')
                        events.put('Success. Downloaded item 789\n')
                    elif tick == 4:
                        events.put(None)
                    if tick > 4: raise AssertionError('Batch failed to terminate')
                    return False
            jobs = [job('123', size=10), job('789')]
            ctx = Context(DownloadConfig(), Path(root) / 'output', Stop(), observer, 'steamcmd', observer.completed)
            with patch.object(steamcmd.tempfile, 'mkdtemp', return_value=str(staging)), \
                 patch.object(steamcmd.subprocess, 'Popen', return_value=process), \
                 patch.object(steamcmd.queue, 'Queue', return_value=events), \
                 patch.object(steamcmd.threading, 'Thread'):
                steamcmd._steamcmd_batch(jobs, ctx, 1)
            self.assertEqual([j.result.status for j in jobs], ['ok', 'ok'])
            self.assertEqual(observer.results, jobs)
            self.assertEqual(snapshots[:2], [(1, 3), (1, 3)])
            self.assertFalse(observer.tasks)
            self.assertFalse(staging.exists())

    def test_cleanup_kills_process_after_observer_failure(self):
        with tempfile.TemporaryDirectory() as root:
            process = Mock()
            process.stdout = io.StringIO()
            process.poll.return_value = None
            observer = RecordingObserver()
            observer.add_task = Mock(side_effect=RuntimeError('observer failed'))
            ctx = Context(DownloadConfig(), Path(root), threading.Event(), observer, 'steamcmd', observer.completed)
            with patch.object(steamcmd.subprocess, 'Popen', return_value=process), \
                 patch.object(steamcmd.threading, 'Thread'):
                with self.assertRaisesRegex(RuntimeError, 'observer failed'):
                    steamcmd._steamcmd_batch([job()], ctx, 1)
            process.kill.assert_called_once()
            process.wait.assert_called_once()
            self.assertTrue(process.stdout.closed)
