'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const form = $('login-form');
  const submit = $('login-submit');
  const errorBox = $('login-error');
  $('toggle-password').addEventListener('click', () => {
    const input = $('login-password');
    const hidden = input.type === 'password';
    input.type = hidden ? 'text' : 'password';
    $('toggle-password').textContent = hidden ? 'Masquer' : 'Afficher';
    $('toggle-password').setAttribute('aria-pressed', String(hidden));
  });
  form.addEventListener('submit', async e => {
    e.preventDefault();
    errorBox.hidden = true;
    submit.disabled = true;
    submit.firstChild.textContent = 'Connexion… ';
    try {
      const response = await fetch('/api/auth/login', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(Object.assign({username: $('login-username').value, password: $('login-password').value},
          $('totp-field').hidden ? {} : {code: $('login-code').value}))
      });
      if (response.status === 401 && $('totp-field').hidden) {
        const data = await response.json().catch(() => ({}));
        if (data.totpRequired) {
          $('totp-field').hidden = false;
          $('login-code').required = true;
          errorBox.textContent = 'Saisissez le code affiché par votre application d’authentification.';
          errorBox.hidden = false;
          $('login-code').focus();
          return;
        }
      }
      if (!response.ok) {
        errorBox.textContent = response.status === 429 ? 'Trop de tentatives. Réessayez plus tard.' : 'Identifiants incorrects ou accès refusé.';
        errorBox.hidden = false;
        $('login-code').value = '';
        if ($('totp-field').hidden) {
          $('login-password').value = '';
          $('login-password').focus();
        } else {
          $('login-code').focus();
        }
        return;
      }
      $('login-password').value = '';
      location.replace('/');
    } catch (_) {
      errorBox.textContent = 'Connexion au serveur impossible.';
      errorBox.hidden = false;
    } finally {
      submit.disabled = false;
      submit.firstChild.textContent = 'Se connecter ';
    }
  });
  fetch('/api/auth/session', {cache:'no-store',credentials:'same-origin'})
    .then(res => res.ok ? res.json() : null)
    .then(data => { if (data && data.authenticated) location.replace('/'); })
    .catch(() => {});
})();
