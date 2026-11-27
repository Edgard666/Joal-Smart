"""v1.33: torrents added to / removed from a running JOAL without restarting it, and one
JVM shared by a rule's qBittorrent and imported parts. Runs the real patched JOAL when
JOAL_JAR (or /tmp/patched-joal/joal.jar) and java are available; skipped otherwise."""
import hashlib
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
sys.path.insert(0, str(ROOT / 'ci'))
import main  # noqa: E402
import smoke_image  # noqa: E402
from core import encode_bencode  # noqa: E402

JAR = Path(os.environ.get('JOAL_JAR', '/tmp/patched-joal/joal.jar'))
RULE = {'id': 'regle', 'enabled': True, 'trackerDomains': ['127.0.0.1'], 'minLeechers': 0, 'minLeechersByHash': {},
        'minUploadRate': 30, 'maxUploadRate': 160, 'simultaneousSeed': 5, 'targetRatio': 0,
        'client': 'qbittorrent-5.3.0.client'}


@unittest.skipUnless(JAR.is_file() and shutil.which('java'), 'JOAL réel indisponible')
class HotSync(unittest.TestCase):
    def setUp(self):
        self.server, self.seen = smoke_image.fake_tracker()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.data = Path(folder.name)
        shutil.copytree(ROOT / 'clients', self.data / 'clients')
        for item in (patch.object(main, 'DATA', self.data), patch.object(main, 'JAR', JAR),
                     patch.object(main, 'RUNNERS', {}), patch.object(main, 'ACTIVE_HASHES', {}),
                     patch.object(main, 'LAST_START', {}), patch.object(main, 'TORRENT_MODES', {}),
                     patch.object(main, 'simulations_paused', lambda: False),
                     patch.object(main, 'safe_audit', lambda *a, **k: None),
                     patch.dict(os.environ, {'JOAL_JAVA_OPTS': ''})):
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(self.stop_all)

    def stop_all(self):
        with main.RUNNER_LOCK:
            for key in list(main.RUNNERS):
                main.runner_stop(key)

    def torrent(self, name):
        info = {b'name': name.encode(), b'length': 734003200, b'piece length': 1048576,
                b'pieces': hashlib.sha1(name.encode()).digest() * 700}
        blob = encode_bencode({b'announce': f'http://127.0.0.1:{self.server.server_port}/announce'.encode(),
                               b'info': info})
        return hashlib.sha1(encode_bencode(info)).hexdigest(), blob

    def events(self):
        result = []
        for request in self.seen:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(request['path']).query, encoding='latin-1')
            result.append((query['info_hash'][0].encode('latin-1').hex(), query.get('event', [''])[0],
                           query['peer_id'][0]))
        return result

    def wait_for(self, check, seconds=60):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if check():
                return
            time.sleep(0.5)
        self.fail(f'délai dépassé ; annonces : {self.events()}')

    def test_add_remove_and_share_without_restart(self):
        a, b, c = (self.torrent(n) for n in ('alpha', 'beta', 'gamma'))
        with main.RUNNER_LOCK:
            main.runner_start(RULE, {a[0]: a[1]})
        proc = main.RUNNERS['regle']
        self.wait_for(lambda: any(e[0] == a[0] and e[1] == 'started' for e in self.events()))
        peer = self.events()[0][2]

        # 1. Add B: announced alone, same JVM, same peer_id, A not re-announced.
        with main.RUNNER_LOCK:
            self.assertTrue(main.runner_hot_update(RULE, {a[0]: a[1], b[0]: b[1]}))
        self.wait_for(lambda: any(e[0] == b[0] and e[1] == 'started' for e in self.events()))
        self.assertIs(main.RUNNERS['regle'], proc)
        self.assertIsNone(proc.poll())
        self.assertEqual({e[2] for e in self.events()}, {peer})
        self.assertEqual([e for e in self.events() if e[0] == a[0]], [(a[0], 'started', peer)])

        # 2. The imported part of the same rule joins this JVM.
        with main.RUNNER_LOCK:
            self.assertTrue(main.runner_hot_update(dict(RULE, id='solo-regle'), {c[0]: c[1]}))
        self.assertIs(main.RUNNERS['solo-regle'], proc)
        self.wait_for(lambda: any(e[0] == c[0] and e[1] == 'started' for e in self.events()))

        # 3. Remove A: one stopped announce for A only.
        with main.RUNNER_LOCK:
            self.assertTrue(main.runner_hot_update(RULE, {b[0]: b[1]}))
        self.wait_for(lambda: (a[0], 'stopped', peer) in self.events())
        self.assertNotIn((b[0], 'stopped', peer), self.events())
        # v1.33: the identity JOAL used for A is kept for the controller's later readings.
        kept = main.JOAL_IDENTITIES[a[0]]
        self.assertEqual(kept['peer'].encode('latin-1').hex(), peer.encode('latin-1').hex())
        self.assertEqual(kept['client'], 'qbittorrent-5.3.0.client')
        borrowed = main.probe_identity(a[0])
        self.assertEqual(borrowed.peer_id, peer)
        self.assertEqual(borrowed.PORT, kept['port'])

        # 4. The qBittorrent part leaves: the JVM keeps running for the imported part.
        with main.RUNNER_LOCK:
            main.runner_stop('regle')
        self.wait_for(lambda: (b[0], 'stopped', peer) in self.events())
        self.assertIsNone(proc.poll())
        self.assertEqual(main.ACTIVE_HASHES, {'solo-regle': {c[0]}})

        # 5. A rate change needs a new JVM: refused as a hot update, process stopped.
        with main.RUNNER_LOCK:
            self.assertFalse(main.runner_hot_update(dict(RULE, id='solo-regle', maxUploadRate=200), {c[0]: c[1]}))
        self.assertIsNotNone(proc.poll())
        self.assertEqual(main.RUNNERS, {})
        starts = [e for e in self.events() if e[1] == 'started']
        self.assertEqual(len(starts), 3, starts)   # exactly one start per torrent, no restart burst


