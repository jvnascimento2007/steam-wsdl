"""HTTP status handling and transport integration against a local HTTP server."""
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import requests
from steam_wsdl.backends import http
from steam_wsdl.config import DownloadConfig
from steam_wsdl.errors import Cancelled, DownloadError
from test_edges import Response
from test_integration import RecordingObserver, job


class HttpTests(unittest.TestCase):
    def test_refused_downloads_are_not_retried(self):
        for code in (401, 403, 404, 410):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as root, patch.object(http, 'get_session') as session:
                session.return_value.get.return_value = Response(code)
                observer = RecordingObserver()
                task = observer.add_task('mod')
                with self.assertRaisesRegex(DownloadError, f'HTTP {code}'):
                    http.download_direct(job(), Path(root) / 'mod', DownloadConfig(), threading.Event(), observer, task)
                self.assertEqual(session.return_value.get.call_count, 1)

    def test_transient_status_recovers_and_empty_chunks_are_ignored(self):
        class Chunks(Response):
            def iter_content(self, size):
                yield b''
                yield self.data
        stop = threading.Event()
        with tempfile.TemporaryDirectory() as root, patch.object(http, 'get_session') as session, \
             patch.object(stop, 'wait', return_value=False):
            session.return_value.get.side_effect = [Response(503), Chunks(200)]
            observer = RecordingObserver()
            task = observer.add_task('mod')
            target = Path(root) / 'mod'
            self.assertEqual(http.download_direct(job(), target, DownloadConfig(), stop, observer, task), 5)
            self.assertEqual(target.read_bytes(), b'fresh')
            self.assertEqual(session.return_value.get.call_count, 2)

    def test_request_exception_is_reported_without_retry(self):
        with tempfile.TemporaryDirectory() as root, patch.object(http, 'get_session') as session:
            session.return_value.get.side_effect = requests.RequestException('bad URL')
            observer = RecordingObserver()
            task = observer.add_task('mod')
            with self.assertRaisesRegex(DownloadError, 'network error'):
                http.download_direct(job(), Path(root) / 'mod', DownloadConfig(), threading.Event(), observer, task)
            self.assertEqual(session.return_value.get.call_count, 1)

    def test_cancel_during_retry_backoff(self):
        stop = threading.Event()
        with tempfile.TemporaryDirectory() as root, patch.object(http, 'get_session') as session, \
             patch.object(stop, 'wait', return_value=True):
            session.return_value.get.side_effect = requests.Timeout()
            observer = RecordingObserver()
            task = observer.add_task('mod')
            with self.assertRaises(Cancelled):
                http.download_direct(job(), Path(root) / 'mod', DownloadConfig(), stop, observer, task)
            self.assertEqual(session.return_value.get.call_count, 1)

    def test_failed_atomic_replace_preserves_old_destination_and_partial(self):
        with tempfile.TemporaryDirectory() as root, patch.object(http, 'get_session') as session:
            session.return_value.get.return_value = Response(200)
            target = Path(root) / 'mod'
            target.write_bytes(b'old')
            observer = RecordingObserver()
            task = observer.add_task('mod')
            with patch.object(http.os, 'replace', side_effect=OSError('replace failed')):
                with self.assertRaisesRegex(DownloadError, 'replace failed'):
                    http.download_direct(job(), target, DownloadConfig(), threading.Event(), observer, task)
            self.assertEqual(target.read_bytes(), b'old')
            self.assertEqual(target.with_name('mod.part').read_bytes(), b'fresh')

    def test_stream_without_length_or_metadata_size(self):
        with tempfile.TemporaryDirectory() as root, patch.object(http, 'get_session') as session:
            response = Response(200)
            response.headers = {}
            session.return_value.get.return_value = response
            observer = RecordingObserver()
            task = observer.add_task('mod')
            self.assertEqual(http.download_direct(job(), Path(root) / 'mod', DownloadConfig(), threading.Event(), observer, task), 5)
            self.assertIsNone(observer.tasks[task]['total'])

    def test_real_http_range_resume_against_local_server(self):
        ranges = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                ranges.append(self.headers.get('Range'))
                data = b'abcdef'
                start = int(self.headers['Range'][6:-1]) if self.headers.get('Range') else 0
                self.send_response(206 if start else 200)
                self.send_header('Content-Length', str(len(data) - start))
                if start: self.send_header('Content-Range', f'bytes {start}-5/6')
                self.end_headers()
                self.wfile.write(data[start:])
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01})
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as root, requests.Session() as session, patch.object(http, 'get_session', return_value=session):
                session.trust_env = False
                target = Path(root) / 'mod'
                target.with_name('mod.part').write_bytes(b'abc')
                observer = RecordingObserver()
                task = observer.add_task('mod')
                item = job(file_url=f'http://127.0.0.1:{server.server_port}/mod', size=6)
                self.assertEqual(http.download_direct(item, target, DownloadConfig(), threading.Event(), observer, task), 6)
                self.assertEqual(target.read_bytes(), b'abcdef')
                self.assertEqual(ranges, ['bytes=3-'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)
        self.assertFalse(thread.is_alive())
