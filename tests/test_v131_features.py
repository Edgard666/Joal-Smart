"""v1.31: JOAL without web server, orphan settings, backup without .torrent, external alerts,
per-tracker statistics, « Nouveautés », release notes and the hardened workflow."""
import datetime as dt
import http.server
import json
import re
import sys
import tempfile
import threading
import time
import unittest
import zipfile
import io
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
sys.path.insert(0, str(ROOT / 'ci'))
import main  # noqa: E402
import alerts  # noqa: E402
import changelog  # noqa: E402
import release_notes  # noqa: E402
import tracker_stats  # noqa: E402
from backup_bundle import build_backup, permitted  # noqa: E402
from notifications import NotificationCenter  # noqa: E402


class JoalWithoutWebServer(unittest.TestCase):
    def test_session_command_disables_the_embedded_web_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            (data / 'clients').mkdir()
            (data / 'clients' / 'demo.client').write_text('{}')
            launched = []

            class Proc:
                stdout = None
                def poll(self):
                    return None
            rule = {'id': 'web', 'minUploadRate': 30, 'maxUploadRate': 6250, 'simultaneousSeed': 1, 'client': 'demo.client'}
            with patch.object(main, 'DATA', data), patch.object(main, 'RUNNERS', {}), patch.object(main, 'ACTIVE_HASHES', {}), \
                    patch.object(main, 'LAST_START', {}), patch.object(main, 'TORRENT_MODES', {}), \
                    patch.object(main, 'simulations_paused', lambda: False), patch.object(main, 'safe_audit', lambda *a, **k: None), \
                    patch.object(main.subprocess, 'Popen', lambda command, **kw: launched.append(command) or Proc()):
                main.runner_start(rule, {'a' * 40: b'd4:infod4:name1:xee'})
            self.assertIn('--spring.main.web-application-type=none', launched[0])
        self.assertIn('--spring.main.web-application-type=none', (ROOT / 'ci' / 'smoke_image.py').read_text())

    def test_gitattributes_forces_linux_line_endings(self):
        text = (ROOT / '.gitattributes').read_text()
        self.assertIn('* text=auto eol=lf', text)
        self.assertIn('*.woff2 binary', text)


class OrphanSettings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.library = type('Lib', (), {'list': lambda _self, rules: [{'hash': 'a' * 40}]})()
        conf = {'rules': [{'id': 'r', 'minLeechersByHash': {'a' * 40: 2, 'b' * 40: 3}}],
                'torrentRuleOverrides': {'c' * 40: 'r'}}
        self.forgotten = []
        for item in (patch.object(main, 'SOLO_LIBRARY', self.library), patch.object(main, 'TORRENT_MODES', {'d' * 40: 'stop'}),
                     patch.object(main, 'read_config', lambda: conf)):
            item.start()
            self.addCleanup(item.stop)

    def test_only_hashes_missing_from_the_library_are_candidates(self):
        self.assertEqual(main.orphan_setting_hashes(main.read_config()), {'b' * 40, 'c' * 40, 'd' * 40})

    def test_retries_while_qbittorrent_cannot_be_checked(self):
        answers = iter([False, False, True])
        with patch.object(main, 'forget_torrent_settings', lambda hashes: self.forgotten.append(set(hashes)) or next(answers)), \
                patch.object(main.STOP, 'wait', lambda delay: False):
            main.prune_orphan_settings(attempts=5, delay=0)
        self.assertEqual(len(self.forgotten), 3)
        self.assertEqual(self.forgotten[-1], {'b' * 40, 'c' * 40, 'd' * 40})


class BackupWithoutTorrents(unittest.TestCase):
    def test_settings_only_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            (data / 'policy.json').write_bytes((ROOT / 'config.example.json').read_bytes())
            (data / 'torrents').mkdir()
            (data / 'torrents' / ('a' * 40 + '.torrent')).write_bytes(b'd8:announce30:https://t.example/PASSKEY4:infod4:name1:xee')
            (data / 'alerts.json').write_text('{"token": "secret"}')
            (data / 'tracker-stats.json').write_text('{"version": 1, "days": {}}')
            full = zipfile.ZipFile(io.BytesIO(build_backup(data))).namelist()
            light = zipfile.ZipFile(io.BytesIO(build_backup(data, include_torrents=False))).namelist()
        self.assertTrue(any(n.startswith('torrents/') for n in full))
        self.assertFalse(any(n.startswith('torrents/') for n in light))
        self.assertIn('tracker-stats.json', light)
        self.assertNotIn('alerts.json', full)               # the alert token never leaves /data
        self.assertFalse(permitted('alerts.json'))


