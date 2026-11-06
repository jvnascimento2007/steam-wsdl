"""Collection regressions using the legacy API shape (no file_type)."""
import threading
import unittest
from unittest.mock import patch

from steam_wsdl import workshop
from steam_wsdl.backends import steam_api
from steam_wsdl.config import DownloadConfig
from steam_wsdl.errors import Cancelled
from test_integration import RecordingObserver


class CollectionTests(unittest.TestCase):
    def resolve(self, ids):
        return workshop.resolve_items(ids, DownloadConfig(), threading.Event(), RecordingObserver())

    def test_legacy_collection_cover_is_never_a_download_job(self):
        # Actual GetPublishedFileDetails responses can expose the cover as file_url
        # and omit file_type entirely, for both collections and regular items.
        payloads = [
            {'response': {'publishedfiledetails': [
                {'publishedfileid': '100', 'result': 1, 'file_url': 'https://example.test/cover.jpg'}]}},
            {'response': {'collectiondetails': [
                {'publishedfileid': '100', 'result': 1, 'children': [{'publishedfileid': '200'}]}]}},
            {'response': {'publishedfiledetails': [
                {'publishedfileid': '200', 'result': 1, 'file_url': 'https://example.test/mod.zip'}]}},
            {'response': {'collectiondetails': [{'publishedfileid': '200', 'result': 9}]}},
        ]
        with patch.object(steam_api, 'api_post', side_effect=payloads) as post:
            jobs = self.resolve(['100'])
        self.assertEqual([(j.item.id, j.item.file_url) for j in jobs],
                         [('200', 'https://example.test/mod.zip')])
        self.assertEqual(post.call_count, 4)

    def test_nested_shared_and_cyclic_collections_preserve_order(self):
        metadata = {
            '100': {'result': 1}, '200': {'result': 1, 'file_type': 2},
            '300': {'result': 1, 'file_type': 0}, '400': {'result': 1, 'file_type': 0},
            '500': {'result': 9},
        }
        collections = {'100': ['200', '300'], '200': ['100', '400', '300', '500']}
        with patch.object(workshop, 'fetch_details', side_effect=lambda ids, *_: {i: metadata[i] for i in ids}) as details, \
             patch.object(workshop, 'fetch_collection_children', side_effect=lambda ids, *_: {i: collections[i] for i in ids}) as children:
            jobs = self.resolve(['100', '300', '100'])
        self.assertEqual([j.item.id for j in jobs], ['400', '300', '500'])
        self.assertEqual(jobs[-1].item.api_result, 9)
        self.assertEqual([call.args[0] for call in details.call_args_list],
                         [['100', '300'], ['200'], ['400', '500']])
        self.assertEqual([call.args[0] for call in children.call_args_list], [['100'], ['200']])

    def test_large_collection_queries_are_batched(self):
        ids = [str(i) for i in range(205)]
        with patch.object(steam_api, 'api_post', return_value={}) as post:
            self.assertEqual(steam_api.fetch_collection_children(ids, DownloadConfig(), threading.Event()), {})
        self.assertEqual([call.args[1]['collectioncount'] for call in post.call_args_list], [100, 100, 5])
        self.assertEqual(post.call_args.args[1]['publishedfileids[0]'], '200')

    def test_cancellation_between_expansion_levels(self):
        stop = threading.Event()
        def children(*_):
            stop.set()
            return {'100': ['200']}
        with patch.object(workshop, 'fetch_details', return_value={'100': {'result': 1}}), \
             patch.object(workshop, 'fetch_collection_children', side_effect=children):
            with self.assertRaises(Cancelled):
                workshop.resolve_items(['100'], DownloadConfig(), stop, RecordingObserver())
