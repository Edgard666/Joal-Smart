"""Read-only token for dashboards such as Homepage, Homarr or Home Assistant (v1.32).

The token is shown once at creation; only its SHA-256 stays in /data/widget-token.json
(0600, never in backups). It opens GET /api/widget and nothing else.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

FILE = 'widget-token.json'
PREFIX = 'jsw_'


def _path(data):
    return Path(data) / FILE


def _read(data):
    try:
        value = json.loads(_path(data).read_text(encoding='utf-8'))
        return value if isinstance(value, dict) and isinstance(value.get('sha256'), str) else {}
    except (OSError, ValueError):
        return {}


def create(data):
    token = PREFIX + secrets.token_urlsafe(32)
    path = _path(data)
    temp = path.with_name(f'.{FILE}.{secrets.token_hex(4)}.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump({'sha256': hashlib.sha256(token.encode('ascii')).hexdigest(), 'created': int(time.time())}, stream)
    os.replace(temp, path)
    os.chmod(path, 0o600)
    return token


def delete(data):
    try:
        _path(data).unlink()
    except FileNotFoundError:
        pass


def check(data, supplied):
    stored = _read(data).get('sha256')
    if not stored or not isinstance(supplied, str) or not supplied.startswith(PREFIX) or len(supplied) > 100 \
            or not supplied.isascii():
        return False
    return hmac.compare_digest(hashlib.sha256(supplied.encode('ascii')).hexdigest(), stored)


def public(data):
    stored = _read(data)
    return {'configured': bool(stored), 'created': stored.get('created')}
