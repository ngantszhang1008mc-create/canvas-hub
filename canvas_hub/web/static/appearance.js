import { icon } from './icons.js';

const KEY = 'ch.theme';
export const currentTheme = () => document.documentElement.dataset.theme === 'light' ? 'light' : 'dark';

function updateButtons() {
  for (const button of document.querySelectorAll('.theme-switcher button')) {
    button.setAttribute('aria-pressed', String(button.dataset.theme === currentTheme()));
  }
}

function applyTheme(theme, remember = true) {
  document.documentElement.dataset.theme = theme === 'light' ? 'light' : 'dark';
  if (remember) {
    try { localStorage.setItem(KEY, currentTheme()); } catch { /* Still switches for this visit. */ }
  }
  updateButtons();
  window.dispatchEvent(new Event('ch-theme-change'));
  // Refits the existing chat to any font/control size changes without replacing
  // its DOM, losing draft text, resetting forms or interrupting a response.
  requestAnimationFrame(() => window.dispatchEvent(new Event('resize')));
}

export function themeSwitcher() {
  const group = document.createElement('div');
  group.className = 'theme-switcher';
  group.setAttribute('role', 'group');
  group.setAttribute('aria-label', 'Appearance');
  for (const [theme, label, glyph] of [['light', 'Light', 'sun'], ['dark', 'Dark', 'moon']]) {
    const button = document.createElement('button');
    button.type = 'button';
    button.dataset.theme = theme;
    button.setAttribute('aria-label', label);
    button.title = theme === 'light' ? 'Use the new Light appearance' : 'Use the original Dark appearance';
    button.setAttribute('aria-pressed', String(currentTheme() === theme));
    const text = document.createElement('span');
    text.className = 'theme-name';
    text.textContent = label;
    button.append(icon(glyph), text);
    button.addEventListener('click', () => applyTheme(theme));
    group.append(button);
  }
  return group;
}

window.addEventListener('storage', (event) => {
  if (event.key === KEY || event.key === null) applyTheme(event.newValue, false);
});
