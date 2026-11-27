"""v1.33: quieter tracker readings — adaptive interval, grouped scrapes, no scrape for torrents
JOAL already announces, requests spread out."""
import http.server
import struct
import sys
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
import swarm_monitor  # noqa: E402
import tracker_scrape  # noqa: E402
from core import encode_bencode, validate, ConfigError  # noqa: E402

H = ['%040x' % (i + 1) for i in range(4)]
URL = 'https://tracker.example.org/pk/announce'
RULE = {'id': 'r', 'enabled': True, 'trackerDomains': ['tracker.example.org'], 'minLeechers': 1}


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


def monitor(clock, count=3):
    folder = tempfile.TemporaryDirectory()
    m = swarm_monitor.SwarmMonitor(Path(folder.name) / 'swarm.json', now=clock)
    m._folder = folder
    for h in H[:count]:
        m.register_solo(h, h[:6], [URL], 1000)
    return m


class AdaptiveInterval(unittest.TestCase):
    def test_factor_steps(self):
        self.assertEqual([swarm_monitor.quiet_factor(x) for x in (0, 3600, 6 * 3600, 86400)], [1, 2, 4, 8])

    def test_due_age_grows_while_quiet_and_resets_on_a_leecher(self):
        clock = Clock()
        m = monitor(clock, 1)
        record = m.entries[H[0]]
        record['history'] = {'r': [{'time': clock.now - 30 * 3600 + i * 900, 'leechers': 0, 'status': 'ok'} for i in range(8)]}
        self.assertEqual(m.due_age(record, 'r', 900, 4 * 3600), 2 * 3600)       # quiet > 24 h: 8 x 15 min
        self.assertEqual(m.due_age(record, 'r', 900, 3600), 3600)               # capped
        self.assertEqual(m.due_age(record, 'r', 900, 900), 900)                 # disabled
        record['history']['r'].append({'time': clock.now - 60, 'leechers': 2, 'status': 'ok'})
        self.assertEqual(m.due_age(record, 'r', 900, 4 * 3600), 900)

    def test_quiet_reading_stays_valid_longer(self):
        clock = Clock()
        m = monitor(clock, 1)
        record = m.entries[H[0]]
        record['history'] = {'r': [{'time': clock.now - 30 * 3600 + i * 900, 'leechers': 0, 'status': 'ok'} for i in range(8)]}
        record['observations'] = {'r': {'sampledAt': clock.now - 2 * 3600, 'attemptedAt': clock.now - 2 * 3600,
                                        'leechers': 0, 'status': 'ok'}}
        m.adaptive = (900, 4 * 3600)
        self.assertEqual(m.validity(H[0], 2700), 2700 + 2 * 3600 - 900)
        self.assertEqual(m.snapshot(2700)['items'][0]['rules']['r']['leechers'], 0)
        self.assertNotIn(H[0], m.stale_hashes(2700))

    def test_setting_validation(self):
        conf = __import__('json').loads((ROOT / 'config.example.json').read_text())
        self.assertEqual(validate(conf)['swarm']['maxIntervalMinutes'], 240)
        conf['swarm']['maxIntervalMinutes'] = 10
        with self.assertRaises(ConfigError):
            validate(conf)


