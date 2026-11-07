(() => {
  const root = document.documentElement;
  const button = document.querySelector('.theme-toggle');
  if (!button) return;
  const current = () => root.dataset.theme || 'light';
  const update = () => {
    const dark = current() === 'dark';
    button.setAttribute('aria-pressed', String(dark));
    button.setAttribute('aria-label', `Switch to ${dark ? 'light' : 'dark'} mode`);
    button.querySelector('.theme-label').textContent = dark ? 'Light mode' : 'Dark mode';
  };
  button.addEventListener('click', () => {
    const next = current() === 'dark' ? 'light' : 'dark';
    root.dataset.theme = next;
    try { localStorage.setItem('w4-theme', next); } catch (_) {}
    update();
  });
  window.addEventListener('storage', event => {
    if (event.key !== 'w4-theme') return;
    if (event.newValue === 'dark' || event.newValue === 'light') root.dataset.theme = event.newValue;
    else delete root.dataset.theme;
    update();
  });
  update();
})();
