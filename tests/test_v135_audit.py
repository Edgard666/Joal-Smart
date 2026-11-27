"""v1.35: fixes from the external audit — no passkey in the anonymized backup, exact restore,
no silent root start, statistics retried after a write error, qBittorrent asked outside the
lock, single alert worker, config saved despite a refused chmod, shared JOAL measure, gzip
quality values, cached static files."""
import io
import json
import logging
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
import alerts  # noqa: E402
import main  # noqa: E402
import swarm_monitor  # noqa: E402
import tracker_stats  # noqa: E402
from backup_bundle import MANIFEST, apply_pending_restore, build_backup, schedule_restore  # noqa: E402
from core import encode_bencode  # noqa: E402

PASSKEY = 'a1b2c3d4e5f6SECRETPASSKEY'
H = 'ab' * 20


def seed(data):
    (data / 'policy.json').write_text((ROOT / 'config.example.json').read_text())
    (data / 'tracker-registry.json').write_text(json.dumps({'version': 1, 'entries': {H: {
        'name': 'x', 'urls': [f'https://tracker.example.org/{PASSKEY}/announce',
                              f'udp://open.example.net:6969/announce?passkey={PASSKEY}'],
        'observations': {}, 'history': {}}}}))
    (data / 'diagnostics.json').write_text(json.dumps({'events': [
        {'message': f'Échec https://tracker.example.org/announce?passkey={PASSKEY}'}]}))
    (data / 'torrents').mkdir()
    info = {b'name': b'x', b'length': 1, b'piece length': 16384, b'pieces': b'p' * 20}
    blob = encode_bencode({b'announce': f'https://tracker.example.org/{PASSKEY}/announce'.encode(), b'info': info})
    (data / 'torrents' / 'film.torrent').write_bytes(blob)


class Backup(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.data = Path(folder.name) / 'data'
        self.data.mkdir()
        seed(self.data)

    def test_anonymized_backup_holds_no_passkey(self):
        blob = build_backup(self.data, include_torrents=False)
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            names = zf.namelist()
            for name in names:
                self.assertNotIn(PASSKEY.encode(), zf.read(name), name)
            registry = json.loads(zf.read('tracker-registry.json'))
            manifest = json.loads(zf.read(MANIFEST))
        self.assertFalse(any(n.startswith('torrents/') for n in names))
        self.assertEqual(registry['entries'][H]['urls'], [])
        self.assertEqual(manifest, {'version': 1, 'torrents': False})
        with zipfile.ZipFile(io.BytesIO(build_backup(self.data))) as zf:   # the full backup keeps everything
            self.assertIn(PASSKEY.encode(), zf.read('tracker-registry.json'))

    def restore(self, blob, target):
        schedule_restore(target, blob)
        self.assertTrue(apply_pending_restore(target))

    def test_full_backup_restores_exactly(self):
        blob = build_backup(self.data)
        target = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__('shutil').rmtree(target, ignore_errors=True))
        seed(target)
        (target / 'notifications.json').write_text('{}')               # not in the archive
        (target / 'torrents' / 'autre.torrent').write_bytes((target / 'torrents' / 'film.torrent').read_bytes())
        (target / 'clients').mkdir()
        (target / 'clients' / 'perso.client').write_text('{}')        # profiles are never removed
        self.restore(blob, target)
        self.assertFalse((target / 'notifications.json').exists())
        self.assertFalse((target / 'torrents' / 'autre.torrent').exists())
        self.assertTrue((target / 'torrents' / 'film.torrent').exists())
        self.assertTrue((target / 'clients' / 'perso.client').exists())
        self.assertFalse((target / MANIFEST).exists())

    def test_anonymized_backup_keeps_the_torrents_in_place(self):
        blob = build_backup(self.data, include_torrents=False)
        target = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__('shutil').rmtree(target, ignore_errors=True))
        seed(target)
        self.restore(blob, target)
        self.assertTrue((target / 'torrents' / 'film.torrent').exists())

    def test_archive_from_before_v135_still_merges(self):
        blob = build_backup(self.data)
        old = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(blob)) as src, zipfile.ZipFile(old, 'w') as dst:
            for name in src.namelist():
                if name != MANIFEST:
                    dst.writestr(name, src.read(name))
        target = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__('shutil').rmtree(target, ignore_errors=True))
        seed(target)
        (target / 'notifications.json').write_text('{}')
        self.restore(old.getvalue(), target)
        self.assertTrue((target / 'notifications.json').exists())


class Entrypoint(unittest.TestCase):
    @unittest.skipUnless(os.geteuid() == 0, 'test du passage root -> utilisateur')
    def test_refuses_to_run_as_root_when_the_drop_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            fake = Path(folder) / 'bin'
            fake.mkdir()
            (fake / 'setpriv').write_text('#!/bin/sh\nexit 1\n')
            (fake / 'setpriv').chmod(0o755)
            env = {'PATH': f'{fake}:/usr/bin:/bin', 'DATA_DIR': str(Path(folder) / 'data')}
            script = str(ROOT / 'docker-entrypoint.sh')
            refused = subprocess.run(['sh', script, 'echo', 'LANCE'], env=env, capture_output=True, text=True)
            self.assertEqual(refused.returncode, 1)
            self.assertNotIn('LANCE', refused.stdout)
            self.assertIn('impossible de quitter root', refused.stderr)
            allowed = subprocess.run(['sh', script, 'echo', 'LANCE'], env=dict(env, ALLOW_ROOT='1'),
                                     capture_output=True, text=True)
            self.assertIn('LANCE', allowed.stdout)


