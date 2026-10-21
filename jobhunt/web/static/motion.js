// Smooth motion, so nothing jumps into place. The timings are the CSS tokens' twins (style.css):
// quick out of the blocks, a long soft landing, and springs that settle with a little give.
//   - every <details> slides open and shut; menus and the Reject panel pop in from their button
//   - a row or card that's been dealt with (approved, rejected, applied) folds away before the page moves on
//   - notices dismissed with "Got it" / "No thanks" fold away the same way
//   - numbers count to their new value; the file tabs' highlight slides between tabs
// All of it is off when Windows asks for less motion.
(function () {
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)');
  const calm = () => reduce.matches || !document.body.animate;
  const EASE = 'cubic-bezier(.32,.72,0,1)';  // --ease-ios
  const EXIT = 'cubic-bezier(.4,0,1,1)';
  const SPRING = getComputedStyle(document.documentElement).getPropertyValue('--spring-layout').trim() || EASE;
  const running = new WeakMap();  // details -> {anim, want}

  function wanted(d) { const r = running.get(d); return r ? r.want : d.open; }

  function finish(d, open) { running.delete(d); d.style.overflow = ''; d.open = open; if (d.classList.contains('menu')) place(d); }

  function slide(d, open) {
    const from = d.getBoundingClientRect().height;  // mid-way through an earlier slide, if there is one
    const prev = running.get(d);
    if (prev) { prev.anim.onfinish = null; prev.anim.cancel(); }
    d.open = false; const shut = d.getBoundingClientRect().height;
    d.open = true; const full = d.getBoundingClientRect().height;
    d.style.overflow = 'hidden';
    const anim = d.animate({ height: [from + 'px', (open ? full : shut) + 'px'] },
                           { duration: Math.min(340, 160 + Math.abs(full - shut) / 4), easing: open ? SPRING : EASE });
    running.set(d, { anim: anim, want: open });
    anim.onfinish = function () { finish(d, open); };
    if (open) {  // the contents fade up as the box opens
      const body = Array.from(d.children).filter(c => c.tagName !== 'SUMMARY');
      body.forEach(c => c.animate && c.animate([{ opacity: 0, transform: 'translateY(-4px)' }, { opacity: 1, transform: 'none' }],
                                                { duration: 220, easing: EASE }));
    }
  }

  function pop(d, open) {  // a panel floating over the page (Reject, menus): scale and fade from its button, nothing below moves
    const panel = d.querySelector(':scope > :not(summary)');
    const prev = running.get(d);
    if (prev) { prev.anim.onfinish = null; prev.anim.cancel(); }
    d.open = true;
    if (d.classList.contains('menu')) place(d);
    const frames = [{ opacity: 0, transform: 'translateY(-4px) scale(.96)' }, { opacity: 1, transform: 'none' }];
    const anim = panel.animate(open ? frames : frames.slice().reverse(),
                               { duration: open ? 240 : 130, easing: open ? SPRING : EXIT });
    running.set(d, { anim: anim, want: open });
    anim.onfinish = function () {
      running.delete(d); d.open = open; if (!open && d.classList.contains('menu')) place(d);
      if (open) { const f = panel.querySelector('.mitem:not([disabled]), input, button'); if (f && d.classList.contains('menu')) f.focus({preventScroll: true}); }
    };
  }

  const floating = d => d.classList.contains('reject') || d.classList.contains('menu');

  // A menu inside a table's scroll box would be clipped by it, and the box would grow its own scrollbar.
  // So it floats over the page instead, under its button, or above it when there's no room below.
  // Phones already show it as a sheet along the bottom (style.css).
  function place(d) {
    const panel = d.querySelector(':scope > .menupanel');
    if (!panel) return;
    if (!d.open || !d.closest('.tablewrap') || window.matchMedia('(max-width: 600px)').matches) {
      panel.classList.remove('fixed'); panel.style.top = panel.style.left = panel.style.maxHeight = ''; return;
    }
    panel.classList.add('fixed');
    const b = d.querySelector('summary').getBoundingClientRect();
    const w = panel.offsetWidth, h = panel.scrollHeight, gap = 6, edge = 8, vh = window.innerHeight;
    const below = vh - b.bottom - gap - edge, above = b.top - gap - edge;
    const down = h <= below || below >= above;
    const room = down ? below : above;
    panel.style.maxHeight = h > room ? room + 'px' : '';
    panel.style.top = (down ? b.bottom + gap : b.top - gap - Math.min(h, room)) + 'px';
    panel.style.left = Math.max(edge, Math.min(b.right - w, window.innerWidth - w - edge)) + 'px';
    panel.style.transformOrigin = down ? 'top right' : 'bottom right';
  }
  function placeAll() { document.querySelectorAll('details.menu[open]').forEach(place); }
  window.addEventListener('scroll', placeAll, true);
  window.addEventListener('resize', placeAll);

  function setOpen(d, open) {
    if (wanted(d) === open) return;
    if (open && floating(d)) {  // one floating panel at a time
      document.querySelectorAll('details.reject, details.menu').forEach(function (o) { if (o !== d) setOpen(o, false); });
    }
    if (calm()) { const prev = running.get(d); if (prev) prev.anim.cancel(); finish(d, open); return; }
    if (floating(d)) pop(d, open); else slide(d, open);
  }

  document.addEventListener('click', function (ev) {
    const s = ev.target.closest && ev.target.closest('summary');
    if (s && s.parentElement && s.parentElement.tagName === 'DETAILS') {
      ev.preventDefault();
      setOpen(s.parentElement, !wanted(s.parentElement));
      return;
    }
    // a click outside an open menu or Reject panel closes it
    document.querySelectorAll('details.reject[open], details.menu[open]').forEach(function (d) {
      if (!d.contains(ev.target)) setOpen(d, false);
    });
  });
  document.addEventListener('keydown', function (ev) {
    if (ev.key !== 'Escape') return;
    document.querySelectorAll('details.reject[open], details.menu[open]').forEach(function (d) {
      setOpen(d, false); const s = d.querySelector('summary'); if (s) s.focus();
    });
  });

  // a panel that was hidden and is shown again fades in rather than appearing at once
  function reveal(el) {
    if (!calm() && el && el.animate) el.animate([{ opacity: 0, transform: 'translateY(4px)' }, { opacity: 1, transform: 'none' }],
                                                { duration: 220, easing: EASE });
  }

  // fold an element away: it fades and shrinks a touch while its height closes, so what's below glides up
  function fold(el) {
    return new Promise(function (done) {
      if (calm() || !el) { done(); return; }
      const h = el.getBoundingClientRect().height;
      const cs = getComputedStyle(el);
      el.classList.add('leaving');
      const anim = el.animate([
        { height: h + 'px', opacity: 1, transform: 'none', marginTop: cs.marginTop, marginBottom: cs.marginBottom,
          paddingTop: cs.paddingTop, paddingBottom: cs.paddingBottom },
        { height: h + 'px', opacity: 0, transform: 'scale(.97)', offset: .45 },
        { height: '0px', opacity: 0, transform: 'scale(.97)', marginTop: '0px', marginBottom: '0px', paddingTop: '0px', paddingBottom: '0px' }
      ], { duration: 340, easing: EASE, fill: 'forwards' });
      anim.onfinish = done;
      setTimeout(done, 450);
    });
  }

  // <form data-leave> inside a [data-row]: the row folds away, then the form is sent.
  // <form|a data-dismiss="#id">: that notice folds away, then the form is sent (or the link followed).
  function leaveThen(ev, target, go) {
    if (calm() || !target) return;
    ev.preventDefault();
    fold(target).then(go);
  }
  document.addEventListener('submit', function (ev) {
    if (ev.defaultPrevented) return;
    const f = ev.target;
    if (f.dataset.dismiss) leaveThen(ev, document.querySelector(f.dataset.dismiss), () => HTMLFormElement.prototype.submit.call(f));
    else if (f.hasAttribute('data-leave') && f.closest('[data-row]')) {
      const row = f.closest('[data-row]');
      const d = f.closest('details'); if (d) setOpen(d, false);
      leaveThen(ev, row, () => HTMLFormElement.prototype.submit.call(f));
    }
  });
  document.addEventListener('click', function (ev) {
    const a = ev.target.closest && ev.target.closest('a[data-dismiss]');
    if (a && !ev.ctrlKey && !ev.metaKey) leaveThen(ev, document.querySelector(a.dataset.dismiss), () => { location.href = a.href; });
  });

  // a number rolls to its new value (spring-ish: quick, then settles), instead of jumping
  const shown = new WeakMap();
  function countTo(el, to, format) {
    const from = shown.has(el) ? shown.get(el) : parseFloat((el.textContent || '').replace(/[^0-9.]/g, '')) || 0;
    shown.set(el, to);
    if (from === to) { el.textContent = format(to); return; }
    if (calm()) { el.textContent = format(to); return; }
    const start = performance.now(), dur = 600;
    (function step(now) {
      if (shown.get(el) !== to) return;  // a newer value took over
      const t = Math.min(1, (now - start) / dur), k = 1 - Math.pow(1 - t, 4);
      el.textContent = format(from + (to - from) * k);
      if (t < 1) requestAnimationFrame(step);
    })(start);
  }

  // Segmented tabs (.seg): one highlight that slides to the chosen tab
  function segment(seg) {
    const thumb = document.createElement('span'); thumb.className = 'thumb'; thumb.setAttribute('aria-hidden', 'true');
    seg.prepend(thumb);
    function place(instant) {
      const on = seg.querySelector('button.on'); if (!on) return;
      if (instant) seg.classList.add('static');
      thumb.style.width = on.offsetWidth + 'px';
      thumb.style.transform = 'translateX(' + (on.offsetLeft - 0) + 'px)';
      thumb.style.top = on.offsetTop + 'px'; thumb.style.height = on.offsetHeight + 'px';
      if (instant) requestAnimationFrame(() => requestAnimationFrame(() => seg.classList.remove('static')));
    }
    place(true); seg.classList.add('ready');
    new MutationObserver(() => place(false)).observe(seg, { attributes: true, subtree: true, attributeFilter: ['class'] });
    window.addEventListener('resize', () => place(true));
  }
  document.querySelectorAll('.seg').forEach(segment);

  window.jhMotion = { open: function (d) { setOpen(d, true); }, close: function (d) { setOpen(d, false); },
                      reveal: reveal, fold: fold, countTo: countTo, calm: calm };
})();
