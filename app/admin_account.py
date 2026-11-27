"""Administrator credentials and optional two-factor authentication (v1.32).

Password sources, highest priority first:
  1. /data/admin-password.json  — set from the interface (scrypt hash, 0600)
  2. ADMIN_PASSWORD_HASH         — scrypt hash given to Docker
  3. ADMIN_PASSWORD              — clear password given to Docker (12 characters minimum)

TOTP (RFC 6238, SHA-1, 6 digits, 30 s) uses the standard library only. The secret
lives in /data/totp.json (0600) and is never part of a backup. DISABLE_TOTP=1 turns
the second factor off for recovery without deleting the secret.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import threading
import time
import urllib.parse
from pathlib import Path

PASSWORD_FILE = 'admin-password.json'
TOTP_FILE = 'totp.json'
MIN_LENGTH = 12
MAX_LENGTH = 256
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1
TOTP_STEP = 30
TOTP_DIGITS = 6
ISSUER = 'JOAL Smart Controller'


def _b64(data):
    return base64.b64encode(data).decode('ascii').rstrip('=')


def _unb64(text):
    return base64.b64decode(text + '=' * (-len(text) % 4), validate=True)


def hash_password(password, *, salt=None, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P):
    """scrypt:N:r:p:salt:hash, salt and hash in base64 without padding.

    ':' rather than '$' so the value survives docker compose .env interpolation;
    the '$' form is still accepted."""
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode('utf-8'), salt=salt, n=n, r=r, p=p,
                            maxmem=128 * 1024 * 1024, dklen=32)
    return f'scrypt:{n}:{r}:{p}:{_b64(salt)}:{_b64(digest)}'


def _parts(encoded):
    text = str(encoded).strip()
    return text.split(':' if text.startswith('scrypt:') else '$')


def valid_hash(encoded):
    try:
        kind, n, r, p, salt, digest = _parts(encoded)
        n, r, p = int(n), int(r), int(p)
        return (kind == 'scrypt' and 2 ** 10 <= n <= 2 ** 20 and n & (n - 1) == 0 and 1 <= r <= 32
                and 1 <= p <= 16 and len(_unb64(salt)) >= 8 and len(_unb64(digest)) == 32)
    except (ValueError, TypeError):
        return False


def verify_password(password, encoded):
    if not valid_hash(encoded) or not isinstance(password, str) or len(password) > MAX_LENGTH:
        return False
    _, n, r, p, salt, digest = _parts(encoded)
    try:
        computed = hashlib.scrypt(password.encode('utf-8', errors='surrogatepass'), salt=_unb64(salt),
                                  n=int(n), r=int(r), p=int(p), maxmem=128 * 1024 * 1024, dklen=32)
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(computed, _unb64(digest))


def password_problem(password):
    if not isinstance(password, str) or len(password) < MIN_LENGTH:
        return f'{MIN_LENGTH} caractères minimum'
    if len(password) > MAX_LENGTH:
        return f'{MAX_LENGTH} caractères maximum'
    if password.startswith('REMPLACER'):
        return 'Remplacer le mot de passe d’exemple'
    return None


def totp_code(secret, step):
    key = _unb32(secret)
    digest = hmac.new(key, struct.pack('>Q', step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** TOTP_DIGITS).zfill(TOTP_DIGITS)


def _unb32(secret):
    text = secret.strip().replace(' ', '').upper()
    return base64.b32decode(text + '=' * (-len(text) % 8))


def new_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode('ascii').rstrip('=')


def otpauth_uri(secret, account='admin'):
    label = urllib.parse.quote(f'{ISSUER}:{account}')
    query = urllib.parse.urlencode({'secret': secret, 'issuer': ISSUER, 'digits': TOTP_DIGITS, 'period': TOTP_STEP})
    return f'otpauth://totp/{label}?{query}'


def _write_private(path, payload):
    path = Path(path)
    temp = path.with_name(f'.{path.name}.{secrets.token_hex(4)}.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(payload, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    os.chmod(path, 0o600)


def _read(path):
    try:
        with Path(path).open(encoding='utf-8') as stream:
            data = json.load(stream)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


class Account:
    def __init__(self, data_dir, environ=None, clock=time.time, clear_password=None):
        self.data = Path(data_dir)
        self.env = os.environ if environ is None else environ
        # main.py passes its ADMIN_PASSWORD value (tests patch that value).
        self.clear_password = clear_password or (lambda: self.env.get('ADMIN_PASSWORD', ''))
        self.clock = clock
        self.lock = threading.RLock()
        self.pending_secret = None

    # ----- password -----
    def stored_hash(self):
        encoded = _read(self.data / PASSWORD_FILE).get('hash')
        return encoded if valid_hash(encoded) else None

    def source(self):
        if self.stored_hash():
            return 'interface'
        if valid_hash(self.env.get('ADMIN_PASSWORD_HASH', '')):
            return 'hash'
        if password_problem(self.clear_password()) is None:
            return 'env'
        return None

    def startup_problem(self):
        """None when a usable password is configured, else a message for the logs."""
        if self.source():
            return None
        if self.env.get('ADMIN_PASSWORD_HASH'):
            return 'ADMIN_PASSWORD_HASH illisible : format attendu scrypt:N:r:p:sel:hash'
        return 'Définir ADMIN_PASSWORD (12 caractères minimum) ou ADMIN_PASSWORD_HASH dans .env'

    def check_password(self, password):
        source = self.source()
        if source == 'interface':
            return verify_password(password, self.stored_hash())
        if source == 'hash':
            return verify_password(password, self.env['ADMIN_PASSWORD_HASH'])
        if source == 'env':
            expected = self.clear_password()
            supplied = hashlib.sha256(str(password).encode('utf-8', errors='surrogatepass')).digest()
            return hmac.compare_digest(supplied, hashlib.sha256(expected.encode('utf-8')).digest())
        return False

    def set_password(self, password):
        problem = password_problem(password)
        if problem:
            raise ValueError(problem)
        encoded = hash_password(password)
        with self.lock:
            _write_private(self.data / PASSWORD_FILE, {'hash': encoded, 'changed': int(self.clock())})
        return encoded

    # ----- TOTP -----
    def _totp(self):
        data = _read(self.data / TOTP_FILE)
        secret = data.get('secret')
        try:
            if not isinstance(secret, str) or len(_unb32(secret)) < 10:
                return {}
        except (ValueError, TypeError):
            return {}
        return data

    def totp_disabled_by_env(self):
        return self.env.get('DISABLE_TOTP', '0') == '1'

    def totp_enabled(self):
        return bool(self._totp().get('enabled')) and not self.totp_disabled_by_env()

    def _match(self, secret, code, last_step):
        if not isinstance(code, str):
            return None
        code = code.strip().replace(' ', '')
        if len(code) != TOTP_DIGITS or not code.isdigit():
            return None
        now = int(self.clock()) // TOTP_STEP
        for step in (now - 1, now, now + 1):
            if step > last_step and hmac.compare_digest(totp_code(secret, step), code):
                return step
        return None

    def check_totp(self, code):
        """Accept a code once (no replay within its validity window)."""
        with self.lock:
            data = self._totp()
            if not data.get('enabled'):
                return False
            step = self._match(data['secret'], code, int(data.get('lastStep', 0)))
            if step is None:
                return False
            data['lastStep'] = step
            _write_private(self.data / TOTP_FILE, data)
            return True

    def start_totp(self):
        with self.lock:
            self.pending_secret = new_secret()
            return {'secret': self.pending_secret, 'uri': otpauth_uri(self.pending_secret)}

    def confirm_totp(self, code):
        with self.lock:
            if not self.pending_secret:
                raise ValueError('Générer d’abord une clé')
            step = self._match(self.pending_secret, code, 0)
            if step is None:
                raise ValueError('Code incorrect : vérifier l’heure du téléphone')
            _write_private(self.data / TOTP_FILE, {'secret': self.pending_secret, 'enabled': True,
                                                   'lastStep': step, 'since': int(self.clock())})
            self.pending_secret = None

    def disable_totp(self):
        with self.lock:
            self.pending_secret = None
            try:
                (self.data / TOTP_FILE).unlink()
            except FileNotFoundError:
                pass

    def public(self):
        return {'source': self.source(), 'totpEnabled': self.totp_enabled(),
                'totpConfigured': bool(self._totp().get('enabled')),
                'totpDisabledByEnv': self.totp_disabled_by_env(), 'minLength': MIN_LENGTH}


if __name__ == '__main__':
    # docker exec -it joal-smart-controller python3 /app/admin_account.py
    import getpass
    first = getpass.getpass('Nouveau mot de passe admin : ')
    if first != getpass.getpass('Confirmer : '):
        raise SystemExit('Les deux saisies diffèrent')
    problem = password_problem(first)
    if problem:
        raise SystemExit(problem)
    print('ADMIN_PASSWORD_HASH=' + hash_password(first))
