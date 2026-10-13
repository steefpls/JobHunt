// Shared page behaviour. No inline scripts anywhere: the pages run under a strict
// Content-Security-Policy, so every handler lives in a static file like this one.

// Keeps the sidebar's "Working / Idle" indicator fresh on every page.
(function () {
  const box = document.getElementById('sideRun');
  if (!box) return;
  async function tick() {
    try {
      const s = await (await fetch('/api/status?after=999999999')).json();
      box.innerHTML = '';
      const dot = document.createElement('span');
      dot.className = 'dot' + (s.busy ? ' live' : '');
      box.appendChild(dot);
      box.appendChild(document.createTextNode(s.busy ? 'Working: ' + s.kind : 'Idle'));
    } catch (e) { /* ignore */ }
  }
  setInterval(tick, 4000);
})();

// Accounts page: once the login window is closed, reload so the buttons come back.
(function () {
  if (!document.getElementById('loginOpen')) return;
  const timer = setInterval(async function () {
    try {
      const s = await (await fetch('/api/login-browser')).json();
      if (!s.open) { clearInterval(timer); location.replace('/accounts?msg=' + encodeURIComponent('Login window closed. Session saved.')); }
    } catch (e) { /* ignore */ }
  }, 2000);
})();

// <form data-confirm="Question?">: ask before sending.
document.addEventListener('submit', function (ev) {
  const q = ev.target.dataset && ev.target.dataset.confirm;
  if (q && !confirm(q)) ev.preventDefault();
});

// <button data-confirm-auto="Question?">: ask only when the form's mode is "auto".
document.addEventListener('click', function (ev) {
  const btn = ev.target.closest && ev.target.closest('[data-confirm-auto]');
  if (!btn || !btn.form) return;
  const mode = btn.form.querySelector('input[name=mode]:checked');
  if (mode && mode.value === 'auto' && !confirm(btn.dataset.confirmAuto)) ev.preventDefault();
});

// <select data-autosubmit>: send the form as soon as a value is picked.
document.addEventListener('change', function (ev) {
  if (ev.target.matches && ev.target.matches('select[data-autosubmit]') && ev.target.value) ev.target.form.submit();
});
