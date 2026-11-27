"""v1.33: lighter and faster — library cache, qBittorrent tracker cache, single qBittorrent
connection for probes, gzip answers, JVM options, jlink image."""
import gzip
import hashlib
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
sys.path.insert(0, str(ROOT / 'tests'))
import main  # noqa: E402
from core import encode_bencode  # noqa: E402
from solo_torrents import SoloLibrary  # noqa: E402
from web_auth import SessionManager  # noqa: E402
from webtest_helpers import login  # noqa: E402


def torrent(name, announce=b'https://t.example.org/pk/announce'):
    info = {b'name': name.encode(), b'length': 1000, b'piece length': 16384, b'pieces': b'x' * 20}
    return hashlib.sha1(encode_bencode(info)).hexdigest(), encode_bencode({b'announce': announce, b'info': info})


class Library(unittest.TestCase):
    def test_files_are_decoded_once_and_payload_read_on_demand(self):
        with tempfile.TemporaryDirectory() as folder:
            h, blob = torrent('alpha')
            (Path(folder) / f'{h}.torrent').write_bytes(blob)
            library = SoloLibrary(folder)
            reads = []
            original = library._read_torrent
            with patch.object(library, '_read_torrent', lambda *a: reads.append(1) or original(*a)):
                first = library.list([{'enabled': True, 'trackerDomains': ['t.example.org']}])
                second = library.list([])
            self.assertEqual(len(reads), 1)
            self.assertEqual(first[0]['matchedDomains'], ['t.example.org'])
            self.assertEqual(second[0]['matchedDomains'], [])           # recomputed for the rules given
            self.assertNotIn('payload', first[0])
            self.assertEqual(library.payload(first[0]), blob)
            (Path(folder) / f'{h}.torrent').write_bytes(blob + b' ')
            with self.assertRaises(ValueError):
                library.payload(first[0])                                # changed since list()


class QbitCaches(unittest.TestCase):
    def test_tracker_list_cached_for_a_stopped_torrent_only(self):
        class Client:
            calls = 0

            def trackers(self, h):
                Client.calls += 1
                return [{'url': 'https://t.example.org/announce'}]
        client = Client()
        stopped = {'hash': 'a' * 40, 'state': 'stoppedUP', 'progress': 1, 'tracker': '', 'trackers_count': 1}
        with patch.dict(main._TRACKERS, clear=True):
            for _ in range(3):
                main.cached_trackers(client, stopped)
            self.assertEqual(Client.calls, 1)
            main.cached_trackers(client, dict(stopped, trackers_count=2))   # changed: read again
            self.assertEqual(Client.calls, 2)
            running = dict(stopped, state='uploading')
            main.cached_trackers(client, running)
            main.cached_trackers(client, running)
            self.assertEqual(Client.calls, 4)                              # running: always fresh

    def test_probe_inventory_read_once_per_minute(self):
        created = []

        class Client:
            def __init__(self, *a):
                created.append(self)

            def torrents_all(self):
                return [{'hash': 'b' * 40}]
        with patch.object(main, 'QBitClient', Client), patch.object(main, 'QBIT_URL', 'http://qbit:8080'), \
                patch.dict(main._PROBE_INVENTORY, clear=True), patch.object(main, 'RUNNERS', {}):
            self.assertTrue(main.import_probe_allowed('a' * 40))
            self.assertFalse(main.import_probe_allowed('b' * 40))
            self.assertTrue(main.import_probe_allowed('c' * 40))
        self.assertEqual(len(created), 1)


class Http(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        data = Path(folder.name)
        (data / 'policy.json').write_text((ROOT / 'config.example.json').read_text())
        rows = [{'infoHash': '%040x' % i, 'name': f'torrent {i}', 'reason': 'Leechers insuffisants : 0 / minimum 3'}
                for i in range(300)]
        for item in (patch.object(main, 'PASSWORD', 'mot-de-passe-test'),
                     patch.object(main, 'AUTH', SessionManager(lambda: main.PASSWORD)),
                     patch.object(main, 'DATA', data), patch.object(main, 'CONFIG', data / 'policy.json'),
                     patch.dict(main.STATUS, {'torrents': rows}), patch.object(main, 'HANDOFF_LEDGER', None)):
            item.start()
            self.addCleanup(item.stop)
        server = main.ThreadingHTTPServer(('127.0.0.1', 0), main.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f'http://127.0.0.1:{server.server_port}'
        self.cookie, _ = login(self.base, 'mot-de-passe-test')

    def get(self, path, encoding):
        request = urllib.request.Request(self.base + path, headers={'Cookie': self.cookie, 'Accept-Encoding': encoding})
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.headers, response.read()

    def test_state_is_gzipped_and_without_upload_history(self):
        headers, body = self.get('/api/state', 'gzip, deflate')
        self.assertEqual(headers['Content-Encoding'], 'gzip')
        plain = gzip.decompress(body)
        state = json.loads(plain)
        self.assertNotIn('uploadHistory', state)
        self.assertEqual(len(state['torrents']), 300)
        self.assertLess(len(body) * 5, len(plain))
        headers, body = self.get('/api/state', 'identity')
        self.assertIsNone(headers['Content-Encoding'])
        self.assertEqual(json.loads(body)['torrents'][0]['name'], 'torrent 0')

    def test_static_script_gzipped(self):
        headers, body = self.get('/assets/app.js', 'gzip')
        self.assertEqual(headers['Content-Encoding'], 'gzip')
        self.assertEqual(gzip.decompress(body), (ROOT / 'app' / 'static' / 'app.js').read_bytes())


class Runtime(unittest.TestCase):
    def test_jvm_options(self):
        options = main._joal_java_opts(0)
        for flag in ('-XX:TieredStopAtLevel=1', '-XX:ReservedCodeCacheSize=24m', '-Xss256k', '-XX:+UseSerialGC',
                     '-XX:+ExitOnOutOfMemoryError'):
            self.assertIn(flag, options)

    def test_image_uses_a_jlink_runtime(self):
        dockerfile = (ROOT / 'Dockerfile').read_text()
        self.assertIn('RUN jlink --add-modules', dockerfile)
        self.assertIn('COPY --from=build /build/jre /opt/java/openjdk', dockerfile)
        self.assertNotIn('JRE_IMAGE', dockerfile)
        self.assertIn('base_image', (ROOT / '.github' / 'workflows' / 'image.yml').read_text())


if __name__ == '__main__':
    unittest.main()
