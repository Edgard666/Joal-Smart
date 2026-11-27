"""v1.32 over HTTP: password change, other sessions closed, TOTP login, widget token,
CSV export, daily cap."""
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
sys.path.insert(0, str(ROOT / 'tests'))
import admin_account  # noqa: E402
import main  # noqa: E402
import tracker_stats  # noqa: E402
from diagnostics import DiagnosticsStore  # noqa: E402
from web_auth import SessionManager  # noqa: E402
from webtest_helpers import login, auth_headers  # noqa: E402

START = 'mot-de-passe-de-depart'


class Server(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.data = Path(folder.name)
        self.clock = [1_800_000_000]
        account = admin_account.Account(self.data, {}, clock=lambda: self.clock[0], clear_password=lambda: START)
        stats = tracker_stats.TrackerStats(self.data / 'tracker-stats.json')
        auth = SessionManager(checker=account.check_password,
                              second_factor=lambda: account.check_totp if account.totp_enabled() else None)
        for item in (patch.object(main, 'ACCOUNT', account), patch.object(main, 'AUTH', auth),
                     patch.object(main, 'DATA', self.data), patch.object(main, 'STATS', stats),
                     patch.object(main, 'DIAGNOSTICS', DiagnosticsStore(self.data / 'diagnostics.json')),
                     patch.object(main, 'safe_audit', lambda *a, **k: None),
                     patch.object(main, 'WIDGET_FAILURES', {})):
            item.start()
            self.addCleanup(item.stop)
        self.account, self.stats = account, stats
        self.server = main.ThreadingHTTPServer(('127.0.0.1', 0), main.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def call(self, path, body=None, headers=None, method=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = dict(headers or {})
        if data is not None:
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                raw = res.read()
                return res.status, (json.loads(raw) if res.headers.get_content_type() == 'application/json' else raw), res.headers
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, json.loads(raw) if raw[:1] == b'{' else raw, exc.headers

    def raw_login(self, password, code=None):
        body = {'username': 'admin', 'password': password, **({'code': code} if code else {})}
        return self.call('/api/auth/login', body)


class Account(Server):
    def test_password_change_closes_other_sessions_and_returns_hash(self):
        other = login(self.base, START)
        mine = auth_headers(*login(self.base, START))
        code, body, _ = self.call('/api/account/password', {'current': 'faux', 'new': 'x' * 12, 'confirm': 'x' * 12}, mine)
        self.assertEqual(code, 403)
        new = 'nouveau-mot-de-passe'
        code, body, _ = self.call('/api/account/password', {'current': START, 'new': new, 'confirm': new}, mine)
        self.assertEqual(code, 200, body)
        self.assertEqual(body['closed'], 1)
        self.assertTrue(admin_account.verify_password(new, body['hash']))
        self.assertEqual(self.call('/api/account', headers=mine)[1]['source'], 'interface')
        self.assertEqual(self.call('/api/account', headers=auth_headers(*other))[0], 401)
        self.assertEqual(self.raw_login(START)[0], 401)
        self.assertEqual(self.raw_login(new)[0], 200)

    def test_revoke_other_sessions(self):
        login(self.base, START)
        login(self.base, START)
        mine = auth_headers(*login(self.base, START))
        self.assertEqual(self.call('/api/account/sessions/revoke', {}, mine)[1]['closed'], 2)
        self.assertEqual(self.call('/api/account', headers=mine)[1]['sessions'], 1)

    def test_totp_setup_and_login(self):
        mine = auth_headers(*login(self.base, START))
        code, setup, _ = self.call('/api/account/totp/start', {'current': START}, mine)
        self.assertEqual(code, 200)
        totp = admin_account.totp_code(setup['secret'], self.clock[0] // 30)
        code, state, _ = self.call('/api/account/totp/confirm', {'code': totp}, mine)
        self.assertEqual(code, 200)
        self.assertTrue(state['totpEnabled'])
        code, body, _ = self.raw_login(START)
        self.assertEqual((code, body.get('totpRequired')), (401, True))
        self.assertEqual(self.raw_login(START, totp)[0], 401, 'code déjà utilisé')
        self.clock[0] += 30
        self.assertEqual(self.raw_login(START, admin_account.totp_code(setup['secret'], self.clock[0] // 30))[0], 200)


class Widget(Server):
    def test_token_lifecycle(self):
        mine = auth_headers(*login(self.base, START))
        self.assertEqual(self.call('/api/widget')[0], 401)
        code, created, _ = self.call('/api/widget/token', {}, mine)
        token = created['token']
        self.assertNotIn(token, (self.data / 'widget-token.json').read_text())
        code, body, _ = self.call('/api/widget', headers={'Authorization': 'Bearer ' + token})
        self.assertEqual(code, 200)
        self.assertIn('uploadKiBps', body)
        self.assertEqual(self.call('/api/widget?token=' + token)[0], 200)
        # The widget token opens nothing else.
        self.assertEqual(self.call('/api/status', headers={'Authorization': 'Bearer ' + token})[0], 401)
        self.call('/api/widget/token/delete', {}, mine)
        self.assertEqual(self.call('/api/widget', headers={'Authorization': 'Bearer ' + token})[0], 401)

    def test_guessing_is_throttled(self):
        for _ in range(10):
            self.call('/api/widget?token=jsw_faux')
        self.assertEqual(self.call('/api/widget?token=jsw_faux')[0], 429)


class StatsAndCap(Server):
    def test_csv_export(self):
        now = time.time()
        self.stats.add(now, {'a' * 40: 0}, {'a' * 40: 'tracker.example'})
        self.stats.add(now + 2, {'a' * 40: 2 ** 29}, {'a' * 40: 'tracker.example'})
        mine = auth_headers(*login(self.base, START))
        code, body, headers = self.call('/api/stats.csv?days=7', headers=mine)
        self.assertEqual(code, 200)
        self.assertIn('attachment', headers['Content-Disposition'])
        text = body.decode('utf-8-sig')
        self.assertTrue(text.startswith('date;tracker;octets;Gio'))
        self.assertIn(';tracker.example;1073741824;1,000;', text)

    def test_daily_cap_reason(self):
        now = time.time()
        self.stats.add(now, {'a' * 40: 0}, {'a' * 40: 'tr.example.org'})
        self.stats.add(now + 2, {'a' * 40: 2 ** 30}, {'a' * 40: 'tr.example.org'})
        rule = {'id': 'r1', 'trackerDomains': ['example.org'], 'dailyUploadCapGiB': 2}
        main._CAP_CACHE.clear()
        self.assertEqual(main.cap_reason(rule), 'Plafond du jour atteint (2,0 / 2 Gio)')
        self.assertFalse(main.rule_open(rule))
        self.assertIsNone(main.cap_reason(dict(rule, id='r2', dailyUploadCapGiB=3)))
        self.assertIsNone(main.cap_reason(dict(rule, id=main.FORCED_RULE)))
        self.assertIsNone(main.cap_reason(dict(rule, id='r3', dailyUploadCapGiB=0)))

    def test_cap_validation(self):
        from core import validate, ConfigError
        conf = json.loads((ROOT / 'config.example.json').read_text())
        conf['rules'][0]['dailyUploadCapGiB'] = -1
        with self.assertRaises(ConfigError):
            validate(conf)
        conf['rules'][0]['dailyUploadCapGiB'] = 12.5
        self.assertEqual(validate(conf)['rules'][0]['dailyUploadCapGiB'], 12.5)


class RealStartup(unittest.TestCase):
    """The real main() with only ADMIN_PASSWORD_HASH (no clear password): starts, logs in."""
    def test_controller_starts_with_a_hash_only(self):
        import os
        import socket
        import subprocess
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'policy.json').write_text((ROOT / 'config.example.json').read_text())
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                port = probe.getsockname()[1]
            env = {k: v for k, v in os.environ.items() if k not in ('ADMIN_PASSWORD', 'ADMIN_PASSWORD_HASH')}
            env.update(DATA_DIR=folder, PORT=str(port), IMPORT_WATCH='0',
                       ADMIN_PASSWORD_HASH=admin_account.hash_password('hash-only-password-1', n=2 ** 10))
            proc = subprocess.Popen([sys.executable, str(ROOT / 'app' / 'main.py')], env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                base = f'http://127.0.0.1:{port}'
                for _ in range(100):
                    try:
                        urllib.request.urlopen(base + '/healthz', timeout=1).read()
                        break
                    except OSError:
                        if proc.poll() is not None:
                            self.fail(proc.stderr.read().decode()[-2000:])
                        time.sleep(0.2)
                cookie, _ = login(base, 'hash-only-password-1')
                self.assertTrue(cookie)
            finally:
                proc.terminate()
                try:
                    proc.wait(30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                proc.stderr.close()


if __name__ == '__main__':
    unittest.main()
