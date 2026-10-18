// Smooth motion: every <details> slides open and shut (the Reject panel fades in and out),
// and shown-again panels fade in, so nothing jumps into place. Off when Windows asks for less motion.
(function () {
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)');
  const EASE = 'cubic-bezier(.2,.7,.3,1)';
  const running = new WeakMap();  // details -> {anim, want}

  function wanted(d) { const r = running.get(d); return r ? r.want : d.open; }

  function finish(d, open) { running.delete(d); d.style.overflow = ''; d.open = open; }

  function slide(d, open) {
    const from = d.getBoundingClientRect().height;  // mid-way through an earlier slide, if there is one
    const prev = running.get(d);
    if (prev) { prev.anim.onfinish = null; prev.anim.cancel(); }
    d.open = false; const shut = d.getBoundingClientRect().height;
    d.open = true; const full = d.getBoundingClientRect().height;
    d.style.overflow = 'hidden';
    const anim = d.animate({ height: [from + 'px', (open ? full : shut) + 'px'] },
                           { duration: Math.min(320, 140 + Math.abs(full - shut) / 4), easing: EASE });
    running.set(d, { anim: anim, want: open });
    anim.onfinish = function () { finish(d, open); };
  }

  function pop(d, open) {  // a panel floating over the page (Reject): fade and drop in, nothing below moves
    const panel = d.querySelector(':scope > :not(summary)');
    const prev = running.get(d);
    if (prev) { prev.anim.onfinish = null; prev.anim.cancel(); }
    d.open = true;
    const frames = [{ opacity: 0, transform: 'translateY(-6px)' }, { opacity: 1, transform: 'none' }];
    const anim = panel.animate(open ? frames : frames.slice().reverse(), { duration: open ? 160 : 120, easing: EASE });
    running.set(d, { anim: anim, want: open });
    anim.onfinish = function () { running.delete(d); d.open = open; };
  }

  function setOpen(d, open) {
    if (wanted(d) === open) return;
    if (open && d.classList.contains('reject')) {  // one Reject panel at a time
      document.querySelectorAll('details.reject').forEach(function (o) { if (o !== d) setOpen(o, false); });
    }
    if (reduce.matches || !d.animate) { const prev = running.get(d); if (prev) prev.anim.cancel(); finish(d, open); return; }
    if (d.classList.contains('reject')) pop(d, open); else slide(d, open);
  }

  document.addEventListener('click', function (ev) {
    const s = ev.target.closest && ev.target.closest('summary');
    if (!s || !s.parentElement || s.parentElement.tagName !== 'DETAILS') return;
    ev.preventDefault();
    setOpen(s.parentElement, !wanted(s.parentElement));
  });

  // a panel that was hidden and is shown again fades in rather than appearing at once
  function reveal(el) {
    if (!reduce.matches && el && el.animate) el.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 180, easing: 'ease-out' });
  }

  window.jhMotion = { open: function (d) { setOpen(d, true); }, close: function (d) { setOpen(d, false); }, reveal: reveal };
})();
