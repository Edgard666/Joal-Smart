"""Chromium v1.33: notice when JOAL simulates another client than the real qBittorrent (one
click aligns the rules), and the « intervalle max. sans leecher » setting. Both designs."""
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'app/static'
conf = json.loads((ROOT / 'config.example.json').read_text())
conf['rules'][0]['client'] = 'qbittorrent-5.3.0.client'
conf['rules'][0]['enabled'] = True
rule_id = conf['rules'][0]['id']
state = {'qbitConnected': True, 'dryRun': True, 'error': None, 'rules': [], 'torrents': [], 'totals': {},
         'cleanup': {}, 'swarm': {}, 'solo': {'count': 0, 'active': 0, 'items': []},
         'clientCheck': {'qbitVersion': '5.2.4', 'suggested': 'qbittorrent-5.2.4.client', 'exact': True,
                         'mismatched': [rule_id]}}
fixtures = {'/api/auth/session': {'authenticated': True, 'csrfToken': 'token'}, '/api/config': conf, '/api/state': state,
            '/api/clients': {'clients': ['qbittorrent-5.2.4.client', 'qbittorrent-5.3.0.client']}}

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=os.getenv('CHROMIUM', '/usr/bin/chromium'), args=['--no-sandbox'])
    for stylesheet in ('app.css', 'design-baie.css'):
        page = browser.new_page(viewport={'width': 1300, 'height': 900})
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('dialog', lambda d: d.accept())
        html = (STATIC / 'index.html').read_text()
        for tag in ['<link rel="stylesheet" href="/assets/app.css">', '<script src="/assets/torrent_table.js" defer></script>',
                    '<script src="/assets/app.js" defer></script>']:
            html = html.replace(tag, '')
        html = html.replace('__APP_VERSION__', '1.33.0').replace('__JOAL_VERSION__', '2.1.37')
        page.route('http://joal.test/', (lambda body: lambda route: route.fulfill(body=body, content_type='text/html'))(html))
        page.goto('http://joal.test/')
        page.add_style_tag(content=(STATIC / stylesheet).read_text())
        page.evaluate('''fixtures => {
          localStorage.setItem('joal.seenVersion', '1.33.0');
          window.posted = [];
          window.fetch = async (path, options) => {
            if (options && options.method === 'POST') {
              window.posted.push({path, body: options.body ? JSON.parse(options.body) : null});
              return new Response('{"success":true}', {status: 200, headers: {'Content-Type': 'application/json'}});
            }
            const data = fixtures[path] || (path.startsWith('/api/changelog') ? {current: '1.33.0', sections: []} : null);
            return new Response(JSON.stringify(data || {error: 'missing ' + path}), {status: data ? 200 : 404,
                                headers: {'Content-Type': 'application/json'}});
          };}''', fixtures)
        page.add_script_tag(content=(STATIC / 'torrent_table.js').read_text())
        page.add_script_tag(content=(STATIC / 'app.js').read_text())
        page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")

        page.locator('.nav-link[data-view="rules"]').click()
        page.wait_for_selector('#client-check', state='visible')
        text = page.locator('#client-check').inner_text()
        assert 'qBittorrent 5.2.4' in text and rule_id in text and 'qbittorrent-5.2.4.client' in text, text
        page.locator('#client-check').screenshot(path=f'/tmp/v133-client-check-{stylesheet[:-4]}.png')
        page.click('#client-check-apply')
        page.wait_for_function("window.posted.some(p => p.path === '/api/config')")
        saved = [p for p in page.evaluate('() => window.posted') if p['path'] == '/api/config'][0]['body']
        assert saved['rules'][0]['client'] == 'qbittorrent-5.2.4.client', saved['rules'][0]

        page.locator('.nav-link[data-view="settings"]').click()
        assert page.locator('#setting-qbit-port').is_checked()          # v1.34: on by default
        assert page.locator('#setting-swarm-max-interval').input_value() == '240'
        page.fill('#setting-swarm-max-interval', '360')
        assert 'Modifications non enregistrées' in page.locator('#settings-save-status').inner_text()
        assert not errors, errors
        page.close()
    print('Chromium v1.33 : alignement du profil client et intervalle adaptatif (Classique et Baie) OK')
    browser.close()
