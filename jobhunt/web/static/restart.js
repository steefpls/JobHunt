// Restarting page: once a new JobHunt answers (a different start time), open it.
(function () {
  const box = document.getElementById('restartState');
  const oldStart = document.getElementById('restarting').dataset.started;
  const begun = Date.now();
  async function poll() {
    try {
      const v = await (await fetch('/api/version', {cache: 'no-store'})).json();
      if (String(v.started) !== oldStart) { location.replace('/'); return; }
    } catch (e) {
      box.textContent = 'Installing the new version and starting it…';
    }
    if (Date.now() - begun > 120000) {
      box.textContent = 'This is taking longer than it should: check the black JobHunt window.';
    }
    setTimeout(poll, 1500);
  }
  setTimeout(poll, 1500);
})();
