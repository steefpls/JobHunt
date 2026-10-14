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
      // "(3) Hunt · JobHunt": packages waiting for your approval, visible from another tab
      const n = (s.counts && s.counts.Ready) || 0;
      document.title = (n ? '(' + n + ') ' : '') + document.title.replace(/^\(\d+\) /, '');
    } catch (e) { /* ignore */ }
  }
  setInterval(tick, 4000);
})();

// Accounts page: while the login window is open or the logins are being checked, wait,
// then reload so the page shows whether each site is logged in.
(function () {
  const open = document.getElementById('loginOpen');
  if (!open && !document.getElementById('loginChecking')) return;
  let wasOpen = !!open;
  const timer = setInterval(async function () {
    try {
      const s = await (await fetch('/api/login-browser')).json();
      if (wasOpen && !s.open) { wasOpen = false; location.replace('/accounts#sitelogins'); return; }  // now "Checking…"
      if (!s.open && !s.checking) { clearInterval(timer); location.replace('/accounts#sitelogins'); }
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

// <form data-guard>: ask before leaving the page with changes that weren't saved.
// (Registered after the data-confirm handler, so a cancelled confirm doesn't count as saving.)
(function () {
  const forms = Array.from(document.querySelectorAll('form[data-guard]'));
  if (!forms.length) return;
  const snap = f => new URLSearchParams(new FormData(f)).toString();
  const saved = new Map(forms.map(f => [f, snap(f)]));
  let saving = false;
  document.addEventListener('submit', function (ev) {
    if (!ev.defaultPrevented && saved.has(ev.target)) saving = true;
  });
  window.addEventListener('beforeunload', function (ev) {
    if (saving || !forms.some(f => snap(f) !== saved.get(f))) return;
    ev.preventDefault();
    ev.returnValue = '';  // the browser shows its own "Leave site? Changes you made may not be saved."
  });
})();

// <select data-autosubmit>: send the form as soon as a value is picked.
document.addEventListener('change', function (ev) {
  if (ev.target.matches && ev.target.matches('select[data-autosubmit]') && ev.target.value) ev.target.form.submit();
});
