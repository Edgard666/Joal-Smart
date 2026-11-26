"""Chromium v1.31: « Nouveautés » after an update, Statistiques page, alert settings."""
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'app/static'
conf = json.loads((ROOT / 'config.example.json').read_text())
days = [f'2026-10-{d:02d}' for d in range(28, 32)] + [f'2026-11-{d:02d}' for d in range(1, 27)]
daily = [0] * 25 + [3 * 2**30, 0, 5 * 2**30, 2**30, 7 * 2**30]
row = lambda domain, today, week, month, seconds, torrents: {
    'domain': domain, 'today': {'bytes': today, 'seconds': 0}, 'week': {'bytes': week, 'seconds': 0},
    'month': {'bytes': month, 'seconds': seconds}, 'torrents': torrents, 'series': [0] * 30}
stats = {'days': days, 'daily': daily, 'totals': {'today': 7 * 2**30, 'week': 16 * 2**30, 'month': 16 * 2**30},
         'trackers': [row('v3x.club', 6 * 2**30, 14 * 2**30, 14 * 2**30, 93784, 3),
                      row('tracker.example.org', 2**30, 2 * 2**30, 2 * 2**30, 1800, 1)]}
changes = {'current': '1.31.0', 'sections': [
    {'version': '1.31.0', 'title': 'Alertes sur le téléphone', 'items': ['Alertes ntfy, Gotify ou Discord.', 'Page Statistiques.']},
    {'version': '1.30.2', 'title': 'Retrait groupé', 'items': ['Bouton « Retirer de JOAL ».']}]}
state = {'qbitConnected': True, 'dryRun': True, 'error': None, 'rules': [], 'torrents': [], 'totals': {},
         'cleanup': {}, 'swarm': {}, 'solo': {'count': 0, 'active': 0, 'items': []}}
fixtures = {'/api/auth/session': {'authenticated': True, 'csrfToken': 'token'}, '/api/config': conf, '/api/state': state,
            '/api/clients': {'clients': []}, '/api/stats?days=30': stats,
            '/api/changelog?since=1.30.1': changes, '/api/changelog?since=0': changes,
            '/api/alerts': {'enabled': False, 'kind': 'ntfy', 'url': '', 'level': 'problems', 'hasToken': False, 'lastError': None},
            '/api/qbit-connection': {'url': 'http://qbittorrent:8080', 'username': 'admin', 'hasPassword': True}}

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=os.getenv('CHROMIUM', '/usr/bin/chromium'), args=['--no-sandbox'])
    for stylesheet in ('app.css', 'design-baie.css'):
        page = browser.new_page(viewport={'width': 1400, 'height': 900})
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        html = (STATIC / 'index.html').read_text()
        for tag in ['<link rel="stylesheet" href="/assets/app.css">', '<script src="/assets/torrent_table.js" defer></script>',
                    '<script src="/assets/app.js" defer></script>']:
            html = html.replace(tag, '')
        html = html.replace('__APP_VERSION__', '1.31.0').replace('__JOAL_VERSION__', '2.1.37')
        # A real origin: localStorage (« Nouveautés ») is refused on about:blank.
        page.route('http://joal.test/', (lambda body: lambda route: route.fulfill(body=body, content_type='text/html'))(html))
        page.goto('http://joal.test/')
        page.add_style_tag(content=(STATIC / stylesheet).read_text())
        page.evaluate('''fixtures => {
          localStorage.setItem('joal.seenVersion', '1.30.1');
          window.posted = [];
          window.fetch = async (path, options) => {
            if (options && options.method === 'POST') {
              window.posted.push({path, body: options.body ? JSON.parse(options.body) : null});
              const answer = path === '/api/alerts'
                ? Object.assign({}, fixtures['/api/alerts'], JSON.parse(options.body), {hasToken: !!JSON.parse(options.body).token})
                : {success: true};
              delete answer.token;
              return new Response(JSON.stringify(answer), {status: 200, headers: {'Content-Type': 'application/json'}});
            }
            const data = fixtures[path];
            return new Response(JSON.stringify(data || {error: 'missing ' + path}), {status: data ? 200 : 404,
                                headers: {'Content-Type': 'application/json'}});
          };}''', fixtures)
        page.add_script_tag(content=(STATIC / 'torrent_table.js').read_text())
        page.add_script_tag(content=(STATIC / 'app.js').read_text())
        page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")

        # 1. Nouveautés opens by itself after the update, once.
        page.wait_for_selector('#whats-new-dialog[open]')
        text = page.locator('#whats-new-items').inner_text()
        assert 'v1.31.0 · Alertes sur le téléphone' in text and 'Retirer de JOAL' in text, text
        assert page.locator('#whats-new-subtitle').inner_text() == 'Mise à jour installée : v1.31.0'
        page.click('#whats-new-close')
        assert page.evaluate("() => localStorage.getItem('joal.seenVersion')") == '1.31.0'
        page.click('#whats-new-open')
        page.wait_for_selector('#whats-new-dialog[open]')
        page.click('#whats-new-close')

        # 2. Statistiques.
        page.locator('.nav-link[data-view="stats"]').click()
        page.wait_for_function("document.querySelectorAll('#stats-items tr').length === 2")
        assert page.locator('#stats-today').inner_text() == '7 Gio'
        assert page.locator('#stats-month').inner_text() == '16 Gio'
        assert page.locator('#stats-trackers').inner_text() == '2'
        assert page.locator('#stats-chart rect').count() == 30
        first = page.locator('#stats-items tr').first.inner_text()
        assert 'v3x.club' in first and '14 Gio' in first and '26 h 03' in first, first
        bar = page.evaluate("() => getComputedStyle(document.querySelector('#stats-chart rect:last-child')).fill")
        assert bar not in ('', 'none', 'rgb(0, 0, 0)'), bar                    # styled in this design
        if stylesheet == 'design-baie.css':
            page.screenshot(path='/tmp/v131-stats-baie.png', full_page=True)
        else:
            page.screenshot(path='/tmp/v131-stats-classique.png', full_page=True)

        # 3. Alert settings.
        page.locator('.nav-link[data-view="settings"]').click()
        page.wait_for_function("document.getElementById('alerts-help').textContent.includes('ntfy.sh')")
        page.select_option('#alerts-kind', 'gotify')
        assert 'Gotify' in page.locator('#alerts-help').inner_text()
        assert page.locator('#alerts-token').get_attribute('placeholder') == 'Jeton de l’application'
        page.select_option('#alerts-kind', 'ntfy')
        page.check('#alerts-enabled')
        page.fill('#alerts-url', 'https://ntfy.sh/mon-sujet')
        page.click('#alerts-test')
        page.wait_for_function("document.getElementById('alerts-message').textContent.includes('vérifie ton téléphone')")
        posted = page.evaluate("() => window.posted")
        assert posted[0] == {'path': '/api/alerts', 'body': {'enabled': True, 'kind': 'ntfy', 'level': 'problems',
                                                             'url': 'https://ntfy.sh/mon-sujet'}}, posted   # no token sent
        assert posted[1]['path'] == '/api/alerts/test', posted
        if stylesheet == 'app.css':
            page.locator('#alerts-form').screenshot(path='/tmp/v131-alerts.png')
        assert not errors, errors
        page.close()
    print('Chromium v1.31: Nouveautés, Statistiques (Classique et Baie) et alertes OK')
    browser.close()
