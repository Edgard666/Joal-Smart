"""v1.32: scrapes with the client profile headers, passkeys masked in joal.log,
connection pinned to the verified tracker address (DNS rebinding)."""
import gzip
import http.server
import io
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
import log_rotation  # noqa: E402
import tracker_scrape  # noqa: E402
from core import encode_bencode  # noqa: E402

HASH = 'ab' * 20


class Recorder(http.server.BaseHTTPRequestHandler):
    seen = []
    gzip_reply = False

    def do_GET(self):
        Recorder.seen.append((self.headers.get('Host'), self.headers.get('User-Agent'), self.path))
        body = encode_bencode({b'files': {bytes.fromhex(HASH): {b'complete': 7, b'incomplete': 3}}})
        if Recorder.gzip_reply:
            body = gzip.compress(body)
        self.send_response(200)
        if Recorder.gzip_reply:
            self.send_header('Content-Encoding', 'gzip')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class Identity:
    headers = [('User-Agent', 'qBittorrent/4.6.2'), ('Accept-Encoding', 'gzip')]


class PinnedScrape(unittest.TestCase):
    def setUp(self):
        Recorder.seen = []
        Recorder.gzip_reply = False
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Recorder)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)

    def scrape(self):
        # The name does not resolve: only the pinned address can answer.
        with patch.object(tracker_scrape, 'public_ip_only', lambda *a: '127.0.0.1'):
            return tracker_scrape.fetch_scrape(f'http://tracker.invalid:{self.port}/pk123/announce', HASH, stats=True)

    def test_connection_goes_to_the_verified_address_with_the_real_host(self):
        self.assertEqual(self.scrape(), {'leechers': 3, 'seeders': 7})
        host, _, path = Recorder.seen[0]
        self.assertEqual(host, f'tracker.invalid:{self.port}')
        self.assertTrue(path.startswith('/pk123/scrape?info_hash='))

    def test_profile_headers_and_gzip_reply(self):
        Recorder.gzip_reply = True
        with patch.object(tracker_scrape, 'SCRAPE_IDENTITY', lambda _h: Identity()):
            self.assertEqual(self.scrape()['leechers'], 3)
        self.assertEqual(Recorder.seen[0][1], 'qBittorrent/4.6.2')

    def test_no_identity_uses_a_neutral_user_agent(self):
        self.scrape()
        self.assertNotIn('JOAL', Recorder.seen[0][1])

    def test_public_ip_only_returns_the_checked_address(self):
        answer = [(2, 1, 6, '', ('93.184.216.34', 80))]
        with patch('tracker_scrape.socket.getaddrinfo', return_value=answer):
            self.assertEqual(tracker_scrape.public_ip_only('example.org', 80), '93.184.216.34')

    def test_gzip_bomb_is_refused(self):
        bomb = gzip.compress(b'0' * (tracker_scrape.MAX_RESPONSE + 10))
        with self.assertRaises(tracker_scrape.ScrapeUnavailable):
            tracker_scrape.bounded_gunzip(bomb, tracker_scrape.MAX_RESPONSE)


class LogRedaction(unittest.TestCase):
    def test_tracker_urls_keep_only_the_host(self):
        line = b'WARN Failed https://tr.example.org/ABCDEF/announce?passkey=SECRET&x=1 (udp://u:p@t.example:6969/k)\n'
        out = log_rotation.redact(line)
        self.assertNotIn(b'ABCDEF', out)
        self.assertNotIn(b'SECRET', out)
        self.assertNotIn(b'u:p', out)
        self.assertIn(b'https://tr.example.org/', out)
        self.assertIn(b'udp://t.example:6969/', out)

    def test_loose_keys_and_markers(self):
        self.assertNotIn(b'zz9', log_rotation.redact(b'authkey=zz9 used\n'))
        line = b'java.lang.OutOfMemoryError: Java heap space\n'
        self.assertEqual(log_rotation.redact(line), line)

    def test_pump_writes_masked_lines(self):
        written = []

        class Log:
            def write(self, data):
                written.append(data)

            def close(self):
                pass

        log_rotation.pump(io.BytesIO(b'GET http://t.example/key/announce\n'), Log())
        self.assertEqual(written, [b'GET http://t.example/\xe2\x80\xa6\n'])


if __name__ == '__main__':
    unittest.main()
