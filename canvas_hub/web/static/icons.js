// Decorative, local SVG icons. No remote fonts, images or icon services.
const shapes = {
  sun: [['circle',{cx:12,cy:12,r:4}],['path',{d:'M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5'}]],
  moon: [['path',{d:'M21 13a9 9 0 1 1-10-10 7 7 0 0 0 10 10Z'}]],
  home: [['path', {d:'m3 10 9-7 9 7M5 9v12h5v-7h4v7h5V9'}]],
  calendar: [['rect',{x:3,y:5,width:18,height:16,rx:2}],['path',{d:'M16 3v4M8 3v4M3 11h18M8 15h2M14 15h2M8 18h2'}]],
  planner: [['rect',{x:3,y:5,width:18,height:16,rx:2}],['path',{d:'M16 3v4M8 3v4M3 11h18m-13 5 2 2 5-5'}]],
  chart: [['rect',{x:3,y:12,width:4,height:9,rx:1}],['rect',{x:10,y:7,width:4,height:14,rx:1}],['rect',{x:17,y:3,width:4,height:18,rx:1}]],
  book: [['path',{d:'M12 5c-3-2-7-2-10 0v15c3-2 7-2 10 0 3-2 7-2 10 0V5c-3-2-7-2-10 0Zm0 0v15'}]],
  search: [['circle',{cx:10.5,cy:10.5,r:7}],['path',{d:'m16 16 5 5'}]],
  clock: [['circle',{cx:12,cy:12,r:9}],['path',{d:'M12 7v6h4'}]],
  shield: [['path',{d:'m12 3 8 4v6c0 4-4 7-8 9-4-2-8-5-8-9V7l8-4Zm-4 9 3 3 5-6'}]],
  checklist: [['path',{d:'M9 4H4v17h16V10M8 12l3 3L21 4'}]],
  folder: [['path',{d:'M3 7V4h6l3 3h9v14H3V7Zm0 3h18'}]],
  bot: [['rect',{x:3,y:7,width:18,height:14,rx:4}],['path',{d:'M12 7V3m-3 0h6M1 12v4m22-4v4M8 17h8'}],['circle',{cx:8,cy:12,r:.8}],['circle',{cx:16,cy:12,r:.8}]],
  settings: [['path',{d:'m9 3-1 3-3 1-2 3 2 2-1 3 3 2 1 3h4l1-3 3-1 2-3-2-2 1-3-3-2-1-3H9Z'}],['circle',{cx:11,cy:11.5,r:3}]],
  message: [['path',{d:'M21 11a9 9 0 0 1-9 9 11 11 0 0 1-4-.8L3 21l1.8-5A9 9 0 1 1 21 11Z'}],['path',{d:'M7 11h.01M12 11h.01M17 11h.01','stroke-width':3}]],
  plus: [['path',{d:'M12 5v14M5 12h14'}]],
  send: [['path',{d:'m22 2-7 20-4-9L2 9l20-7ZM11 13 22 2'}]],
  maximize: [['path',{d:'M8 3H3v5M16 21h5v-5M3 3l6 6m12 12-6-6M16 3h5v5M8 21H3v-5'}]],
  megaphone: [['path',{d:'m3 9 12-5v16L3 15V9Zm12-2h4v10h-4M6 16l2 6h4l-2-5M22 9v6'}]],
  sliders: [['path',{d:'M3 6h5m4 0h9M3 12h11m4 0h3M3 18h3m4 0h11'}],['circle',{cx:10,cy:6,r:2}],['circle',{cx:16,cy:12,r:2}],['circle',{cx:8,cy:18,r:2}]],
  cube: [['path',{d:'m12 2 9 5v10l-9 5-9-5V7l9-5Zm0 10L3 7m9 5 9-5m-9 5v10M7.5 4.5l9 5'}]],
  bulb: [['path',{d:'M9 18h6m-6 3h6M8 15a7 7 0 1 1 8 0c-1 1-1 2-1 3H9c0-1 0-2-1-3Z'}]],
  brain: [['path',{d:'M12 4c-3-3-7 0-6 3-4 1-4 6-1 8-1 4 4 7 7 4 3 3 8 0 7-4 3-2 3-7-1-8 1-3-3-6-6-3Zm0 0v15M6 7l3 2m9-2-3 2M5 15l4-2m10 2-4-2'}]],
  sparkles: [
    ['path',{d:'M26 18c3 10 5 12 15 15-10 3-12 5-15 15-3-10-5-12-15-15 10-3 12-5 15-15Z',fill:'#31c66a',stroke:'none'}],
    ['path',{d:'M44 2c1.5 5 3 6.5 8 8-5 1.5-6.5 3-8 8-1.5-5-3-6.5-8-8 5-1.5 6.5-3 8-8Z',fill:'#61aaff',stroke:'none'}],
    ['path',{d:'M56 22c1.3 4 2.5 5.2 6.5 6.5-4 1.3-5.2 2.5-6.5 6.5-1.3-4-2.5-5.2-6.5-6.5 4-1.3 5.2-2.5 6.5-6.5Z',fill:'#ff8eb8',stroke:'none'}],
  ],
};

export function icon(name, className = '') {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  for (const [key, value] of Object.entries({viewBox: name === 'sparkles' ? '0 0 64 52' : '0 0 24 24', fill:'none', stroke:'currentColor', 'stroke-width':1.7, 'stroke-linecap':'round', 'stroke-linejoin':'round', 'aria-hidden':'true', focusable:'false', class:`ui-icon ${className}`})) svg.setAttribute(key, value);
  for (const [tag, attrs] of shapes[name] || shapes.settings) {
    const el = document.createElementNS(ns, tag);
    for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, value);
    svg.append(el);
  }
  return svg;
}

export function decorateNavigation() {
  const names = {overview:'home', assistant:'sparkles', deadlines:'calendar', planner:'planner', trackers:'chart', courses:'book', search:'search', changes:'clock', coverage:'shield', notes:'checklist', collect:'folder', requests:'bot', settings:'settings'};
  for (const link of document.querySelectorAll('.nav a[data-view]')) link.prepend(icon(names[link.dataset.view]));
}
