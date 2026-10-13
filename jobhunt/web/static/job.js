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
