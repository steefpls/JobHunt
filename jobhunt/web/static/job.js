// Job page: file tabs (PDF previews in a frame, the match report inline).
(function () {
  const tabs = document.querySelectorAll('#tabs button');
  const frame = document.getElementById('preview');
  const box = document.getElementById('previewBox');
  const report = document.getElementById('reportView');
  if (!frame || !report) return;
  function reveal(el) { if (window.jhMotion) window.jhMotion.reveal(el); }
  // While a PDF loads, a placeholder page shimmers where it will be, instead of a white flash.
  frame.addEventListener('load', function () { box.classList.remove('loading'); });
  function show(btn) {
    tabs.forEach(b => { b.classList.toggle('on', b === btn); b.setAttribute('aria-selected', b === btn ? 'true' : 'false'); });
    if (btn.dataset.name === 'match_report.md') { box.style.display = 'none'; report.style.display = 'block'; reveal(report); }
    else {
      report.style.display = 'none'; box.style.display = 'block';
      if (frame.getAttribute('src') !== btn.dataset.src) {
        box.classList.add('loading'); frame.src = btn.dataset.src;
        setTimeout(() => box.classList.remove('loading'), 4000);  // a PDF viewer that never says it loaded
      }
      reveal(box);
    }
  }
  tabs.forEach(b => b.addEventListener('click', () => show(b)));
  // Arrow keys move between the tabs, as in any tab bar.
  document.getElementById('tabs').addEventListener('keydown', function (ev) {
    const list = Array.from(tabs), i = list.indexOf(document.activeElement);
    if (i < 0 || (ev.key !== 'ArrowRight' && ev.key !== 'ArrowLeft')) return;
    const next = list[(i + (ev.key === 'ArrowRight' ? 1 : list.length - 1)) % list.length];
    next.focus(); show(next); ev.preventDefault();
  });
  if (tabs.length) show(tabs[0]);
})();

// Job page: while JobHunt looks the company up, wait, then show the page again with the summary.
(function () {
  const busy = document.getElementById('companyBusy');
  if (!busy) return;
  const timer = setInterval(async function () {
    try {
      const s = await (await fetch('/api/status?after=999999999')).json();
      if (s.busy) return;
      clearInterval(timer);
      let q = '';
      if (s.last_kind === 'company') q = '?' + (s.last_status === 'done' ? 'msg' : 'err') + '=' + encodeURIComponent(s.last_result || 'Finished.');
      location.replace('/jobs/' + busy.dataset.job + q + '#company');
    } catch (e) { /* server restarting; try again */ }
  }, 2000);
})();
