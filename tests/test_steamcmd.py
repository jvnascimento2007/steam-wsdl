"""SteamCMD discovery, process output, file publication and progress lifecycle."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from steam_wsdl.backends import steamcmd
from steam_wsdl.config import DownloadConfig
from steam_wsdl.errors import UsageError
from steam_wsdl.runtime import Context
from test_integration import RecordingObserver, job
from steamcmd_fixture import executable, records, work_queue


class SteamcmdTests(unittest.TestCase):
    def run_session(self, root, jobs, body='', **config):
        executable_path = executable(root, body)
        observer = RecordingObserver()
        ctx = Context(DownloadConfig(retries=0, **config), root / 'output', threading.Event(),
                      observer, str(executable_path), observer.completed)
        steamcmd.steamcmd_session(work_queue(jobs), ctx, 1)
        self.assertEqual(observer.results, jobs)
        self.assertFalse(observer.tasks)
        self.assertEqual(list((root / 'output/.staging').iterdir()), [])
        return observer

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

    def test_one_login_for_more_than_40_items_and_same_filesystem_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = [job(str(i)) for i in range(45)]
            with patch.object(steamcmd.shutil, 'move', side_effect=AssertionError('must rename, not copy')):
                self.run_session(root, jobs)
            trace = records(root)
            starts = [r for r in trace if r['event'] == 'start']
            self.assertEqual(len(starts), 1)
            self.assertEqual(starts[0]['login_count'], 1)
            self.assertEqual(Path(starts[0]['root']).parent, root / 'output/.staging')
            self.assertEqual([r['item'] for r in trace if r['event'] == 'download'], [j.item.id for j in jobs])
            self.assertEqual(trace[-1]['event'], 'quit')
            self.assertTrue(all(j.result.status == 'ok' and j.result.bytes == 7 for j in jobs))

    def test_item_failure_does_not_restart_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = [job('123'), job('999'), job('789')]
            self.run_session(root, jobs)
            self.assertEqual([j.result.status for j in jobs], ['ok', 'failed', 'ok'])
            self.assertIn('--steam-user', jobs[1].result.message)
            self.assertEqual(sum(r['event'] == 'start' for r in records(root)), 1)

    def test_success_without_content_is_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            j = job()
            self.run_session(Path(tmp), [j], "print(f'Success. Downloaded item {pid}', flush=True)\nreturn")
            self.assertEqual(j.result.status, 'failed')
            self.assertIn('did not create', j.result.message)

    def test_content_without_success_is_not_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            j = job()
            self.run_session(root, [j], "content.mkdir(parents=True)\nprint('ERROR unexpected problem', flush=True)\nreturn")
            self.assertEqual(j.result.status, 'failed')
            self.assertIn('unexpected problem', j.result.message)
            self.assertFalse((root / 'output/456').exists())

    def test_overwrite_and_publication_error(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                dest = root / 'output/456/item [123]'
                dest.mkdir(parents=True)
                (dest / 'old').write_bytes(b'old')
                j = job()
                if fail:
                    with patch.object(Path, 'rename', side_effect=OSError('rename failed')):
                        self.run_session(root, [j], overwrite=True)
                    self.assertIn('disk error: rename failed', j.result.message)
                else:
                    self.run_session(root, [j], overwrite=True)
                    self.assertEqual((dest / 'data').read_bytes(), b'content')
                    self.assertFalse((dest / 'old').exists())

    def test_progress_and_confirmation_states_before_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            body = """staging.mkdir(parents=True)
(staging / 'data').write_bytes(b'abc')
time.sleep(0.25)
(staging / 'data').write_bytes(b'content')
time.sleep(0.25)
"""
            states = []
            j = job(size=7)
            class Observer(RecordingObserver):
                def add_task(self, description, **kwargs):
                    states.append(description)
                    return super().add_task(description, **kwargs)
                def update(self, task, **kwargs):
                    states.append(kwargs['description'])
                    if 'Awaiting confirmation' in kwargs['description']:
                        self_outer.assertEqual(j.result.status, 'pending')
                    super().update(task, **kwargs)
            self_outer = self
            obs = Observer()
            ctx = Context(DownloadConfig(retries=0), root/'out', threading.Event(), obs,
                          str(executable(root, body)), obs.completed)
            steamcmd.steamcmd_session(work_queue([j]), ctx, 1)
            for state in ('Starting', 'Downloading', 'Awaiting confirmation', 'Finalizing'):
                self.assertTrue(any(state in s for s in states), states)
            self.assertEqual(j.result.status, 'ok')
            # Both staging and content contain 7 bytes; they must not be added.
            self.assertEqual(j.result.bytes, 7)

    def test_crashed_process_is_replaced_without_losing_queued_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = [job('222'), job('789')]
            self.run_session(root, jobs, "if pid == '222': sys.exit(2)")
            self.assertEqual([j.result.status for j in jobs], ['failed', 'ok'])
            self.assertEqual(sum(r['event'] == 'start' for r in records(root)), 2)

    def test_retry_only_affected_item_after_process_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / 'crashed'
            body = f"""if not Path({str(marker)!r}).exists():
    Path({str(marker)!r}).touch()
    sys.exit(2)
