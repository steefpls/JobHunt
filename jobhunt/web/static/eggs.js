// Small surprises. None of this touches a job, a document or the AI.
(function () {
  const calm = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  function flag(name) {
    fetch('/achievements/flag', {method: 'POST', body: new URLSearchParams({name: name})}).catch(function () {});
  }

  // Achievement pop-ups: fade out after a while (hovering keeps them).
  const toasts = document.getElementById('toasts');
  if (toasts) {
    let hold = false;
    toasts.addEventListener('mouseenter', function () { hold = true; });
    toasts.addEventListener('mouseleave', function () { hold = false; });
    setTimeout(function fade() {
      if (hold) { setTimeout(fade, 1500); return; }
      toasts.querySelectorAll('.toast').forEach(function (t) { t.classList.add('gone'); });
      setTimeout(function () { toasts.remove(); }, 700);
    }, 9000);
  }

  // Five clicks on the logo, or the Konami code, find the achievements page.
  const logo = document.getElementById('logo');
  if (logo) {
    let clicks = 0, timer = null;
    logo.addEventListener('click', function () {
      clicks += 1;
      clearTimeout(timer);
      timer = setTimeout(function () { clicks = 0; }, 1500);
      if (clicks >= 5) location.href = '/achievements';
    });
  }
  const code = ['ArrowUp', 'ArrowUp', 'ArrowDown', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'ArrowLeft', 'ArrowRight', 'b', 'a'];
  let at = 0;
  document.addEventListener('keydown', function (ev) {
    const k = ev.key.length === 1 ? ev.key.toLowerCase() : ev.key;
    at = k === code[at] ? at + 1 : (k === code[0] ? 1 : 0);
    if (at === code.length) {
      at = 0;
      flag('old_school');
      setTimeout(function () { location.href = '/achievements'; }, 250);
    }
  });

  // Rejection decoder on a Rejected job.
  const decoder = document.getElementById('decoder');
  if (decoder) decoder.addEventListener('toggle', function () { if (decoder.open) flag('codebreaker'); });

  // Late at night the sidebar has an opinion.
  const hour = new Date().getHours();
  if (hour < 5 && Math.random() < 0.5) {
    const name = document.body.dataset.name;
    window.jhIdleText = 'Idle (go to sleep' + (name ? ', ' + name : '') + ')';
    const box = document.getElementById('sideRun');
    if (box && !box.querySelector('.dot.live')) box.lastChild.textContent = window.jhIdleText;
  }

  // Confetti for an offer, a smaller burst for an interview.
  const party = document.body.dataset.party;
  if (party) {
    if (party === 'offer') flag('party_time');
    const url = new URL(location.href);
    url.searchParams.delete('party');
    history.replaceState(null, '', url.pathname + url.search + url.hash);  // a reload doesn't throw it again
    if (!calm) confetti(party === 'offer' ? 180 : 60);
  }

  function confetti(count) {
    const c = document.createElement('canvas');
    c.style.cssText = 'position:fixed;inset:0;width:100%;height:100%;pointer-events:none;z-index:60';
    document.body.appendChild(c);
    const ctx = c.getContext('2d');
    const w = c.width = window.innerWidth, h = c.height = window.innerHeight;
    const colours = ['#2f5bea', '#3ddc97', '#f5d27a', '#ff8f86', '#8fb0ff', '#c084fc'];
    const bits = [];
    for (let i = 0; i < count; i++) {
      bits.push({x: w / 2 + (Math.random() - 0.5) * w * 0.3, y: h * 0.35, vx: (Math.random() - 0.5) * 14,
                 vy: -Math.random() * 14 - 4, s: 5 + Math.random() * 6, r: Math.random() * 6, vr: (Math.random() - 0.5) * 0.3,
                 c: colours[i % colours.length]});
    }
    const start = performance.now();
    (function frame(now) {
      const t = now - start;
      ctx.clearRect(0, 0, w, h);
      ctx.globalAlpha = Math.max(0, 1 - Math.max(0, t - 2200) / 800);
      bits.forEach(function (b) {
        b.vy += 0.35; b.vx *= 0.99; b.x += b.vx; b.y += b.vy; b.r += b.vr;
        ctx.save(); ctx.translate(b.x, b.y); ctx.rotate(b.r); ctx.fillStyle = b.c;
        ctx.fillRect(-b.s / 2, -b.s / 4, b.s, b.s / 2); ctx.restore();
      });
      if (t < 3000) requestAnimationFrame(frame); else c.remove();
    })(start);
  }
})();
