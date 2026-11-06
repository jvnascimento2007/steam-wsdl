"""Offline regression tests for orchestration, process lifecycle and CLI exits."""
import io
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from rich.console import Console
from steam_wsdl import cli, orchestrator
from steam_wsdl.backends import http, steamcmd
from steam_wsdl.config import DownloadConfig
from steam_wsdl.errors import DownloadError, WorkshopError
from steam_wsdl.models import DownloadJob, WorkshopItem
from steam_wsdl.runtime import Context


class RecordingObserver:
    def __init__(self):
        self.results = []
        self.tasks = {}
        self.messages = []
        self.counter = 0

    def add_task(self, description, **kwargs):
        self.counter += 1
        self.tasks[self.counter] = kwargs
        return self.counter

    def update(self, task, **kwargs): self.tasks[task].update(kwargs)
    def start_task(self, task): pass
    def remove_task(self, task): self.tasks.pop(task)
    def message(self, text): self.messages.append(text)
    def completed(self, job): self.results.append(job)


def job(pid='123', **kwargs):
    return DownloadJob(WorkshopItem(pid, app_id='456', **kwargs))


class IntegrationTests(unittest.TestCase):
    def test_real_steamcmd_process_batches_success_and_failure(self):
        with tempfile.TemporaryDirectory() as root:
            executable = Path(root) / 'fake-steamcmd'
            executable.write_text(f'''#!{sys.executable}
import sys
from pathlib import Path
args = sys.argv
root = Path(args[args.index('+force_install_dir') + 1])
assert args.count('+login') == 1
for i, arg in enumerate(args):
    if arg != '+workshop_download_item': continue
    app, pid = args[i+1:i+3]
    if pid == '999':
        print('ERROR! Download item 999 failed (No subscription)', flush=True)
    else:
        dest = root / 'steamapps' / 'workshop' / 'content' / app / pid
        dest.mkdir(parents=True)
        (dest / 'data').write_bytes(b'content')
        print(f'Success. Downloaded item {{pid}}', flush=True)
''')
            executable.chmod(0o755)
            observer = RecordingObserver()
            jobs = [job('123'), job('999'), job('789')]
            orchestrator.execute_downloads(jobs, DownloadConfig(workers=1), Path(root) / 'output',
                                           threading.Event(), observer, str(executable))
            self.assertEqual([j.result.status for j in jobs], ['ok', 'failed', 'ok'])
            self.assertEqual(jobs[0].result.path.joinpath('data').read_bytes(), b'content')
            self.assertEqual(jobs[0].result.bytes, 7)
            self.assertIn('--steam-user', jobs[1].result.message)
            self.assertCountEqual(observer.results, jobs)
            self.assertFalse(observer.tasks)

    def test_http_failure_falls_back_and_combines_errors_once(self):
        observer = RecordingObserver()
        jobs = [job(file_url='http://example.test')]
        def fail_batch(batch, ctx, number, delay):
            for j in batch:
                j.result.status, j.result.message = 'failed', 'steam error'
                ctx.on_complete(j)
        with tempfile.TemporaryDirectory() as root, \
             patch.object(orchestrator, 'download_direct', side_effect=DownloadError('http error')), \
             patch.object(orchestrator, 'steamcmd_session', side_effect=fail_batch):
            orchestrator.execute_downloads(jobs, DownloadConfig(workers=1), Path(root),
                                           threading.Event(), observer, 'steamcmd')
        self.assertEqual(jobs[0].result.message, 'direto: http error | steam error')
        self.assertEqual(observer.results, jobs)

    def test_pending_jobs_are_cancelled_when_pool_is_interrupted(self):
        observer = RecordingObserver()
        stop = threading.Event()
        jobs = [job('123'), job('789')]
        def interrupt(*args):
            stop.set()
            raise KeyboardInterrupt
        with tempfile.TemporaryDirectory() as root, \
             patch.object(orchestrator, 'run_direct_stage', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                orchestrator.execute_downloads(jobs, DownloadConfig(), Path(root), stop, observer, None)
        self.assertEqual([j.result.status for j in jobs], ['cancelled', 'cancelled'])
        self.assertEqual(observer.results, jobs)

    def test_existing_download_skips_network(self):
        observer = RecordingObserver()
        j = job(title='Mod', file_url='http://example.test', filename='mod.bin')
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / '456' / 'Mod [123].bin'
            target.parent.mkdir()
            target.write_bytes(b'existing')
            with patch.object(orchestrator, 'download_direct') as download:
                orchestrator.execute_downloads([j], DownloadConfig(), Path(root), threading.Event(), observer, None)
            download.assert_not_called()
            self.assertEqual(j.result.status, 'skipped')
            self.assertEqual(target.read_bytes(), b'existing')

    def test_authenticated_account_limits_parallel_sessions(self):
        observer = RecordingObserver()
        with tempfile.TemporaryDirectory() as root:
            ctx = Context(DownloadConfig(workers=4, steam_user='user'), Path(root), threading.Event(), observer, 'steamcmd', observer.completed)
            with patch.object(orchestrator, '_run_pool', return_value=[]) as pool:
                orchestrator.run_steamcmd_stage([job('123'), job('789')], ctx)
            self.assertEqual(pool.call_args.args[0], 1)

    def test_steamcmd_session_chunks_batches(self):
        observer = RecordingObserver()
        with tempfile.TemporaryDirectory() as root:
            ctx = Context(DownloadConfig(), Path(root), threading.Event(), observer, 'steamcmd', observer.completed)
            with patch.object(steamcmd, '_steamcmd_batch') as batch:
                steamcmd.steamcmd_session([job(str(i)) for i in range(81)], ctx, 1)
            self.assertEqual([len(c.args[0]) for c in batch.call_args_list], [40, 40, 1])

    def test_cli_exit_codes_and_failed_file(self):
        for status, expected in [('ok', 0), ('failed', 1)]:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as root:
                j = job()
                failed_file = Path(root) / 'failed.txt'
                def execute(items, *args):
                    j.result.status = status
                    j.result.message = 'error' if status == 'failed' else ''
                    args[-2].completed(j)
                with patch.object(cli, 'resolve_items', return_value=[j]), \
                     patch.object(cli, 'execute_downloads', side_effect=execute):
                    code = cli.run(DownloadConfig(url=['123'], output=root, no_steamcmd=True,
                                                  failed_file=str(failed_file)), Console(file=io.StringIO()))
                self.assertEqual(code, expected)
                self.assertEqual(failed_file.exists(), status == 'failed')
                if status == 'failed': self.assertEqual(failed_file.read_text(), j.item.url + '\n')
        with patch.object(cli, 'Console', return_value=Console(file=io.StringIO())):
            self.assertEqual(cli.main(['-u', 'invalid']), 2)
        with tempfile.TemporaryDirectory() as root, \
             patch.object(cli, 'resolve_items', side_effect=WorkshopError('offline')):
            self.assertEqual(cli.run(DownloadConfig(url=['123'], output=root, no_steamcmd=True), Console(file=io.StringIO())), 1)
        with tempfile.TemporaryDirectory() as root, \
             patch.object(cli, 'resolve_items', return_value=[job()]), \
             patch.object(cli, 'execute_downloads', side_effect=KeyboardInterrupt):
            self.assertEqual(cli.run(DownloadConfig(url=['123'], output=root, no_steamcmd=True), Console(file=io.StringIO())), 130)

    def test_http_server_ignoring_range_restarts_file(self):
        class Response:
            status_code = 200
            headers = {'Content-Length': '3'}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def raise_for_status(self): pass
            def iter_content(self, size): yield b'new'
        with tempfile.TemporaryDirectory() as root, patch.object(http, 'get_session') as session:
            final = Path(root) / 'mod'
            final.with_suffix('.part').write_bytes(b'old')
            session.return_value.get.return_value = Response()
            observer = RecordingObserver()
            task = observer.add_task('download')
            self.assertEqual(http.download_direct(job(file_url='http://example.test'), final,
                             DownloadConfig(retries=0), threading.Event(), observer, task), 3)
            self.assertEqual(final.read_bytes(), b'new')


if __name__ == '__main__':
    unittest.main()
