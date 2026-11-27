"""v1.34: JOAL announces the real qBittorrent port and no longer asks public IP sites."""
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
sys.path.insert(0, str(ROOT / 'tests'))
import main  # noqa: E402
import smoke_image  # noqa: E402
from test_v133_hot_sync import JAR, RULE  # noqa: E402


class PortSetting(unittest.TestCase):
    def test_announced_port_follows_the_option(self):
        with patch.dict(main.QBIT_PORT, {'port': 51413}), patch.object(main, 'QBIT_URL', 'http://qbit:8080'):
            with patch.object(main, 'read_config', lambda: {'announceQbitPort': True}):
                self.assertEqual(main.announced_port(), 51413)
                key = main.joal_config_key(RULE)
            with patch.object(main, 'read_config', lambda: {'announceQbitPort': False}):
                self.assertIsNone(main.announced_port())
                self.assertNotEqual(main.joal_config_key(RULE), key)   # a port change needs a new JVM
        with patch.dict(main.QBIT_PORT, {'port': 51413}), patch.object(main, 'QBIT_URL', ''):
            self.assertIsNone(main.announced_port())                     # JOAL only: nothing to copy

    def test_listen_port_from_preferences(self):
        import qbit
        client = qbit.QBitClient('http://qbit:8080', '', '')
        with patch.object(client, 'call', lambda path: b'{"listen_port": 51413, "upnp": true}'):
            self.assertEqual(client.listen_port(), 51413)
        with patch.object(client, 'call', lambda path: b'{"listen_port": 0}'):
            self.assertIsNone(client.listen_port())

    def test_validation(self):
        import json
        from core import validate, ConfigError
        conf = json.loads((ROOT / 'config.example.json').read_text())
        self.assertTrue(validate(conf)['announceQbitPort'])
        conf['announceQbitPort'] = 'oui'
        with self.assertRaises(ConfigError):
            validate(conf)


@unittest.skipUnless(JAR.is_file() and shutil.which('java'), 'JOAL réel indisponible')
class RealJoal(unittest.TestCase):
    def test_session_announces_qbittorrent_port_without_ip_lookup(self):
        server, seen = smoke_image.fake_tracker()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        blob, infohash = smoke_image.test_torrent(server.server_port)
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            shutil.copytree(ROOT / 'clients', data / 'clients')
            with patch.object(main, 'DATA', data), patch.object(main, 'JAR', JAR), \
                    patch.object(main, 'RUNNERS', {}), patch.object(main, 'ACTIVE_HASHES', {}), \
                    patch.object(main, 'LAST_START', {}), patch.object(main, 'TORRENT_MODES', {}), \
                    patch.object(main, 'simulations_paused', lambda: False), \
                    patch.object(main, 'safe_audit', lambda *a, **k: None), \
                    patch.object(main, 'QBIT_URL', 'http://qbit:8080'), patch.dict(main.QBIT_PORT, {'port': 51413}), \
                    patch.object(main, 'read_config', lambda: {'announceQbitPort': True}), \
                    patch.dict(os.environ, {'JOAL_JAVA_OPTS': ''}):
                try:
                    with main.RUNNER_LOCK:
                        main.runner_start(RULE, {infohash: blob})
                    deadline = time.time() + 60
                    while not seen and time.time() < deadline:
                        time.sleep(0.5)
                    self.assertTrue(seen, 'aucune annonce')
                    query = urllib.parse.parse_qs(urllib.parse.urlsplit(seen[0]['path']).query)
                    self.assertEqual(query['port'], ['51413'])
                    log = (data / 'runtime' / RULE['id'] / 'joal.log').read_text(errors='replace')
                    self.assertNotIn('Fetching ip from', log)
                    self.assertNotIn('Successfully fetched public IP', log)
                    self.assertIn('IP reported to tracker will be: 127.0.0.1', log)
                finally:
                    with main.RUNNER_LOCK:
                        for key in list(main.RUNNERS):
                            main.runner_stop(key)


if __name__ == '__main__':
    unittest.main()