class ClientAlignment(unittest.TestCase):
    NAMES = ['qbittorrent-5.2.3.client', 'qbittorrent-5.2.4.client', 'qbittorrent-5.3.0.client', 'transmission-4.0.6.client']

    def test_suggested_profile(self):
        self.assertEqual(main.suggested_profile('v5.3.0', self.NAMES), 'qbittorrent-5.3.0.client')
        self.assertEqual(main.suggested_profile('v5.2.9', self.NAMES), 'qbittorrent-5.2.4.client')
        self.assertIsNone(main.suggested_profile('v4.6.2', self.NAMES))
        self.assertIsNone(main.suggested_profile('inconnu', self.NAMES))

    def test_client_check_lists_rules_simulating_another_client(self):
        class Client:
            def version(self):
                return 'v5.2.4'
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'clients').mkdir()
            for name in self.NAMES:
                (Path(folder) / 'clients' / name).write_text('{}')
            conf = {'rules': [dict(RULE, id='a'), dict(RULE, id='b', client='qbittorrent-5.2.4.client'),
                              dict(RULE, id='c', enabled=False)]}
            with patch.object(main, 'DATA', Path(folder)), \
                    patch.dict(main.QBIT_VERSION, {'generation': None, 'checked': 0, 'version': None}):
                check = main.client_check(Client(), conf)
        self.assertEqual(check, {'qbitVersion': '5.2.4', 'suggested': 'qbittorrent-5.2.4.client', 'exact': True,
                                 'mismatched': ['a']})

    def test_borrowed_identity_keeps_the_profile(self):
        import client_identity
        base = client_identity.load(ROOT / 'clients' / 'qbittorrent-5.3.0.client')
        clone = base.borrowed('ab' * 20, '-qB5300-abcdefghijkl', 'C0FFEE12', 49153)
        self.assertEqual((clone.peer_id, clone.key('ab' * 20), clone.PORT), ('-qB5300-abcdefghijkl', 'C0FFEE12', 49153))
        self.assertNotEqual(base.peer_id, clone.peer_id)
        self.assertEqual(clone.headers, base.headers)
        self.assertIn('peer_id=-qB5300-abcdefghijkl', clone.announce_query('ab' * 20, 0))


class HotSyncRules(unittest.TestCase):
    def test_config_key_ignores_what_joal_does_not_read(self):
        key = main.joal_config_key(RULE)
        self.assertEqual(key, main.joal_config_key(dict(RULE, schedule=None, dailyUploadCapGiB=3, minLeechers=9)))
        self.assertNotEqual(key, main.joal_config_key(dict(RULE, maxUploadRate=161)))

    def test_heap_limit(self):
        self.assertEqual(main.joal_heap_limit(['-Xmx448m', '-XX:+UseSerialGC']), 448)
        self.assertEqual(main.joal_heap_limit(['-Xmx1g']), 1024)
        self.assertEqual(main.joal_heap_limit(['-XX:+UseSerialGC']), float('inf'))


if __name__ == '__main__':
    unittest.main()