class Persistence(unittest.TestCase):
    def test_statistics_retried_after_a_failed_write(self):
        with tempfile.TemporaryDirectory() as folder:
            stats = tracker_stats.TrackerStats(Path(folder) / 'stats.json')
            stats.add(1000, {H: 0}, {H: 't.example'})
            stats.add(1001, {H: 500}, {H: 't.example'})
            with patch.object(Path, 'write_text', side_effect=OSError('disque plein')):
                with self.assertRaises(OSError):
                    stats.flush()
            stats.flush()                                   # no new measure in between
            self.assertTrue((Path(folder) / 'stats.json').exists())

    def test_config_saved_even_if_chmod_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            conf = json.loads((ROOT / 'config.example.json').read_text())
            target = Path(folder) / 'policy.json'
            with patch.object(main, 'CONFIG', target), patch.object(main.os, 'chmod', side_effect=PermissionError):
                main.write_config(conf)
            self.assertEqual(json.loads(target.read_text())['rules'], conf['rules'])
            self.assertEqual(stat.S_IMODE(target.stat().st_mode) & 0o077, 0)


class Concurrency(unittest.TestCase):
    def test_qbittorrent_is_asked_outside_the_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            monitor = swarm_monitor.SwarmMonitor(Path(folder) / 'swarm.json')
            free = []

            def trackers(h):
                got = []

                def probe():
                    acquired = monitor.lock.acquire(timeout=1)
                    got.append(acquired)
                    if acquired:
                        monitor.lock.release()
                worker = threading.Thread(target=probe)
                worker.start()
                worker.join()
                free.append(got[0])
                return [{'url': 'https://t.example.org/announce'}]
            monitor.synchronize([{'hash': H, 'name': 'x'}], trackers, 'joal')
        self.assertEqual(free, [True])
        self.assertEqual(monitor.entries[H]['urls'], ['https://t.example.org/announce'])

    def test_a_single_alert_worker(self):
        delivered = []
        gate = threading.Event()

        def deliver(config, title, message, level):
            gate.wait(2)
            delivered.append(title)
        with tempfile.TemporaryDirectory() as folder:
            dispatcher = alerts.Dispatcher(folder, logging.getLogger('test'), deliver_fn=deliver)
            config = {'enabled': True, 'url': 'https://ntfy.sh/x', 'level': 'all', 'kind': 'ntfy', 'token': ''}
            before = {t for t in threading.enumerate() if t.name == 'alerts'}   # other tests' workers
            with patch.object(alerts, 'load', lambda root: config):
                threads = [threading.Thread(target=dispatcher.submit, args=(f't{i}', 'm', 'error')) for i in range(20)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                workers = [t for t in threading.enumerate() if t.name == 'alerts' and t not in before]
                gate.set()
                deadline = time.time() + 5
                while len(delivered) < 20 and time.time() < deadline:
                    time.sleep(0.05)
        self.assertLessEqual(len(workers), 1)
        self.assertEqual(len(delivered), 20)


class Http(unittest.TestCase):
    def test_gzip_quality_values(self):
        class H_:
            def __init__(self, value):
                self.headers = {'Accept-Encoding': value}
        for value, expected in (('gzip', True), ('gzip, deflate, br', True), ('gzip;q=0', False),
                                ('br, gzip; q=0.0', False), ('*', True), ('*;q=0', False), ('identity', False),
                                ('gzip;q=0.5, *;q=0', True), ('', False)):
            self.assertEqual(main._accepts_gzip(H_(value)), expected, value)

    def test_shared_measure_is_reused(self):
        calls = []
        with patch.object(main, 'live_joal_upload', lambda **k: calls.append(1) or {'bytesPerSecond': 1}), \
                patch.dict(main.LIVE_UPLOAD, {'measure': (time.monotonic(), {'bytesPerSecond': 7, 'byInfoHash': {}}),
                                              'totals': (time.monotonic(), {H: 99})}):
            result = main.shared_joal_upload()
        self.assertEqual((calls, result['bytesPerSecond'], result['totalUploadedBytes']), ([], 7, {H: 99}))
        with patch.object(main, 'live_joal_upload', lambda **k: calls.append(1) or {'bytesPerSecond': 1}), \
                patch.dict(main.LIVE_UPLOAD, {'measure': (time.monotonic() - 10, {}), 'totals': None}):
            main.shared_joal_upload()                    # loop late: direct reading
        self.assertEqual(calls, [1])

    def test_static_files_are_versioned_and_cached(self):
        sent = {}

        class Page:
            path = '/assets/app.js?v=' + main.APP_VERSION
            headers = {}

            def __init__(self):
                self.wfile = io.BytesIO()

            def send_response(self, *_):
                pass

            def send_header(self, name, value):
                sent[name] = value

            def security_headers(self, cache='no-store'):
                sent['Cache-Control'] = cache

            def end_headers(self):
                pass
        page = Page()
        main.Handler.serve_static(page, 'app.js', 'text/javascript')
        self.assertIn('immutable', sent['Cache-Control'])
        page.path = '/assets/app.js'
        main.Handler.serve_static(page, 'app.js', 'text/javascript')
        self.assertEqual(sent['Cache-Control'], 'no-cache')
        page.path = '/'
        with tempfile.TemporaryDirectory() as folder, patch.object(main, 'DATA', Path(folder)):
            main.Handler.serve_static(page, 'index.html', 'text/html')
        html = page.wfile.getvalue()
        self.assertIn(f'src="/assets/app.js?v={main.APP_VERSION}"'.encode(), html)
        self.assertEqual(sent['Cache-Control'], 'no-store')


if __name__ == '__main__':
    unittest.main()
