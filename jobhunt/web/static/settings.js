// Settings page: show only what applies to the choices made, and catch a mistake before saving.
(function () {
  const provider = document.getElementById('f_provider');
  const reset = document.getElementById('resetModels'), base = document.getElementById('baseUrl');
  function shown(el, on) {
    if (on === !el.hidden) return;
    el.hidden = !on;
    if (on && window.jhMotion) window.jhMotion.reveal(el);
  }
  function sync() {
    shown(reset, provider.value !== provider.dataset.initial);  // only asked when the provider changes
    shown(base, provider.value === 'openai_compatible');
  }
  provider.addEventListener('change', sync);
  sync();

  const lo = document.getElementById('f_min_delay_s'), hi = document.getElementById('f_max_delay_s');
  const err = document.getElementById('delayErr');
  function check() { shown(err, parseFloat(hi.value) < parseFloat(lo.value)); }
  lo.addEventListener('input', check); hi.addEventListener('input', check);
})();
