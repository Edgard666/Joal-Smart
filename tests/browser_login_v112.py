"""Full browser end-to-end smoke: no Basic popup, login, CSRF, diagnostics, logout."""
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
import main
from web_auth import SessionManager
from diagnostics import DiagnosticsStore


def run():
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        policy = json.loads((ROOT/'config.example.json').read_text())
        (folder/'policy.json').write_text(json.dumps(policy))
        with patch.object(main,'DATA',folder),patch.object(main,'CONFIG',folder/'policy.json'), \
             patch.object(main,'PASSWORD','browser-test-password'), \
             patch.object(main,'AUTH',SessionManager(lambda: main.PASSWORD)), \
             patch.object(main,'DIAGNOSTICS',DiagnosticsStore(folder/'diagnostics.json')):
            main.DIAGNOSTICS.event('test.ready', 'Diagnostic disponible pour la vérification')
            server = main.ThreadingHTTPServer(('127.0.0.1',0),main.Handler)
            worker=threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                with sync_playwright() as playwright:
                    browser=playwright.chromium.launch(executable_path=os.getenv('CHROMIUM','/usr/bin/chromium'), args=['--no-sandbox'])
                    page=browser.new_page(viewport={'width':1280,'height':800})
                    errors=[]
                    page.on('pageerror',lambda e: errors.append(str(e)))
                    page.goto(base+'/',wait_until='domcontentloaded')
                    assert page.url.endswith('/login'),page.url
                    assert page.get_by_role('heading',name='Connexion').is_visible()
                    # screenshot used for internal visual inspection
                    page.screenshot(path=str(folder/'joal-v112-login-preview.png'),full_page=True)
                    page.get_by_label('Mot de passe',exact=True).fill('bad-password')
                    page.get_by_role('button',name='Se connecter').click()
                    # v1.32+: the 401 body is read (double authentification) before the message
                    # appears, so wait for it instead of checking at once (failed on GitHub).
                    page.wait_for_selector('#login-error', state='visible', timeout=10000)
                    page.get_by_label('Mot de passe',exact=True).fill('browser-test-password')
                    page.get_by_role('button',name='Se connecter').click()
                    page.wait_for_url(base+'/',timeout=10000)
                    # Le lien est dans le groupe repliable « Suivi et règles » de la barre latérale.
                    page.locator('nav summary', has_text='Suivi et règles').click()
                    page.get_by_role('button',name='Diagnostics & journal').click()
                    # No wait_for_function here: the real server's CSP forbids eval, which Playwright
                    # needs as soon as the condition is not already true (slow machine).
                    page.locator('#diag-events', has_text='Diagnostic disponible').wait_for()
                    assert page.locator('#diag-processes').is_visible()
                    page.screenshot(path=str(folder/'joal-v112-diagnostics-preview.png'),full_page=True)
                    page.locator('#logout-btn').click()
                    page.wait_for_url(base+'/login',timeout=10000)
                    assert page.get_by_role('heading',name='Connexion').is_visible()
                    assert not errors,errors
                    print('Browser login/logout + diagnostics: OK')
                    browser.close()
            finally:
                server.shutdown(); server.server_close();worker.join(5)

if __name__ == '__main__':run()