class AlertSettings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_token_is_kept_hidden_and_preserved(self):
        result = alerts.save(self.root, {'enabled': True, 'kind': 'ntfy', 'url': 'https://ntfy.sh/sujet', 'token': 'tk_secret'})
        self.assertEqual(result, {'enabled': True, 'kind': 'ntfy', 'url': 'https://ntfy.sh/sujet', 'level': 'problems', 'hasToken': True})
        alerts.save(self.root, {'level': 'all'})
        self.assertEqual(alerts.load(self.root)['token'], 'tk_secret')
        alerts.save(self.root, {'token': ''})
        self.assertFalse(alerts.public(self.root)['hasToken'])
        self.assertEqual((self.root / 'alerts.json').stat().st_mode & 0o777, 0o600)

    def test_invalid_settings_refused(self):
        for body in ({'kind': 'sms'}, {'url': 'ftp://x'}, {'url': 'https://user:pw@x.example/t'},
                     {'enabled': True, 'url': ''}, {'enabled': True, 'kind': 'gotify', 'url': 'https://g.example'},
                     {'unexpected': 1}, {'token': 'a\nb'}):
            with self.assertRaises(ValueError, msg=body):
                alerts.save(self.root, body)

    def test_request_formats(self):
        ntfy = alerts.request_for({'kind': 'ntfy', 'url': 'https://ntfy.sh/sujet', 'token': 'tk'}, 'Titre é', 'Message', 'error')
        self.assertIn('title=Titre+%C3%A9', ntfy.full_url)
        self.assertIn('priority=4', ntfy.full_url)
        self.assertEqual(ntfy.get_header('Authorization'), 'Bearer tk')
        self.assertEqual(ntfy.data, b'Message')
        gotify = alerts.request_for({'kind': 'gotify', 'url': 'https://g.example/', 'token': 'app'}, 'T', 'M', 'success')
        self.assertEqual(gotify.full_url, 'https://g.example/message')
        self.assertEqual(gotify.get_header('X-gotify-key'), 'app')
        self.assertEqual(json.loads(gotify.data)['priority'], 4)
        discord = alerts.request_for({'kind': 'discord', 'url': 'https://discord.com/api/webhooks/1/x', 'token': ''}, 'T', 'M', 'error')
        body = json.loads(discord.data)
        self.assertIn('**T**', body['content'])
        self.assertEqual(body['allowed_mentions'], {'parse': []})         # no @everyone from a torrent name

    def test_delivery_to_a_real_http_endpoint(self):
        seen = {}

        class Ntfy(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen['path'] = self.path
                seen['body'] = self.rfile.read(int(self.headers['Content-Length'])).decode()
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass
        server = http.server.HTTPServer(('127.0.0.1', 0), Ntfy)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        config = {'kind': 'ntfy', 'url': f'http://127.0.0.1:{server.server_port}/joal', 'token': ''}
        alerts.deliver(config, 'JOAL Smart : problème', 'Session JOAL arrêtée', 'error')
        self.assertTrue(seen['path'].startswith('/joal?title='))
        self.assertEqual(seen['body'], 'Session JOAL arrêtée')
        with self.assertRaises(OSError) as error:
            alerts.deliver({'kind': 'ntfy', 'url': 'http://127.0.0.1:9/x', 'token': 'SECRET'}, 'T', 'M', 'error')
        self.assertNotIn('SECRET', str(error.exception))

    def test_dispatcher_follows_the_chosen_level(self):
        sent = []
        log = type('Log', (), {'warning': lambda *a: None})()
        dispatcher = alerts.Dispatcher(self.root, log, deliver_fn=lambda c, t, m, l: sent.append((t, l)))
        self.assertFalse(dispatcher.submit('T', 'M', 'error'))             # disabled
        alerts.save(self.root, {'enabled': True, 'kind': 'ntfy', 'url': 'https://ntfy.sh/x'})
        self.assertTrue(dispatcher.submit('T', 'M', 'error'))
        self.assertFalse(dispatcher.submit('T', 'M', 'success'))           # problems only
        alerts.save(self.root, {'level': 'all'})
        self.assertTrue(dispatcher.submit('T', 'M', 'info'))
        dispatcher.thread.join(timeout=0.1)
        deadline = time.time() + 3
        while len(sent) < 2 and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual([level for _, level in sent], ['error', 'info'])

    def test_notification_center_feeds_the_listeners(self):
        center = NotificationCenter(self.root / 'notifications.json')
        received = []
        center.listeners.append(received.append)
        center.observe('joal-session-x', True, 'Session arrêtée', 'Session rétablie')
        center.observe('joal-session-x', True, 'Session arrêtée', 'Session rétablie')     # no transition
        center.observe('joal-session-x', False, 'Session arrêtée', 'Session rétablie')
        center.post('update', 'info', 'Mis à jour')
        self.assertEqual([(i['level'], i['message']) for i in received],
                         [('error', 'Session arrêtée'), ('success', 'Session rétablie'), ('info', 'Mis à jour')])


class TrackerStatistics(unittest.TestCase):
    def test_daily_totals_per_tracker(self):
        with tempfile.TemporaryDirectory() as tmp:
            stats = tracker_stats.TrackerStats(Path(tmp) / 'tracker-stats.json')
            # Fixed time of day: the one-hour pause below must stay on the same day.
            start = dt.datetime.combine(dt.date.today(), dt.time(9, 0)).timestamp()
            domains = {'a' * 40: 'v3x.club', 'b' * 40: 'tracker.example.org'}
            stats.add(start, {}, domains)
            for second in range(1, 11):
                stats.add(start + second, {'a' * 40: 1000, 'b' * 40: 500, 'c' * 40: None}, domains)
            stats.add(start + 3600, {'a' * 40: 1000}, domains)               # after a pause: 5 s credited at most
            summary = stats.summary(30, today=dt.date.today())
            rows = {row['domain']: row for row in summary['trackers']}
            self.assertEqual(rows['v3x.club']['today'], {'bytes': 15000, 'seconds': 15})
            self.assertEqual(rows['tracker.example.org']['month'], {'bytes': 5000, 'seconds': 10})
            self.assertEqual(rows['v3x.club']['torrents'], 1)
            self.assertEqual(summary['totals']['today'], 20000)
            self.assertEqual(summary['daily'][-1], 20000)
            self.assertNotIn('autre', rows)                                  # missing measure: not counted
            stats.flush()
            again = tracker_stats.TrackerStats(Path(tmp) / 'tracker-stats.json')
            self.assertEqual(again.summary(30)['totals']['today'], 20000)

    def test_old_days_are_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            stats = tracker_stats.TrackerStats(Path(tmp) / 'tracker-stats.json')
            old = (dt.date.today() - dt.timedelta(days=120)).isoformat()
            stats.days[old] = {'x': {'bytes': 1, 'seconds': 1, 'torrents': set()}}
            stats.add(time.time(), {'a' * 40: 10}, {})
            stats.add(time.time() + 1, {'a' * 40: 10}, {})
            stats.flush()
            saved = json.loads((Path(tmp) / 'tracker-stats.json').read_text())
            self.assertNotIn(old, saved['days'])
            self.assertIn('autre', saved['days'][dt.date.today().isoformat()])


class WhatsNew(unittest.TestCase):
    TEXT = """# Journal des versions

## v1.31.0 · Grande version

- **Gras** et `code` retirés
  suite de la ligne
- Deuxième point

## v1.30.2 · Retrait groupé

- Bouton

## v1.30.1 · Ancienne

- Vieux
"""

    def test_sections_newer_than_the_seen_version(self):
        sections = changelog.since(self.TEXT, '1.30.1', '1.31.0')
        self.assertEqual([s['version'] for s in sections], ['1.31.0', '1.30.2'])
        self.assertEqual(sections[0]['items'][0], 'Gras et code retirés suite de la ligne')
        self.assertEqual(changelog.since(self.TEXT, '1.31.0', '1.31.0'), [])
        self.assertEqual(len(changelog.since(self.TEXT, '0', '1.31.0')), 3)

    def test_real_changelog_has_the_current_version(self):
        self.assertEqual(changelog.parse(changelog.read(main.CHANGELOG_PATH))[0]['version'], main.APP_VERSION)

    def test_update_announced_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            center = NotificationCenter(Path(tmp) / 'notifications.json')
            (Path(tmp) / 'last-version.json').write_text('{"version": "1.30.2"}')
            with patch.object(main, 'DATA', Path(tmp)), patch.object(main, 'NOTIFICATIONS', center):
                main.announce_new_version()
                main.announce_new_version()
            messages = [item['message'] for item in center.items]
            self.assertEqual(len(messages), 1)
            self.assertIn(f'v1.30.2 → v{main.APP_VERSION}', messages[0])

    def test_first_install_announces_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            center = NotificationCenter(Path(tmp) / 'notifications.json')
            with patch.object(main, 'DATA', Path(tmp)), patch.object(main, 'NOTIFICATIONS', center):
                main.announce_new_version()
            self.assertEqual(center.items, [])


class ReleaseAndWorkflow(unittest.TestCase):
    def test_release_notes(self):
        text = release_notes.notes(WhatsNew.TEXT, '1.30.2', '2.1.37', 'eclipse-temurin:21-jre@sha256:x', '96')
        self.assertIn('## Retrait groupé', text)
        self.assertIn('- JOAL 2.1.37', text)
        self.assertIn('Reconstruction', release_notes.notes(WhatsNew.TEXT, '9.9.9', '2.1.37', 'x', '1'))

    def test_workflow_is_pinned_and_checks_before_publishing(self):
        workflow = (ROOT / '.github' / 'workflows' / 'image.yml').read_text()
        for line in re.findall(r'uses:\s*(\S+)', workflow):
            self.assertRegex(line, r'@[0-9a-f]{40}$', line)
        order = [workflow.index(step) for step in ('node --check', 'tests/browser_*.py', 'docker build',
                                                   'ci/smoke_image.py', 'docker push', 'gh release create')]
        self.assertEqual(order, sorted(order))
        self.assertIn('contents: write', workflow)


if __name__ == '__main__':
    unittest.main()
