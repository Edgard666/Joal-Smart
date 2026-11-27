"""v1.32: scrypt hash, password from the interface, global logout, TOTP."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
import admin_account as aa  # noqa: E402
from web_auth import SessionManager  # noqa: E402


class Clock:
    def __init__(self, now=1_800_000_000):
        self.now = now

    def __call__(self):
        return self.now


def rfc_code(secret_b32, t):
    return aa.totp_code(secret_b32, t // 30)


class Hashes(unittest.TestCase):
    def test_round_trip_and_wrong_password(self):
        encoded = aa.hash_password('un-mot-de-passe-long', n=2 ** 10)
        self.assertTrue(encoded.startswith('scrypt:1024:8:1:'))
        # The '$' form (older documentation, other tools) is accepted too.
        self.assertTrue(aa.verify_password('un-mot-de-passe-long', encoded.replace(':', '$')))
        self.assertTrue(aa.verify_password('un-mot-de-passe-long', encoded))
        self.assertFalse(aa.verify_password('un-mot-de-passe-lonG', encoded))

    def test_malformed_hashes_never_verify(self):
        for bad in ('', 'scrypt$', 'scrypt:1024:8:1::', 'scrypt$1000$8$1$AAAA$AAAA', 'bcrypt$x', None):
            self.assertFalse(aa.verify_password('x' * 12, bad))

    def test_rfc6238_vector(self):
        # RFC 6238 appendix B, SHA-1, T=59 -> 94287082 (8 digits); 6 digits -> 287082.
        secret = __import__('base64').b32encode(b'12345678901234567890').decode()
        self.assertEqual(rfc_code(secret, 59), '287082')


class AccountSources(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.data = Path(self.dir.name)

    def test_priority_interface_then_hash_then_env(self):
        env = {'ADMIN_PASSWORD': 'mot-de-passe-docker', 'ADMIN_PASSWORD_HASH': aa.hash_password('mot-de-passe-hache', n=2 ** 10)}
        account = aa.Account(self.data, env)
        self.assertEqual(account.source(), 'hash')
        self.assertTrue(account.check_password('mot-de-passe-hache'))
        self.assertFalse(account.check_password('mot-de-passe-docker'))
        account.set_password('mot-de-passe-interface')
        self.assertEqual(account.source(), 'interface')
        self.assertTrue(account.check_password('mot-de-passe-interface'))
        self.assertEqual(os.stat(self.data / aa.PASSWORD_FILE).st_mode & 0o777, 0o600)
        self.assertNotIn('mot-de-passe-interface', (self.data / aa.PASSWORD_FILE).read_text())

    def test_startup_problems(self):
        self.assertIn('ADMIN_PASSWORD', aa.Account(self.data, {'ADMIN_PASSWORD': 'court'}).startup_problem())
        self.assertIn('illisible', aa.Account(self.data, {'ADMIN_PASSWORD_HASH': 'abc'}).startup_problem())
        self.assertIsNone(aa.Account(self.data, {'ADMIN_PASSWORD': 'x' * 12}).startup_problem())

    def test_short_password_refused(self):
        with self.assertRaises(ValueError):
            aa.Account(self.data, {}).set_password('court')


class Totp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.clock = Clock()
        self.account = aa.Account(self.dir.name, {'ADMIN_PASSWORD': 'x' * 12}, clock=self.clock)

    def enable(self):
        setup = self.account.start_totp()
        self.assertTrue(setup['uri'].startswith('otpauth://totp/'))
        self.account.confirm_totp(rfc_code(setup['secret'], self.clock.now))
        return setup['secret']

    def test_confirm_requires_a_valid_code(self):
        self.account.start_totp()
        with self.assertRaises(ValueError):
            self.account.confirm_totp('000000')
        self.assertFalse(self.account.totp_enabled())

    def test_code_accepted_once_and_window(self):
        secret = self.enable()
        self.clock.now += 30
        code = rfc_code(secret, self.clock.now)
        self.assertTrue(self.account.check_totp(code))
        self.assertFalse(self.account.check_totp(code), 'rejeu refusé')
        self.clock.now += 30
        self.assertTrue(self.account.check_totp(rfc_code(secret, self.clock.now - 30 + 60)))  # +1 pas
        self.assertFalse(self.account.check_totp(rfc_code(secret, self.clock.now + 300)))

    def test_env_recovery_switch(self):
        self.enable()
        self.account.env['DISABLE_TOTP'] = '1'
        self.assertFalse(self.account.totp_enabled())

    def test_secret_file_private(self):
        self.enable()
        path = Path(self.dir.name) / aa.TOTP_FILE
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertTrue(json.loads(path.read_text())['enabled'])


class Sessions(unittest.TestCase):
    def test_second_factor_and_revoke_all(self):
        codes = {'123456'}
        manager = SessionManager(checker=lambda p: p == 'bon-mot-de-passe',
                                 second_factor=lambda: (lambda c: c in codes))
        self.assertEqual(manager.create('admin', 'bon-mot-de-passe', 'a')[0], 'totp')
        self.assertEqual(manager.create('admin', 'bon-mot-de-passe', 'a', '999999')[0], 'invalid')
        self.assertEqual(manager.create('admin', 'mauvais', 'a', '123456')[0], 'invalid')
        state, token, _ = manager.create('admin', 'bon-mot-de-passe', 'a', '123456')
        self.assertEqual(state, 'ok')
        manager.create('admin', 'bon-mot-de-passe', 'b', '123456')
        mine = manager.get(f'joal_session={token}')
        self.assertEqual(manager.revoke_all(keep_id=mine['id']), 1)
        self.assertIsNotNone(manager.get(f'joal_session={token}'))
        self.assertEqual(manager.count(), 1)

    def test_wrong_user_refused_with_checker(self):
        manager = SessionManager(checker=lambda p: True)
        self.assertEqual(manager.create('root', 'x', 'a')[0], 'invalid')


if __name__ == '__main__':
    unittest.main()
