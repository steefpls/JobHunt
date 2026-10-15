// Job page: file tabs (PDF previews in a frame, the match report inline).
(function () {
  const tabs = document.querySelectorAll('#tabs button');
  const frame = document.getElementById('preview');
  const report = document.getElementById('reportView');
  if (!frame || !report) return;
  function show(btn) {
    tabs.forEach(b => b.classList.toggle('on', b === btn));
    if (btn.dataset.name === 'match_report.md') { frame.style.display = 'none'; report.style.display = 'block'; }
    else { report.style.display = 'none'; frame.style.display = 'block'; frame.src = btn.dataset.src; }
  }
  tabs.forEach(b => b.addEventListener('click', () => show(b)));
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
