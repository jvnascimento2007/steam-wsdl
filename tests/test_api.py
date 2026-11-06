"""Steam API transport, response parsing and thread-local session tests."""
import threading
import unittest
from unittest.mock import Mock, patch

import requests
from steam_wsdl.backends import steam_api
from steam_wsdl.config import DownloadConfig
from steam_wsdl.errors import Cancelled, WorkshopError
from steam_wsdl.workshop import item_from_detail, resolve_items
from test_integration import RecordingObserver


class ApiTests(unittest.TestCase):
    def test_sessions_are_reused_per_thread_but_not_shared(self):
        with patch.object(steam_api, '_local', threading.local()):
            main = steam_api.get_session()
            self.addCleanup(main.close)
            self.assertIs(steam_api.get_session(), main)
            sessions = []
            def worker():
                session = steam_api.get_session()
                sessions.append((session, steam_api.get_session()))
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads: thread.start()
            for thread in threads: thread.join(5)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(len(sessions), 2)
            for first, second in sessions:
                self.addCleanup(first.close)
                self.assertIs(first, second)
                self.assertIsNot(first, main)
                self.assertEqual(first.headers['User-Agent'], steam_api.USER_AGENT)
            self.assertIsNot(sessions[0][0], sessions[1][0])

    def test_cancelled_request_never_opens_connection(self):
        stop = threading.Event()
        stop.set()
        with patch.object(steam_api, 'get_session') as session:
            with self.assertRaises(Cancelled):
                steam_api.api_post('unused', {}, DownloadConfig(), stop)
            session.assert_not_called()

    def test_transient_failure_recovers(self):
        stop = threading.Event()
        response = Mock(status_code=200)
        response.json.return_value = {'response': 'ok'}
        with patch.object(steam_api, 'get_session') as session, patch.object(stop, 'wait', return_value=False):
            session.return_value.post.side_effect = [requests.ConnectionError('offline'), response]
            self.assertEqual(steam_api.api_post('url', {'id': '123'}, DownloadConfig(), stop), {'response': 'ok'})
            self.assertEqual(session.return_value.post.call_count, 2)

    def test_retries_exhausted(self):
        stop = threading.Event()
        with patch.object(steam_api, 'get_session') as session, patch.object(stop, 'wait', return_value=False):
            session.return_value.post.side_effect = requests.Timeout('offline')
            with self.assertRaisesRegex(WorkshopError, '3 attempts'):
                steam_api.api_post('url', {}, DownloadConfig(retries=2), stop)
            self.assertEqual(session.return_value.post.call_count, 3)

    def test_unexpected_transport_error_is_not_retried(self):
        with patch.object(steam_api, 'get_session') as session:
            session.return_value.post.side_effect = requests.RequestException('bad URL')
            with self.assertRaisesRegex(WorkshopError, 'network error'):
                steam_api.api_post('url', {}, DownloadConfig(), threading.Event())
            self.assertEqual(session.return_value.post.call_count, 1)

    def test_http_error_without_response(self):
        with patch.object(steam_api, 'get_session') as session:
            response = session.return_value.post.return_value
            response.status_code = 200
            response.raise_for_status.side_effect = requests.HTTPError('missing response')
            with self.assertRaisesRegex(WorkshopError, r'HTTP \?'):
                steam_api.api_post('url', {}, DownloadConfig(), threading.Event())

    def test_collection_response_filters_errors_and_missing_ids(self):
        payload = {'response': {'collectiondetails': [
            {'publishedfileid': '123', 'result': 1, 'children': [
                {'publishedfileid': '456'}, {}, {'publishedfileid': None}, {'publishedfileid': '789'}]},
            {'publishedfileid': '999', 'result': 9},
            {'publishedfileid': '888', 'result': 1, 'children': None},
        ]}}
        with patch.object(steam_api, 'api_post', return_value=payload) as post:
            self.assertEqual(steam_api.fetch_collection_children(['123', '999', '888'], DownloadConfig(), threading.Event()),
                             {'123': ['456', '789'], '888': []})
            self.assertEqual(post.call_args.args[1], {'collectioncount': 3, 'publishedfileids[0]': '123',
                                                     'publishedfileids[1]': '999', 'publishedfileids[2]': '888'})
        with patch.object(steam_api, 'api_post') as post:
            self.assertEqual(steam_api.fetch_collection_children([], DownloadConfig(), threading.Event()), {})
            post.assert_not_called()

    def test_missing_and_empty_api_results(self):
        for payload in ({}, {'response': {'publishedfiledetails': None, 'collectiondetails': None}}):
            with self.subTest(payload=payload), patch.object(steam_api, 'api_post', return_value=payload):
                self.assertEqual(steam_api.fetch_details(['123'], DownloadConfig(), threading.Event()), {})
                self.assertEqual(steam_api.fetch_collection_children(['123'], DownloadConfig(), threading.Event()), {})

    def test_item_metadata_defaults_and_creator_app_fallback(self):
        self.assertEqual(item_from_detail('123', None).item.api_result, 0)
        self.assertEqual(item_from_detail('123', {'result': 9}).item.api_result, 9)
        item = item_from_detail('123', {'result': 1, 'creator_app_id': 456}).item
        self.assertEqual((item.app_id, item.title, item.size, item.filename), ('456', '', 0, ''))
        item = item_from_detail('123', {'result': 1, 'consumer_app_id': 789, 'creator_app_id': 456,
                                       'title': 'Mod', 'file_url': 'url', 'file_size': '12', 'filename': 'x.bin'}).item
        self.assertEqual((item.app_id, item.title, item.size, item.filename, item.file_url), ('789', 'Mod', 12, 'x.bin', 'url'))

    def test_empty_collection_produces_no_downloads(self):
        with patch('steam_wsdl.workshop.fetch_details', return_value={'123': {'result': 1, 'file_type': 2}}), \
             patch('steam_wsdl.workshop.fetch_collection_children', return_value={'123': []}):
            self.assertEqual(resolve_items(['123'], DownloadConfig(), threading.Event(), RecordingObserver()), [])
