// Runs before stylesheets so a saved appearance is applied before first paint.
(() => {
  let theme = document.documentElement.dataset.demoMode === '1' ? 'light' : 'dark';
  try {
    const saved = localStorage.getItem('ch.theme');
    if (saved === 'light' || saved === 'dark') theme = saved;
  } catch { /* Storage may be unavailable; keep default appearance. */ }
  document.documentElement.dataset.theme = theme;
})();
