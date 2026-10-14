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

  function line(e) {
    const d = document.createElement('div'); d.className = e.level;
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
      if (atBottom) log.scrollTop = log.scrollHeight;
      const m = s.meter;
      $('mState').textContent = s.busy ? s.kind : (s.run && s.run.status) || 'idle';
      if (m) {
        $('mTokens').textContent = fmt(m.tokens_in + m.tokens_out);
        $('mCalls').textContent = fmt(m.calls);
        $('mCost').textContent = '$' + m.run_cost.toFixed(4);
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
        setTimeout(() => location.reload(), 1200);
      }
      wasBusy = s.busy;
    } catch (err) { /* server restarting; try again */ }
  }

  async function estimate() {
    const f = $('huntForm');
    const sites = [...f.querySelectorAll('input[name=sites]:checked')].map(x => x.value).join(',');
    const auto = f.querySelector('input[name=mode]:checked').value === 'auto' ? 1 : 0;
    const jobs = parseInt($('jobsInput').value || '10');
    const e = await (await fetch(`/api/estimate?jobs=${jobs}&sites=${sites}&auto=${auto}`)).json();
    const box = $('estimate');
    const cost = `about <b>$${e.total_usd.toFixed(3)}</b> ($${e.per_job_usd.toFixed(4)} per job, ${fmt(e.per_job_tokens)} tokens, `
      + `~${e.calls_per_job.toFixed(0)} AI calls each)` + (e.subscription ? ' at API prices; your flat plan covers it' : '');
    let speed = `<b>~${e.drafts_per_hour.toFixed(0)}</b> drafts/hour`;
    if (e.applies_per_hour) speed += `, <b>~${e.applies_per_hour.toFixed(0)}</b> auto-applies/hour (capped at ${MAX_APPS}/hour per site)`;
    let html = `Estimate for ${jobs} job(s): ${cost}. Speed: ${speed}`
      + (e.hours ? `, so roughly <b>${e.hours < 1 ? Math.round(e.hours * 60) + ' min' : e.hours.toFixed(1) + ' h'}</b>.` : '.');
    html += `<br><span class="hint">${e.from_history ? 'Based on your own past runs.' : 'First-run guess; it gets better as you use it.'}`
      + ` Assumes ~${e.candidates_per_match.toFixed(1)} postings read per match. Real sites are slower than this guess when they rate-limit.</span>`;
    if (e.over_run_cap || e.over_job_cap) html += `<br><b>Heads-up:</b> this may hit your caps (Settings); the run will stop cleanly if it does.`;
    box.innerHTML = html; box.classList.toggle('warn', e.over_run_cap || e.over_job_cap);
  }

  $('huntForm').addEventListener('input', estimate);
  estimate(); poll(); setInterval(poll, 1500);
  $('log').scrollTop = 1e9;
})();
