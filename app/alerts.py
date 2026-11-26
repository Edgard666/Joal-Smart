"""External alerts (v1.31): ntfy, Gotify or Discord, sent in the background.

Settings live in /data/alerts.json (0600). The token is never returned to the WebUI and the
file is not part of backups. Delivery never blocks the controller: a bounded queue, one
worker, 10 s per request, no redirects, no proxy.
"""
import json
import os
import queue
import re
import threading
import urllib.parse
import urllib.request
from pathlib import Path

KINDS = ('ntfy', 'gotify', 'discord')
LEVELS = ('problems', 'all')
FILE = 'alerts.json'
DEFAULT = {'enabled': False, 'kind': 'ntfy', 'url': '', 'token': '', 'level': 'problems'}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _path(root):
    return Path(root) / FILE


def load(root):
    try:
        data = json.loads(_path(root).read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            raise ValueError
    except (OSError, ValueError):
        return dict(DEFAULT)
    config = dict(DEFAULT)
    config.update({key: data[key] for key in DEFAULT if key in data})
    return config


def public(root):
    config = load(root)
    return {'enabled': bool(config['enabled']), 'kind': config['kind'], 'url': config['url'],
            'level': config['level'], 'hasToken': bool(config['token'])}


def _valid_url(url):
    parts = urllib.parse.urlsplit(url)
    return (parts.scheme in ('http', 'https') and bool(parts.hostname) and parts.username is None
            and parts.password is None and len(url) <= 500 and not re.search(r'\s', url))


def save(root, body):
    """Validate and store. A missing 'token' keeps the stored one; '' clears it."""
    if not isinstance(body, dict) or not set(body) <= {'enabled', 'kind', 'url', 'token', 'level'}:
        raise ValueError('Paramètres inattendus')
    old = load(root)
    config = {'enabled': bool(body.get('enabled', old['enabled'])), 'kind': body.get('kind', old['kind']),
              'url': str(body.get('url', old['url'])).strip(), 'level': body.get('level', old['level']),
              'token': old['token'] if 'token' not in body else str(body['token'])}
    if config['kind'] not in KINDS or config['level'] not in LEVELS:
        raise ValueError('Type ou niveau d’alerte inconnu')
    if config['url'] and not _valid_url(config['url']):
        raise ValueError('Adresse HTTP(S) invalide')
    if config['enabled'] and not config['url']:
        raise ValueError('Adresse requise pour activer les alertes')
    if len(config['token']) > 300 or not config['token'].isprintable():
        raise ValueError('Jeton invalide')
    if config['kind'] == 'gotify' and config['enabled'] and not config['token']:
        raise ValueError('Gotify demande le jeton de l’application')
    path = _path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(FILE + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(config, stream)
    os.replace(temp, path)
    os.chmod(path, 0o600)
    return public(root)


def request_for(config, title, message, level):
    """urllib Request for one alert (no network here, so it can be tested)."""
    problem = level == 'error'
    kind, url, token = config['kind'], config['url'], config['token']
    headers = {'User-Agent': 'JOAL-Smart-Controller'}
    if kind == 'ntfy':
        query = urllib.parse.urlencode({'title': title, 'priority': 4 if problem else 3,
                                        'tags': 'warning' if problem else 'white_check_mark'})
        target = url + ('&' if '?' in url else '?') + query
        if token:
            headers['Authorization'] = 'Bearer ' + token
        headers['Content-Type'] = 'text/plain; charset=utf-8'
        return urllib.request.Request(target, data=message.encode('utf-8'), headers=headers, method='POST')
    headers['Content-Type'] = 'application/json'
    if kind == 'gotify':
        headers['X-Gotify-Key'] = token
        body = {'title': title, 'message': message, 'priority': 8 if problem else 4}
        return urllib.request.Request(url.rstrip('/') + '/message', data=json.dumps(body).encode(),
                                      headers=headers, method='POST')
    icon = '⚠️' if problem else '✅'
    body = {'content': f'{icon} **{title}**\n{message}'[:1900], 'allowed_mentions': {'parse': []}}
    return urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method='POST')


def deliver(config, title, message, level, opener=None):
    """Send now. Raises OSError with a short public reason (never the URL or token)."""
    opener = opener or urllib.request.build_opener(_NoRedirect(), urllib.request.ProxyHandler({}))
    try:
        with opener.open(request_for(config, title, message, level), timeout=10) as response:
            if not 200 <= response.status < 300:
                raise OSError(f'réponse HTTP {response.status}')
    except urllib.error.HTTPError as exc:
        raise OSError(f'réponse HTTP {exc.code}') from None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        reason = getattr(exc, 'reason', exc)
        raise OSError('service injoignable' + (' (délai dépassé)' if 'timed out' in str(reason) else '')) from None


class Dispatcher:
    """Background delivery of the controller's notifications."""

    def __init__(self, root, log, deliver_fn=deliver):
        self.root, self.log, self.deliver_fn = root, log, deliver_fn
        self.queue = queue.Queue(maxsize=50)
        self.last_error = None
        self.thread = None

    def wants(self, config, level):
        return config['enabled'] and config['url'] and (config['level'] == 'all' or level == 'error')

    def submit(self, title, message, level):
        config = load(self.root)
        if not self.wants(config, level):
            return False
        try:
            self.queue.put_nowait((config, title, message, level))
        except queue.Full:
            self.log.warning('Alertes externes : file pleine, alerte ignorée')
            return False
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._run, name='alerts', daemon=True)
            self.thread.start()
        return True

    def _run(self):
        while True:
            try:
                config, title, message, level = self.queue.get(timeout=60)
            except queue.Empty:
                return
            try:
                self.deliver_fn(config, title, message, level)
                self.last_error = None
            except OSError as exc:
                self.last_error = str(exc)
                self.log.warning('Alerte externe non envoyée (%s) : %s', config['kind'], exc)
