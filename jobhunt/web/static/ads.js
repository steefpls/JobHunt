// Is the ad still open? To review asks about the jobs waiting there, a job page about its own job
// (MyCareersFuture and LinkedIn answer in a second; JobStreet's are checked as a hunt starts).
(function () {
  const anyIcon = document.querySelector('svg use');
  const sprite = anyIcon ? anyIcon.getAttribute('href').split('#')[0] : '/static/icons.svg';

  function icon(name, cls) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'i' + (cls ? ' ' + cls : ''));
    svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', sprite + '#' + name);
    svg.appendChild(use);
    return svg;
  }

  function sentence(r) { return r.why ? r.why.charAt(0).toUpperCase() + r.why.slice(1) : 'The site says so'; }

  async function ask(job) {
    try {
      const res = await fetch('/api/ads/check' + (job ? '?job=' + job : ''), {method: 'POST'});
      return res.ok ? await res.json() : {};
    } catch (e) { return {}; }
  }

  function markCard(id, r) {  // a card on To review gets the red "Ad closed" tag
    const line = document.querySelector('.facts-line[data-ad-job="' + id + '"]');
    if (!line || line.querySelector('.pill.closed')) return;
    const pill = document.createElement('span');
    pill.className = 'pill closed pop';
    pill.title = r.site + ': ' + r.why;
    pill.append(icon('circle-x'), 'Ad closed');
    line.prepend(pill);
    line.hidden = false;
  }

  function countClosed() {  // To review's "Reject all closed" bar shows once any card says Ad closed
    const bar = document.getElementById('closedBar');
    if (!bar) return;
    const n = document.querySelectorAll('.facts-line .pill.closed').length;
    bar.querySelector('[data-closed-text]').textContent = n + (n === 1 ? ' job here has' : ' jobs here have') + ' a closed ad.';
    bar.hidden = !n;
  }

  function markJob(r) {  // the job page says so above what to do next
    const what = document.querySelector('#next .what');
    if (!what || document.getElementById('adClosed')) return;
    const box = document.createElement('div');
    box.className = 'adclosed pop';
    box.id = 'adClosed';
    const text = document.createElement('span');
    const b = document.createElement('b');
    b.textContent = 'This ad has closed. ';
    text.append(b, sentence(r) + ' (checked just now).');
    box.append(icon('circle-x', 'sm'), text);
    what.prepend(box);
  }

  const bar = document.querySelector('#next[data-ad-job]');
  if (bar) {
    const id = bar.dataset.adJob;
    if (bar.dataset.adCheck === '1') ask(id).then(function (all) { const r = all[id]; if (r && r.state === 'closed') markJob(r); });
    const btn = document.querySelector('[data-ad-now]');
    if (btn) btn.addEventListener('click', async function () {
      btn.disabled = true;
      const r = (await ask(id))[id];
      btn.disabled = false;
      const last = btn.lastChild;
      if (!r || r.state === 'unknown') last.textContent = "Couldn't tell" + (r && r.why ? ': ' + r.why : '');
      else if (r.state === 'open') { last.textContent = 'Still open (checked just now)'; const c = document.getElementById('adClosed'); if (c) c.remove(); }
      else { last.textContent = 'Closed'; markJob(r); }
    });
  } else if (document.querySelector('.facts-line[data-ad-job]')) {
    ask('').then(function (all) {
      Object.keys(all).forEach(function (id) { if (all[id] && all[id].state === 'closed') markCard(id, all[id]); });
      countClosed();
    });
  }
})();
