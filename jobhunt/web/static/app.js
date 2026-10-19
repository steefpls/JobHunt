// Shared page behaviour. No inline scripts anywhere: the pages run under a strict
// Content-Security-Policy, so every handler lives in a static file like this one.

const jhCalm = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;

// Keeps the sidebar's "Working / Idle" indicator and the To review count fresh on every page.
(function () {
  const box = document.getElementById('sideRun');
  if (!box) return;
  const dot = box.querySelector('.dot'), txt = box.querySelector('.txt'), top = document.getElementById('topDot');
  async function tick() {
    try {
      const s = await (await fetch('/api/status?after=999999999')).json();
      const text = s.busy ? 'Working: ' + s.kind : (window.jhIdleText || 'Idle');
      dot.classList.toggle('live', !!s.busy);
      if (top) { top.classList.toggle('live', !!s.busy); top.title = text; }
      if (txt.textContent !== text) {  // cross-fade the words rather than swapping them
        txt.style.opacity = '0';
        setTimeout(function () { txt.textContent = text; txt.style.opacity = ''; }, jhCalm() ? 0 : 140);
      }
      // "(3) Hunt · JobHunt": packages waiting for your approval, visible from another tab
      const n = (s.counts && s.counts.Ready) || 0;
      document.title = (n ? '(' + n + ') ' : '') + document.title.replace(/^\(\d+\) /, '');
      const c = document.getElementById('navCount'), total = n + ((s.counts && s.counts['Needs you']) || 0);
      if (c && String(total) !== c.textContent) { if (total) c.textContent = total; else c.remove(); }
    } catch (e) { /* ignore */ }
  }
  setInterval(tick, 4000);
})();