"""
            obs = RecordingObserver()
            jobs = [job('123'), job('789')]
            ctx = Context(DownloadConfig(retries=1), root/'out', threading.Event(), obs,
                          str(executable(root, body)), obs.completed)
            steamcmd.steamcmd_session(work_queue(jobs), ctx, 1)
            self.assertEqual([j.result.status for j in jobs], ['ok', 'ok'])
            self.assertEqual(obs.results, jobs)
            self.assertEqual([r['item'] for r in records(root) if r['event']=='download'], ['123','123','789'])
            self.assertIn('restarting', obs.messages[0])

    def test_timeout_replaces_only_stalled_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = [job('333'), job('789')]
            self.run_session(root, jobs, "if pid == '333': time.sleep(30)", steamcmd_timeout=0.4)
            self.assertEqual([j.result.status for j in jobs], ['failed', 'ok'])
            self.assertIn('idle for', jobs[0].result.message)

    def test_non_posix_fallback_uses_shared_queue(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(steamcmd.sys, 'platform', 'win32'):
            root = Path(tmp)
            jobs = [job('123'), job('999'), job('789')]
            self.run_session(root, jobs)
            self.assertEqual([j.result.status for j in jobs], ['ok', 'failed', 'ok'])
            self.assertEqual(sum(r['event'] == 'start' for r in records(root)), 3)

    def test_no_process_when_queue_empty_cancelled_or_drained_during_stagger(self):
        stop = threading.Event()
        obs = RecordingObserver()
        with tempfile.TemporaryDirectory() as tmp, patch.object(steamcmd, 'SteamcmdProcess') as process:
            ctx = Context(DownloadConfig(), Path(tmp), stop, obs, 'steamcmd', obs.completed)
            steamcmd.steamcmd_session(work_queue([]), ctx, 1)
            stop.set()
            steamcmd.steamcmd_session(work_queue([job()]), ctx, 1)
            steamcmd.steamcmd_session(work_queue([job()]), ctx, 1, delay=.1)
            stop.clear()
            with patch.object(stop, 'wait', return_value=False):
                steamcmd.steamcmd_session(work_queue([]), ctx, 1, delay=.1)
            process.assert_not_called()

    def test_unexpected_error_fails_current_item_and_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = [job('123'), job('789')]
            original = steamcmd.SteamcmdProcess.download
            def download(process, item, *args):
                if item.item.id == '123': raise RuntimeError('boom')
                return original(process, item, *args)
            with patch.object(steamcmd.SteamcmdProcess, 'download', download):
                self.run_session(root, jobs)
            self.assertIn('RuntimeError', jobs[0].result.message)
            self.assertEqual(jobs[1].result.status, 'ok')

    def test_login_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(steamcmd.SteamcmdProcess, 'wait_prompt', return_value='ERROR Login Failure\nSteam>'):
            j = job()
            self.run_session(Path(tmp), [j])
            self.assertEqual(j.result.status, 'failed')
            self.assertIn('--steam-user', j.result.message)

    def test_broken_command_pipe_reports_session_error(self):
        process = steamcmd.SteamcmdProcess.__new__(steamcmd.SteamcmdProcess)
        process.output = Mock()
        process.output.write.side_effect = BrokenPipeError('closed')
        with self.assertRaisesRegex(steamcmd.SessionError, 'could not send command'):
            process.send('quit')

    def test_graceful_shutdown_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = executable(root)
            fake.write_text(fake.read_text().replace("record(event='quit')", "time.sleep(30)"))
            obs = RecordingObserver()
            j = job()
            ctx = Context(DownloadConfig(retries=0), root/'out', threading.Event(), obs, str(fake), obs.completed)
            with patch.object(steamcmd, 'SHUTDOWN_TIMEOUT', .1):
                steamcmd.steamcmd_session(work_queue([j]), ctx, 1)
            self.assertEqual(j.result.status, 'ok')
            self.assertFalse(obs.tasks)

    def test_non_posix_start_failure_and_timeout_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(steamcmd.sys, 'platform', 'win32'):
            root = Path(tmp)
            j = job()
            with patch.object(steamcmd.subprocess, 'Popen', side_effect=OSError('missing')):
                self.run_session(root, [j])
            self.assertIn('could not run', j.result.message)
            stalled = job('333')
            self.run_session(root, [stalled], 'time.sleep(30)', steamcmd_timeout=.3)
            self.assertIn('idle for', stalled.result.message)

    def test_staging_failure_completes_owned_item(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(steamcmd.tempfile, 'mkdtemp', side_effect=OSError('disk full')):
            obs = RecordingObserver()
            j = job()
            ctx = Context(DownloadConfig(), Path(tmp), threading.Event(), obs, 'steamcmd', obs.completed)
            pending = work_queue([j])
            steamcmd.steamcmd_session(pending, ctx, 1)
            self.assertEqual(j.result.message, 'disk error: disk full')
            self.assertEqual(obs.results, [j])
            self.assertFalse(obs.tasks)
            self.assertEqual(pending.unfinished_tasks, 0)

    def test_cancel_after_claim_and_observer_failure_leave_no_orphaned_job(self):
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled), tempfile.TemporaryDirectory() as tmp:
                obs = RecordingObserver()
                stop = Mock()
                stop.is_set.side_effect = [False, cancelled, True, True]
                if not cancelled: obs.add_task = Mock(side_effect=RuntimeError('UI error'))
                j = job()
                ctx = Context(DownloadConfig(), Path(tmp), stop, obs, 'steamcmd', obs.completed)
                steamcmd.steamcmd_session(work_queue([j]), ctx, 1)
                self.assertEqual(j.result.status, 'cancelled' if cancelled else 'failed')
                self.assertEqual(obs.results, [j])

    def test_timeout_kills_descendants_holding_terminal_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = "import subprocess\nsubprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\ntime.sleep(30)"
            j = job()
            self.run_session(Path(tmp), [j], body, steamcmd_timeout=.3)
            self.assertIn('idle for', j.result.message)
