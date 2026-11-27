"""Chromium v1.32 on the real server: password change, TOTP setup then login with a code,
widget token, daily cap field in the rule editor. Both designs."""
import json
import re
import os
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
import admin_account  # noqa: E402
import interface_design  # noqa: E402
import main  # noqa: E402
from diagnostics import DiagnosticsStore  # noqa: E402
from web_auth import SessionManager  # noqa: E402

START, NEW = 'mot-de-passe-de-depart', 'nouveau-mot-de-passe-1'


def login(page, base, password, code=None):
    page.goto(base + '/login', wait_until='domcontentloaded')
    page.fill('#login-password', password)
    page.click('#login-submit')
    if code is not None:
        page.wait_for_selector('#login-code', state='visible')
        assert 'code' in page.locator('#login-error').inner_text()
        page.fill('#login-code', code)
        page.click('#login-submit')
    page.wait_for_url(base + '/', timeout=10000)
    page.wait_for_selector('nav details')
    page.evaluate("document.querySelectorAll('nav details').forEach(d => d.open = true)")


def run(design):
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        (folder / 'policy.json').write_text((ROOT / 'config.example.json').read_text())
        interface_design.save(folder, design)
        clock = [1_800_000_000]
        account = admin_account.Account(folder, {}, clock=lambda: clock[0], clear_password=lambda: START)
        auth = SessionManager(checker=account.check_password,
                              second_factor=lambda: account.check_totp if account.totp_enabled() else None)
        with patch.object(main, 'DATA', folder), patch.object(main, 'CONFIG', folder / 'policy.json'), \
                patch.object(main, 'ACCOUNT', account), patch.object(main, 'AUTH', auth), \
                patch.object(main, 'DIAGNOSTICS', DiagnosticsStore(folder / 'diagnostics.json')):
            server = main.ThreadingHTTPServer(('127.0.0.1', 0), main.Handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(executable_path=os.getenv('CHROMIUM', '/usr/bin/chromium'),
                                                         args=['--no-sandbox'])
                    page = browser.new_page(viewport={'width': 1360, 'height': 900})
                    page.on('dialog', lambda d: d.accept())
                    errors = []
                    page.on('pageerror', lambda e: errors.append(str(e)))
                    login(page, base, START)
                    page.locator('.nav-link[data-view="settings"]').click()
                    page.locator('#account-summary', has_text='ADMIN_PASSWORD').wait_for()

                    # 1. Password change: hash shown, interface becomes the source.
                    page.fill('#password-current', START)
                    page.fill('#password-new', NEW)
                    page.fill('#password-confirm', NEW)
                    page.click('#password-save')
                    page.wait_for_selector('#password-hash-box', state='visible')
                    shown = page.locator('#password-hash').inner_text()
                    assert shown.startswith('ADMIN_PASSWORD_HASH=scrypt:'), shown
                    assert admin_account.verify_password(NEW, shown.split('=', 1)[1])
                    assert 'interface' in page.locator('#account-summary').inner_text()

                    # 2. TOTP: needs the current password, then a valid code.
                    page.click('#totp-start')
                    assert 'mot de passe actuel' in page.locator('#security-message').inner_text()
                    page.fill('#password-current', NEW)
                    page.click('#totp-start')
                    page.wait_for_selector('#totp-setup', state='visible')
                    secret = page.locator('#totp-secret').inner_text().replace(' ', '')
                    assert page.locator('#totp-uri').get_attribute('href').startswith('otpauth://totp/')
                    page.fill('#totp-confirm-code', '000000')
                    page.click('#totp-confirm')
                    page.locator('#security-message', has_text='Code incorrect').wait_for()
                    page.fill('#totp-confirm-code', admin_account.totp_code(secret, clock[0] // 30))
                    page.click('#totp-confirm')
                    # has_text ignores case: « Désactivée » would match « Activée ».
                    page.locator('#totp-status', has_text=re.compile(r'^Activée')).wait_for()
                    assert page.locator('#totp-disable').is_visible()
                    page.locator('#security-panel').screenshot(path=f'/tmp/v132-security-{design}.png')

                    # 3. Widget token shown once with a Homepage example.
                    page.click('#widget-create')
                    page.wait_for_selector('#widget-new', state='visible')
                    token = page.locator('#widget-token').inner_text()
                    assert token.startswith('jsw_') and token in page.locator('#widget-example').inner_text()
                    page.locator('#widget-panel').screenshot(path=f'/tmp/v132-widget-{design}.png')

                    # 4. Daily cap field in the rule editor.
                    page.locator('.nav-link[data-view="rules"]').click()
                    page.wait_for_selector('text=Plafond d’upload simulé par jour')

                    # 5. Log out, log in again: the code is asked for.
                    page.locator('#logout-btn').click()
                    page.wait_for_url(base + '/login', timeout=10000)
                    clock[0] += 30
                    login(page, base, NEW, admin_account.totp_code(secret, clock[0] // 30))
                    page.screenshot(path=f'/tmp/v132-after-totp-{design}.png')
                    assert not errors, errors
                    browser.close()
            finally:
                server.shutdown()
                server.server_close()
                worker.join(5)


if __name__ == '__main__':
    for name in ('classique', 'baie'):
        run(name)
    print('Chromium v1.32 : mot de passe, double authentification, widget et plafond (Classique et Baie) OK')
