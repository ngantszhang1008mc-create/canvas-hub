// Runs before stylesheets so a saved appearance is applied before first paint.
(() => {
  let theme = 'dark';
  try {
    if (localStorage.getItem('ch.theme') === 'light') theme = 'light';
  } catch { /* Storage may be unavailable; retain the original Dark appearance. */ }
  document.documentElement.dataset.theme = theme;
})();
