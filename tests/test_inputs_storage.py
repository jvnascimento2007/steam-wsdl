"""Input validation, safe destination names and filesystem edge cases."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from steam_wsdl.config import DownloadConfig
from steam_wsdl.errors import UsageError
from steam_wsdl.formatting import human_size, truncate
from steam_wsdl.inputs import collect_ids, parse_workshop_id
from steam_wsdl.models import WorkshopItem
from steam_wsdl.storage import direct_target, dir_size, prepare_output, sanitize, steamcmd_target


class InputStorageTests(unittest.TestCase):
    def test_workshop_url_variants(self):
        valid = ['123', ' "123" ', 'steam://url/CommunityFilePage/123',
                 'https://steamcommunity.com/sharedfiles/filedetails/?id=123',
                 'steamcommunity.com/workshop/filedetails/?id=123',
                 'http://www.steamcommunity.com/sharedfiles/filedetails/?id=123']
        for text in valid:
            with self.subTest(text=text): self.assertEqual(parse_workshop_id(text), '123')
        invalid = ['', '12', 'http://[', 'https://steamcommunity.com/?id=abc',
                   'https://steamcommunity.com/', 'ftp://steamcommunity.com/?id=123',
                   'https://evilsteamcommunity.com/?id=123', 'https://steamcommunity.com.evil.test/?id=123']
        for text in invalid:
            with self.subTest(text=text): self.assertIsNone(parse_workshop_id(text))

    def test_input_file_errors(self):
        with tempfile.TemporaryDirectory() as root:
            invalid = Path(root) / 'invalid.txt'
            invalid.write_bytes(b'\xff\xfe')
            cases = [(Path(root) / 'missing', 'not found'), (Path(root), 'directory'), (invalid, 'UTF-8')]
            for path, message in cases:
                with self.subTest(path=path), self.assertRaisesRegex(UsageError, message):
                    collect_ids(DownloadConfig(file=str(path)))
        with patch.object(Path, 'read_text', side_effect=OSError('disk failure')):
            with self.assertRaisesRegex(UsageError, 'could not read'):
                collect_ids(DownloadConfig(file='file'))

    def test_blank_lines_and_input_order(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'links.txt'
            path.write_text('\n  \n123\n456\n123\n')
            ids, problems = collect_ids(DownloadConfig(url=['789', 'invalid'], file=str(path)))
            self.assertEqual(ids, ['789', '123', '456'])
            self.assertEqual(len(problems), 1)

    def test_safe_names_and_destinations(self):
        for value, expected in [('CON', '_CON'), ('com1', '_com1'), ('A/B:C', 'A_B_C'),
                                ('  hello   world. ', 'hello world'), ('...', ''), ('', '')]:
            with self.subTest(value=value): self.assertEqual(sanitize(value), expected)
        self.assertEqual(sanitize('abcdefgh', 4), 'abcd')
        self.assertEqual(sanitize('x\x00y'), 'x_y')
        item = WorkshopItem('123', title='CON', filename=r'C:\mods\test.gma')
        self.assertEqual(direct_target(item, Path('out')), Path('out/unknown_app/_CON [123].gma'))
        self.assertEqual(steamcmd_target(WorkshopItem('123'), Path('out')), Path('out/unknown_app/item [123]'))
        self.assertEqual(direct_target(WorkshopItem('123', filename='x.' + 'a' * 30), Path('out')).suffix, '.' + 'a' * 11)

    def test_directory_size_and_files_disappearing(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / 'sub').mkdir()
            (path / 'sub/data').write_bytes(b'12345')
            (path / 'other').write_bytes(b'12')
            self.assertEqual(dir_size(path), 7)
            with patch('steam_wsdl.storage.os.path.getsize', side_effect=FileNotFoundError):
                self.assertEqual(dir_size(path), 0)
            with patch('steam_wsdl.storage.os.walk', side_effect=OSError):
                self.assertEqual(dir_size(path), 0)

    def test_prepare_output_creates_directory_and_removes_probe(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'nested/output'
            self.assertEqual(prepare_output(str(path)), path.resolve())
            self.assertEqual(list(path.iterdir()), [])

    def test_size_and_truncation_formatting(self):
        for value, expected in [(0, '0 B'), (1023, '1023 B'), (1024, '1.0 KB'),
                                (1024**2, '1.0 MB'), (1024**3, '1.0 GB'),
                                (1024**4, '1.0 TB'), (1024**5, '1024.0 TB'), (-1024, '-1.0 KB')]:
            with self.subTest(value=value): self.assertEqual(human_size(value), expected)
        self.assertEqual(truncate('hello', 5), 'hello')
        self.assertEqual(truncate('hello', 4), 'hel…')