class GroupedRefresh(unittest.TestCase):
    def test_one_request_for_three_torrents(self):
        clock = Clock()
        m = monitor(clock)
        calls, singles = [], []
        with patch.object(swarm_monitor, 'fetch_scrape_many',
                          lambda url, hashes: calls.append(list(hashes)) or {h: {'leechers': 2, 'seeders': 5} for h in hashes}), \
                patch.object(swarm_monitor, 'fetch_tracker_stats', lambda url, h: singles.append(h)):
            m.refresh([RULE], min_age=900)
        self.assertEqual(calls, [sorted(H[:3])])
        self.assertEqual(singles, [])
        self.assertEqual(m.requests_made, 1)
        self.assertEqual(m.entries[H[0]]['observations']['r']['leechers'], 2)

    def test_refused_group_falls_back_and_is_remembered(self):
        clock = Clock()
        m = monitor(clock)
        pauses = []

        def refuse(url, hashes):
            raise tracker_scrape.ScrapeUnavailable('Tracker inaccessible (scrape)')
        with patch.object(swarm_monitor, 'fetch_scrape_many', refuse), \
                patch.object(swarm_monitor, 'fetch_tracker_stats', lambda url, h: {'leechers': 1, 'seeders': 1}):
            m.refresh([RULE], min_age=900, pause=lambda: pauses.append(1))
        self.assertIn('tracker.example.org', m.multi_refused)
        self.assertEqual(m.requests_made, 4)          # refused group + 3 single scrapes
        self.assertEqual(len(pauses), 3)              # spaced: one pause between two requests

    def test_torrents_announced_by_joal_are_not_scraped(self):
        clock = Clock()
        m = monitor(clock, 2)
        m.pending_imports.clear()      # just imported torrents are read right away, whatever their source
        joal = {H[0]: {'leechers': 4, 'seeders': 9, 'time': clock.now - 60}}
        with patch.object(swarm_monitor, 'fetch_scrape_many', lambda *a: self.fail('aucune requête attendue')), \
                patch.object(swarm_monitor, 'fetch_tracker_stats', lambda url, h: {'leechers': 0, 'seeders': 3}):
            m.refresh([RULE], min_age=900, joal_peers=joal.get)
        self.assertEqual(m.requests_made, 1)          # only H[1]
        observation = m.entries[H[0]]['observations']['r']
        self.assertEqual((observation['leechers'], observation['source']), (4, 'Annonces JOAL'))

    def test_quiet_torrent_is_skipped_until_due(self):
        clock = Clock()
        m = monitor(clock, 1)
        m.entries[H[0]]['history'] = {'r': [{'time': clock.now - 30 * 3600 + i * 900, 'leechers': 0, 'status': 'ok'}
                                            for i in range(8)]}
        m.entries[H[0]]['observations'] = {'r': {'attemptedAt': clock.now - 3600, 'sampledAt': clock.now - 3600,
                                                 'leechers': 0, 'status': 'ok'}}
        with patch.object(swarm_monitor, 'fetch_tracker_stats', lambda url, h: {'leechers': 0, 'seeders': 1}):
            self.assertEqual(m.refresh([RULE], min_age=900, max_age=4 * 3600), 0)
            clock.now += 3 * 3600
            self.assertEqual(m.refresh([RULE], min_age=900, max_age=4 * 3600), 1)


class Scrape(http.server.BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, encoding='latin-1')
        hashes = [h.encode('latin-1') for h in query['info_hash']]
        Scrape.seen.append(len(hashes))
        files = {h: {b'complete': 3, b'incomplete': i} for i, h in enumerate(hashes[:-1])}  # last one unknown
        body = encode_bencode({b'files': files})
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class Wire(unittest.TestCase):
    def test_http_grouped_scrape(self):
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Scrape)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with patch.object(tracker_scrape, 'public_ip_only', lambda *a: '127.0.0.1'):
            result = tracker_scrape.fetch_scrape_many(f'http://t.invalid:{server.server_port}/pk/announce', H[:3])
        self.assertEqual(Scrape.seen, [3])
        self.assertEqual(result, {H[0]: {'leechers': 0, 'seeders': 3}, H[1]: {'leechers': 1, 'seeders': 3}})

    def test_udp_grouped_scrape(self):
        class Sock:
            def __init__(self, *a):
                self.sent = []

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def settimeout(self, t):
                pass

            def connect(self, d):
                pass

            def sendall(self, data):
                self.sent.append(data)

            def recv(self, size):
                last = self.sent[-1]
                txn = struct.unpack('!I', last[12:16])[0]
                if len(self.sent) == 1:
                    return struct.pack('!IIQ', 0, txn, 99)
                count = (len(last) - 16) // 20
                return struct.pack('!II', 2, txn) + b''.join(struct.pack('!III', 5, 0, i) for i in range(count))
        resolver = lambda *a, **k: [(2, 2, 17, '', ('93.184.216.34', 6969))]
        result = tracker_scrape._fetch_udp_many('udp://t.example:6969/announce', H[:3],
                                                resolver=resolver, socket_factory=Sock)
        self.assertEqual(result[H[2]], {'leechers': 2, 'seeders': 5})


if __name__ == '__main__':
    unittest.main()
