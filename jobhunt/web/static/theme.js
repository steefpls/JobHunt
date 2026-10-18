// Loaded in <head>: picks light or dark before the page draws, so it never flashes white.
// 'jh-theme' is auto (follow Windows), dark or light; the switch under the menu (app.js) sets it.
(function () { var m = matchMedia('(prefers-color-scheme: dark)');
  function set() { var t = localStorage.getItem('jh-theme') || 'auto';
    document.documentElement.dataset.theme = t === 'auto' ? (m.matches ? 'dark' : 'light') : t; }
  set(); window.jhTheme = set; if (m.addEventListener) m.addEventListener('change', set); })();
