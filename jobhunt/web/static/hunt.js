// Hunt page: live meter + progress log, and the pre-run estimate.
// Page values come from data-* attributes on #huntPage (no inline scripts).
(function () {
  const cfg = document.getElementById('huntPage').dataset;
  const READY = cfg.ready === '1';  // AI key, Profile and Scope all done (else Start stays greyed out)
  const MAX_APPS = cfg.maxApps;
  let lastEvent = parseInt(cfg.lastEvent || '0');
  let wasBusy = cfg.busy === '1';
  const fmt = n => Math.round(n).toLocaleString();
  const $ = id => document.getElementById(id);
  const count = (el, to, f) => window.jhMotion ? window.jhMotion.countTo(el, to, f) : (el.textContent = f(to));

  function line(e) {
    const d = document.createElement('div'); d.className = e.level + ' new';
    const ts = document.createElement('span'); ts.className = 'ts'; ts.textContent = e.ts.slice(11, 19) + ' ';
    d.appendChild(ts); d.appendChild(document.createTextNode(e.message)); return d;
  }

  async function poll() {
    try {
      const s = await (await fetch('/api/status?after=' + lastEvent)).json();
      const log = $('log');
      const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
      if (s.events.length && $('logArt')) $('logArt').remove();
      for (const e of s.events) { log.appendChild(line(e)); lastEvent = e.id; }
      if (atBottom && s.events.length) log.scrollTo({top: log.scrollHeight, behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'});
      const m = s.meter;
      const state = s.busy ? s.kind : (s.run && s.run.status) || 'idle';
      if ($('mState').textContent !== state) $('mState').textContent = state;
      if (m) {
        count($('mTokens'), m.tokens_in + m.tokens_out, fmt);
        count($('mCalls'), m.calls, fmt);
        count($('mCost'), m.run_cost, v => '$' + v.toFixed(4));
        const caps = [];
        if (!m.subscription && m.run_cap) caps.push('$' + m.run_cap.toFixed(2) + ' per run, $' + m.job_cap.toFixed(2) + ' per job');
        if (m.run_token_cap) caps.push(fmt(m.run_token_cap) + ' tokens per run');
        if (m.job_token_cap) caps.push(fmt(m.job_token_cap) + ' tokens per job');
        $('mCaps').textContent = caps.length ? 'Caps: ' + caps.join(' · ') : '';
      }
      const j = s.job;
      $('mJob').textContent = j ? `Now: ${j.company} · ${j.role} (${j.state}) · ${fmt(j.tokens_in + j.tokens_out)} tokens`
        + ` · $${j.cost_usd.toFixed(4)}` : 'No job in progress.';
      $('stopBtn').disabled = !s.busy;
      $('startBtn').disabled = s.busy || !READY;
      if (wasBusy && !s.busy) {
        // Building the profile or drafting the scope: go back to that page with the result.
        const back = {profile: '/profile', scope: '/scope'}[s.last_kind];
        if (back) {
          const key = s.last_status === 'done' ? 'msg' : 'err';
          location.replace(back + '?' + key + '=' + encodeURIComponent(s.last_result || 'Finished.'));
          return;
        }
        // The run ended: show the new packages (a navigation, so the page cross-fades instead of blinking)
        setTimeout(() => location.replace(location.pathname + location.hash), 1200);
      }
      wasBusy = s.busy;
    } catch (err) { /* server restarting; try again */ }
  }

  let estTimer = null, estSeq = 0;
  async function estimate() {
    const seq = ++estSeq;
    const f = $('huntForm');
    const box = $('estimate');
    const sites = [...f.querySelectorAll('input[name=sites]:checked')].map(x => x.value).join(',');
    const auto = f.querySelector('input[name=mode]:checked').value === 'auto' ? 1 : 0;
    const jobs = parseInt($('jobsInput').value || '10');
    let e;
    try {
      e = await (await fetch(`/api/estimate?jobs=${jobs}&sites=${sites}&auto=${auto}`)).json();
    } catch (err) {
      if (seq === estSeq) box.textContent = "Couldn't work out an estimate just now. You can still start.";
      return;
    }
    if (seq !== estSeq) return;  // a newer estimate is on its way
    const time = e.hours ? (e.hours < 1 ? Math.round(e.hours * 60) + ' min' : e.hours.toFixed(1) + ' hours') : '';
    let html = `<span class="big">${time ? 'About ' + time : 'Estimate'}</span>`
      + `for ${jobs} package${jobs === 1 ? '' : 's'}: about <b>$${e.total_usd.toFixed(3)}</b>`
      + (e.subscription ? ' at API prices (your flat plan covers it)' : '')
      + `, ~<b>${e.drafts_per_hour.toFixed(0)}</b> drafts an hour`;
    if (e.applies_per_hour) html += `, ~<b>${e.applies_per_hour.toFixed(0)}</b> auto-applies an hour (at most ${MAX_APPS} an hour per site)`;
    html += '.';
    html += `<br><span class="hint">${e.from_history ? 'Based on your own past runs.' : 'A first guess; it gets better as you use it.'}`
      + ` $${e.per_job_usd.toFixed(4)} and ${fmt(e.per_job_tokens)} tokens a package; real sites are slower when they rate-limit.</span>`;
    if (e.over_run_cap || e.over_job_cap) html += `<br><b>Heads-up:</b> this may hit your caps (Settings); the run stops cleanly if it does.`;
    box.innerHTML = html; box.classList.toggle('warn', e.over_run_cap || e.over_job_cap);
    if (window.jhMotion) window.jhMotion.reveal(box.firstChild);
  }

  $('huntForm').addEventListener('input', function () { clearTimeout(estTimer); estTimer = setTimeout(estimate, 200); });
  estimate(); poll(); setInterval(poll, 1500);
  $('log').scrollTop = 1e9;
})();
