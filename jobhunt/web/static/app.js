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
      box.appendChild(document.createTextNode(s.busy ? 'Working: ' + s.kind : (window.jhIdleText || 'Idle')));
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

// Profile page: drop CV files on the box (or anywhere on the page) to upload them.
// Without this, a file dropped on the page just opens in the browser.
(function () {
  const zone = document.getElementById('cvDrop');
  if (!zone) return;
  const input = zone.querySelector('input[type=file]');
  const hasFiles = ev => ev.dataTransfer && Array.from(ev.dataTransfer.types || []).includes('Files');
  let depth = 0;
  window.addEventListener('dragenter', function (ev) {
    if (!hasFiles(ev)) return;
    depth++;
    zone.classList.add('over');
  });
  window.addEventListener('dragleave', function (ev) {
    if (hasFiles(ev) && --depth <= 0) { depth = 0; zone.classList.remove('over'); }
  });
  window.addEventListener('dragover', function (ev) {
    if (!hasFiles(ev)) return;
    ev.preventDefault();
    ev.dataTransfer.dropEffect = 'copy';
  });
  window.addEventListener('drop', function (ev) {
    if (!hasFiles(ev)) return;
    ev.preventDefault();
    depth = 0;
    zone.classList.remove('over');
    if (!ev.dataTransfer.files.length) return;
    input.files = ev.dataTransfer.files;
    zone.classList.add('sending');
    zone.requestSubmit();
  });
  // Picking files with the button uploads them straight away too.
  input.addEventListener('change', function () {
    if (input.files.length) { zone.classList.add('sending'); zone.requestSubmit(); }
  });
})();

// Reject panels (<details class="reject">): Cancel or Esc closes one; opening one closes the others.
document.addEventListener('click', function (ev) {
  const btn = ev.target.closest && ev.target.closest('[data-close-details]');
  if (!btn) return;
  const box = btn.closest('details');
  if (box) { box.open = false; const f = btn.closest('form'); if (f) f.reset(); }
});
document.addEventListener('toggle', function (ev) {
  if (!ev.target.matches || !ev.target.matches('details.reject') || !ev.target.open) return;
  document.querySelectorAll('details.reject[open]').forEach(d => { if (d !== ev.target) d.open = false; });
}, true);
document.addEventListener('keydown', function (ev) {
  if (ev.key === 'Escape') document.querySelectorAll('details.reject[open]').forEach(d => { d.open = false; });
});

// <select data-autosubmit>: send the form as soon as a value is picked.
document.addEventListener('change', function (ev) {
  if (ev.target.matches && ev.target.matches('select[data-autosubmit]') && ev.target.value) ev.target.form.submit();
});