// Narrow windows: the menu button slides the sidebar in as a drawer.
(function () {
  const btn = document.getElementById('menuBtn'), side = document.getElementById('side'), scrim = document.getElementById('scrim');
  if (!btn || !side) return;
  function set(open) {
    side.classList.toggle('open', open); scrim.classList.toggle('open', open);
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) { const a = side.querySelector('nav a.on') || side.querySelector('nav a'); if (a) a.focus({preventScroll: true}); }
  }
  btn.addEventListener('click', () => set(!side.classList.contains('open')));
  scrim.addEventListener('click', () => set(false));
  document.addEventListener('keydown', ev => { if (ev.key === 'Escape' && side.classList.contains('open')) { set(false); btn.focus(); } });
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

// Asking before something that can't be taken back: a proper dialog, not the browser's grey box.
// <form data-confirm="What happens." data-confirm-title="Question?" data-confirm-ok="Do it" [data-confirm-danger]>
function jhConfirm(opts) {
  const dlg = document.getElementById('confirmDialog');
  if (!dlg || !dlg.showModal) return Promise.resolve(window.confirm(opts.text));
  document.getElementById('confirmTitle').textContent = opts.title || 'Are you sure?';
  document.getElementById('confirmText').textContent = opts.text || '';
  const yes = document.getElementById('confirmYes');
  yes.textContent = opts.ok || 'Continue';
  yes.className = 'btn ' + (opts.danger ? 'danger solid' : 'primary');
  dlg.classList.toggle('danger', !!opts.danger);
  const use = dlg.querySelector('#confirmIcon use');
  if (use) use.setAttribute('href', use.getAttribute('href').replace(/#.*$/, '#' + (opts.icon || (opts.danger ? 'triangle-alert' : 'send'))));
  return new Promise(function (resolve) {
    dlg.returnValue = '';
    dlg.showModal();
    (opts.danger ? document.getElementById('confirmNo') : yes).focus();
    function closed() {
      dlg.removeEventListener('close', closed);
      resolve(dlg.returnValue === 'yes');
    }
    dlg.addEventListener('close', closed);
  });
}
(function () {  // the dialog leaves with a short fade instead of vanishing
  const dlg = document.getElementById('confirmDialog');
  if (!dlg) return;
  dlg.addEventListener('click', ev => { if (ev.target === dlg) dlg.close('no'); });  // a click on the backdrop
  dlg.querySelector('form').addEventListener('submit', function (ev) {
    if (jhCalm()) return;
    ev.preventDefault();
    const v = ev.submitter ? ev.submitter.value : 'no';
    dlg.classList.add('closing');
    setTimeout(() => { dlg.classList.remove('closing'); dlg.close(v); }, 130);
  });
})();

document.addEventListener('submit', function (ev) {
  const f = ev.target, q = f.dataset && f.dataset.confirm;
  if (!q || f.dataset.confirmed) { delete f.dataset.confirmed; return; }
  ev.preventDefault();
  ev.stopImmediatePropagation();
  const submitter = ev.submitter;
  jhConfirm({text: q, title: f.dataset.confirmTitle, ok: f.dataset.confirmOk, icon: f.dataset.confirmIcon,
             danger: f.hasAttribute('data-confirm-danger')}).then(function (yes) {
    if (!yes) return;
    f.dataset.confirmed = '1';
    if (f.requestSubmit) f.requestSubmit(submitter && submitter.form === f ? submitter : undefined); else f.submit();
  });
}, true);

// <button data-confirm-auto="Question?">: ask only when the form's mode is "auto".
document.addEventListener('click', function (ev) {
  const btn = ev.target.closest && ev.target.closest('[data-confirm-auto]');
  if (!btn || !btn.form || btn.dataset.confirmed) { if (btn) delete btn.dataset.confirmed; return; }
  const mode = btn.form.querySelector('input[name=mode]:checked');
  if (!mode || mode.value !== 'auto') return;
  ev.preventDefault();
  jhConfirm({text: btn.dataset.confirmAuto, title: 'Send applications automatically?', ok: 'Start and send', danger: true})
    .then(function (yes) { if (yes) { btn.dataset.confirmed = '1'; btn.click(); } });
});

// Every button shows it's working once pressed: a spinner, and "Saving…"-style words when the form says what it does.
document.addEventListener('submit', function (ev) {
  if (ev.defaultPrevented) return;
  const f = ev.target, btn = ev.submitter || f.querySelector('button:not([type=button])');
  if (!btn || btn.closest('dialog') || btn.classList.contains('mitem') || btn.classList.contains('tbtn')) return;
  btn.setAttribute('aria-busy', 'true');
  const icon = btn.querySelector('svg use');
  if (icon) { icon.setAttribute('href', icon.getAttribute('href').replace(/#.*$/, '#loader-circle')); icon.parentNode.classList.add('spin'); }
  const words = btn.dataset.busy || f.dataset.busy;
  if (words) { const t = Array.from(btn.childNodes).reverse().find(n => n.nodeType === 3 && n.textContent.trim()); if (t) t.textContent = words; }
  setTimeout(() => { btn.disabled = true; }, 0);  // after the browser has read the button's own value
});
window.addEventListener('pageshow', function (ev) {  // back from another page: buttons aren't stuck spinning
  if (ev.persisted) document.querySelectorAll('[aria-busy=true]').forEach(b => { b.removeAttribute('aria-busy'); b.disabled = false; });
});

// <form data-guard>: ask before leaving the page with changes that weren't saved, and say when there are some.
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
  function mark(f) {
    const dirty = snap(f) !== saved.get(f);
    const id = f.id ? '[data-savestate="' + f.id + '"]' : null;
    const el = (id && document.querySelector(id)) || f.querySelector('[data-savestate]');
    if (!el) return;
    el.classList.toggle('dirty', dirty);
    el.innerHTML = '<span class="sdot"></span>' + (dirty ? 'Unsaved changes' : 'All changes saved');
  }
  forms.forEach(f => { f.addEventListener('input', () => mark(f)); f.addEventListener('change', () => mark(f)); mark(f); });
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
  const say = document.getElementById('dropSay');
  const hasFiles = ev => ev.dataTransfer && Array.from(ev.dataTransfer.types || []).includes('Files');
  let depth = 0;
  function send() {
    zone.classList.add('sending');
    if (say) say.textContent = 'Uploading ' + input.files.length + ' file' + (input.files.length === 1 ? '' : 's') + '…';
    zone.requestSubmit();
  }
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
    send();
  });
  // Picking files with the button uploads them straight away too.
  input.addEventListener('change', function () { if (input.files.length) send(); });
})();

// Reject panels (<details class="reject">): Cancel closes one (motion.js handles Esc, clicks outside, one at a time).
function closeDetails(d) { if (window.jhMotion) window.jhMotion.close(d); else d.open = false; }
document.addEventListener('click', function (ev) {
  const btn = ev.target.closest && ev.target.closest('[data-close-details]');
  if (!btn) return;
  const box = btn.closest('details');
  if (box) { closeDetails(box); const f = btn.closest('form'); if (f) f.reset(); const s = box.querySelector('summary'); if (s) s.focus(); }
});

// <select data-autosubmit>: send the form as soon as a value is picked.
document.addEventListener('change', function (ev) {
  if (ev.target.matches && ev.target.matches('select[data-autosubmit]') && ev.target.value) ev.target.form.submit();
});

// Table rows that open their job: a click anywhere on the row (not on its own links and buttons).
document.addEventListener('click', function (ev) {
  const row = ev.target.closest && ev.target.closest('tr[data-href]');
  if (!row || ev.target.closest('a, button, summary, details, input, select, label, form')) return;
  if (ev.ctrlKey || ev.metaKey) window.open(row.dataset.href); else location.href = row.dataset.href;
});

// The theme switch under the menu: auto (follows Windows) -> dark -> light -> auto.
(function () {
  const btn = document.getElementById('themeBtn'), order = ['auto', 'dark', 'light'];
  if (!btn) return;
  const icons = {auto: 'sun-moon', dark: 'moon', light: 'sun'};
  function label() {
    const t = localStorage.getItem('jh-theme') || 'auto';
    btn.querySelector('span').textContent = 'Theme: ' + t;
    const use = btn.querySelector('use');
    if (use) use.setAttribute('href', use.getAttribute('href').replace(/#.*$/, '#' + icons[t]));
  }
  label();
  btn.addEventListener('click', function () {
    const now = localStorage.getItem('jh-theme') || 'auto';
    localStorage.setItem('jh-theme', order[(order.indexOf(now) + 1) % order.length]);
    const root = document.documentElement;
    if (!jhCalm()) { root.classList.add('theming'); setTimeout(() => root.classList.remove('theming'), 400); }
    window.jhTheme(); label();
  });
})();

// Toasts: the message after an action slides in at the bottom right and leaves by itself
// (errors and ones with Undo stay longer; hovering holds them). The message comes out of the address,
// so a reload or Back doesn't show it again.
(function () {
  const url = new URL(location.href);
  let changed = false;
  ['msg', 'err', 'warn', 'undo', 'restore'].forEach(k => { if (url.searchParams.has(k)) { url.searchParams.delete(k); changed = true; } });
  if (changed) history.replaceState(history.state, '', url.pathname + url.search + url.hash);

  const box = document.getElementById('toasts');
  if (!box) return;
  function dismiss(t) {
    if (t.classList.contains('gone')) return;
    t.classList.add('gone');
    setTimeout(() => t.remove(), jhCalm() ? 0 : 230);
  }
  box.querySelectorAll('.toast').forEach(function (t, i) {
    t.style.animationDelay = (i * 60) + 'ms';
    let left = parseInt(t.dataset.ttl || '5000') + i * 400, last = performance.now(), held = false;
    t.addEventListener('mouseenter', () => { held = true; });
    t.addEventListener('mouseleave', () => { held = false; last = performance.now(); });
    t.addEventListener('focusin', () => { held = true; });
    const close = t.querySelector('.tclose');
    if (close) close.addEventListener('click', () => dismiss(t));
    (function tick() {
      const now = performance.now();
      if (!held) left -= now - last;
      last = now;
      if (left <= 0) dismiss(t); else setTimeout(tick, 200);
    })();
  });
})();

// <button data-copy="#el">: copies that element's text, and says so on the button for a moment.
// <button data-fill="#field">: puts the button's own words in the field (the examples on the Ask page).
document.addEventListener('click', function (ev) {
  const copy = ev.target.closest && ev.target.closest('[data-copy]');
  if (copy) {
    const el = document.querySelector(copy.dataset.copy);
    if (!el || !navigator.clipboard) return;
    navigator.clipboard.writeText(el.innerText.trim()).then(function () {
      const t = Array.from(copy.childNodes).reverse().find(n => n.nodeType === 3 && n.textContent.trim());
      const use = copy.querySelector('svg use');
      if (!t || copy.dataset.was) return;
      copy.dataset.was = t.textContent;
      t.textContent = 'Copied';
      if (use) use.setAttribute('href', use.getAttribute('href').replace(/#.*$/, '#check'));
      copy.classList.add('copied');
      setTimeout(function () {
        t.textContent = copy.dataset.was; delete copy.dataset.was; copy.classList.remove('copied');
        if (use) use.setAttribute('href', use.getAttribute('href').replace(/#.*$/, '#copy'));
      }, 1600);
    });
    return;
  }
  const fill = ev.target.closest && ev.target.closest('[data-fill]');
  if (fill) {
    const field = document.querySelector(fill.dataset.fill);
    if (field) { field.value = fill.textContent.trim(); field.focus(); }
  }
});
// Ctrl+Enter in a textarea sends its form (the Ask box).
document.addEventListener('keydown', function (ev) {
  if (ev.key !== 'Enter' || !(ev.ctrlKey || ev.metaKey) || !ev.target.matches('textarea[data-send]')) return;
  ev.preventDefault();
  const f = ev.target.form;
  if (f && f.requestSubmit) f.requestSubmit();
});

// A link to a folded section (the sidebar's AI cost -> /applications#aiuse) opens it.
(() => {
  const open = () => {
    const el = location.hash.length > 1 && document.getElementById(location.hash.slice(1));
    if (el && el.tagName === 'DETAILS') { el.open = true; el.scrollIntoView({block: 'start'}); }
  };
  open();
  window.addEventListener('hashchange', open);
})();
