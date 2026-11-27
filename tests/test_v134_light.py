"""v1.34: qBittorrent's own counts instead of scrapes, lighter guard, telemetry in RAM,
image on Ubuntu 24.04 with the tests run inside it."""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
sys.path.insert(0, str(ROOT / 'tests'))
import main  # noqa: E402
from test_v14 import policy, torrent  # noqa: E402

H = 'a' * 40


class QbitCounts(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(main.QBIT_LIVE_PEERS, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_running_torrent_with_working_tracker(self):
        trackers = [{'url': 'https://a/announce', 'status': 2, 'num_leeches': 3, 'num_seeds': 40},
                    {'url': 'https://b/announce', 'status': 4, 'num_leeches': 99, 'num_seeds': 99},   # not working
                    {'url': '** [DHT] **', 'status': 2, 'num_leeches': -1, 'num_seeds': -1}]
        main.remember_qbit_peers({'hash': H, 'state': 'uploading'}, trackers)
        self.assertEqual((main.QBIT_LIVE_PEERS[H]['leechers'], main.QBIT_LIVE_PEERS[H]['seeders']), (3, 40))
        main.remember_qbit_peers({'hash': H, 'state': 'stoppedUP'}, trackers)
        self.assertNotIn(H, main.QBIT_LIVE_PEERS)                     # stopped: its counts age, scrape again

    def test_known_peers_respects_handoff(self):
        main.QBIT_LIVE_PEERS[H] = {'leechers': 2, 'seeders': 5, 'time': time.time(), 'source': 'Annonces qBittorrent'}
        with patch.object(main, 'joal_session_peers', lambda h, age: None):
            self.assertEqual(main.known_peers(H, 2700, False)['source'], 'Annonces qBittorrent')
            self.assertIsNone(main.known_peers(H, 2700, True))        # handoff keeps requiring scrapes
            main.QBIT_LIVE_PEERS[H]['time'] -= 3600
            self.assertIsNone(main.known_peers(H, 2700, False))       # too old


class TwoPasses:
    def __init__(self):
        self.count = 0

    def is_set(self):
        return self.count >= 2

    def wait(self, duration):
        self.count += 1


class LightGuard(unittest.TestCase):
    def run_guard(self, second_answer):
        class Client:
            full_reads = 0
            by_hash = []

            def torrents(self, tag):
                Client.full_reads += 1
                return [dict(torrent(), tags='joal')]

            def torrents_by_hashes(self, hashes):
                Client.by_hash.append(list(hashes))
                return second_answer
        proc = Mock()
        proc.poll.side_effect = lambda: 0 if proc.terminate.called else None
        proc.wait.return_value = 0
        with tempfile.TemporaryDirectory() as folder:
            conf = Path(folder) / 'policy.json'
            conf.write_text(json.dumps(policy(dry=False)))
            with patch.object(main, 'CONFIG', conf), patch.object(main, 'RUNNERS', {'alpha-0': proc}), \
                    patch.object(main, 'ACTIVE_HASHES', {'alpha-0': {H}}), \
                    patch.object(main, 'STATUS', {'rules': [], 'torrents': [], 'totals': {}}), \
                    patch.object(main, 'STOP', TwoPasses()), patch.object(main, 'QBitClient', return_value=Client()), \
                    patch.object(main, 'POLL_NOW'), patch.object(main, 'HANDOFF_LEDGER', None), \
                    patch.object(main, 'CLEANUP_LEDGER', None), patch.object(main, 'save_final_volumes', lambda *a: None):
                main.GUARD_FULL_NOW.clear()
                main.guard_loop()
                stopped = proc.terminate.called
        return Client, stopped

    def test_second_pass_reads_only_the_running_torrents(self):
        client, stopped = self.run_guard([dict(torrent(), tags='joal')])
        self.assertEqual((client.full_reads, client.by_hash), (1, [[H]]))
        self.assertFalse(stopped)

    def test_resumed_torrent_still_stops_joal_within_5_seconds(self):
        _, stopped = self.run_guard([dict(torrent(state='uploading'), tags='joal')])
        self.assertTrue(stopped)

    def test_untagged_torrent_counts_as_absent(self):
        _, stopped = self.run_guard([dict(torrent(), tags='autre')])
        self.assertTrue(stopped)


class Telemetry(unittest.TestCase):
    def test_telemetry_in_ram_when_available(self):
        self.assertEqual(main._telemetry_dir(), '/dev/shm' if Path('/dev/shm').is_dir() else None)

    def test_java_writes_announce_file_only_on_change(self):
        source = (ROOT / 'telemetry' / 'UploadTelemetry.java').read_text()
        self.assertIn('Arrays.equals(announceBytes, lastAnnounces)', source)


class Image(unittest.TestCase):
    def test_ubuntu_2404_and_tests_inside_the_image(self):
        dockerfile = (ROOT / 'Dockerfile').read_text()
        self.assertIn('ARG BASE_IMAGE=ubuntu:24.04@sha256:', dockerfile)
        self.assertIn("'base_image': 'ubuntu:24.04'", (ROOT / 'ci' / 'resolve_inputs.py').read_text())
        workflow = (ROOT / '.github' / 'workflows' / 'image.yml').read_text()
        self.assertIn('Tests dans l’image', workflow)
        self.assertIn('PatchConnectionHandler.java', dockerfile)


if __name__ == '__main__':
    unittest.main()
