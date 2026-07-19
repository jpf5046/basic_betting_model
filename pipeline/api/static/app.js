/* My Model — Researcher Console SPA.
   Vanilla JS, no build step. Every figure comes from a real endpoint
   (pipeline/api); charts are hand-rolled inline SVG (no chart library),
   matching the design's terminal aesthetic. */

'use strict';

// ------------------------------------------------------------------ utils
const $ = (sel, root = document) => root.querySelector(sel);
const WEIGHT_KEYS = ['season', 'common', 'form', 'home', 'h2h'];
const WEIGHT_COLORS = { season: '#4C8DFF', common: '#46C9C0', form: '#E3B341', home: '#9B7BEA', h2h: '#7D879C' };
const SPORTS = ['MLB', 'NBA', 'NHL', 'WNBA'];

function h(tag, props, ...kids) {
  const e = document.createElement(tag);
  if (props) for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    if (k === 'class') e.className = v;
    else if (k === 'html') e.innerHTML = v;
    else if (k.startsWith('on') && typeof v === 'function') e.addEventListener(k.slice(2), v);
    else if (k === 'style' && typeof v === 'object') Object.assign(e.style, v);
    else e.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    e.appendChild(typeof kid === 'string' || typeof kid === 'number' ? document.createTextNode(String(kid)) : kid);
  }
  return e;
}
const clear = (el) => { while (el.firstChild) el.removeChild(el.firstChild); return el; };

async function api(path, opts) {
  const r = await fetch('/api' + path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
    body: opts && opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  return r.status === 204 ? null : r.json();
}

function toast(msg, kind = 'ok') {
  const t = h('div', { class: `toast ${kind}` }, h('span', { class: 'dot ' + (kind === 'err' ? 'red' : 'green') }), msg);
  $('#toasts').appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; setTimeout(() => t.remove(), 200); }, 2600);
}

const fnum = (v, d = 2) => (v == null || v === '' || isNaN(v)) ? '—' : Number(v).toFixed(d);
const fint = (v) => (v == null || v === '' || isNaN(v)) ? '—' : String(Math.round(v));
const fpct = (v, d = 1) => (v == null || isNaN(v)) ? '—' : (v >= 0 ? '+' : '') + Number(v).toFixed(d) + '%';
const fmoney = (v) => (v == null || isNaN(v)) ? '—' : (v >= 0 ? '+$' : '−$') + Math.abs(v).toFixed(0);
const signClass = (v) => v == null || isNaN(v) ? '' : (v > 0 ? 'pos' : v < 0 ? 'neg' : '');

// ------------------------------------------------------------------ state
const state = {
  meta: null,
  defaults: null,
  liveParams: null,
  editor: { drafts: {}, loadedName: null, activeSport: 'MLB' },
  picks: { sport: 'MLB', date: null },
};

// --------------------------------------------------------------- SVG charts
function lineChart(series, opts = {}) {
  // series: [{points:[{x,cumulative}], color, dashed, label}]
  const W = opts.width || 720, H = opts.height || 220, pad = { l: 48, r: 56, t: 14, b: 24 };
  const all = series.flatMap(s => s.points);
  if (!all.length) return h('div', { class: 'empty' }, 'no data to chart');
  const xs = all.map((_, i) => i);
  const n = Math.max(...series.map(s => s.points.length));
  const ys = all.map(p => p.cumulative);
  let ymin = Math.min(0, ...ys), ymax = Math.max(0, ...ys);
  if (ymin === ymax) { ymin -= 1; ymax += 1; }
  const px = (i, len) => pad.l + (len <= 1 ? 0 : (i / (len - 1)) * (W - pad.l - pad.r));
  const py = (v) => pad.t + (1 - (v - ymin) / (ymax - ymin)) * (H - pad.t - pad.b);
  const svg = (t, a, ...k) => { const e = document.createElementNS('http://www.w3.org/2000/svg', t); for (const [kk, vv] of Object.entries(a || {})) e.setAttribute(kk, vv); k.forEach(c => c && e.appendChild(c)); return e; };
  const root = svg('svg', { viewBox: `0 0 ${W} ${H}`, width: '100%', style: 'min-width:520px' });
  // zero gridline
  const zy = py(0);
  root.appendChild(svg('line', { x1: pad.l, y1: zy, x2: W - pad.r, y2: zy, stroke: '#232A38', 'stroke-dasharray': '2 3' }));
  root.appendChild(svg('text', { x: pad.l - 8, y: zy + 3, fill: '#59637A', 'font-size': 10, 'text-anchor': 'end', 'font-family': 'IBM Plex Mono' }, document.createTextNode('$0')));
  // y max/min labels
  root.appendChild(svg('text', { x: pad.l - 8, y: py(ymax) + 3, fill: '#59637A', 'font-size': 10, 'text-anchor': 'end', 'font-family': 'IBM Plex Mono' }, document.createTextNode('$' + Math.round(ymax))));
  root.appendChild(svg('text', { x: pad.l - 8, y: py(ymin) + 3, fill: '#59637A', 'font-size': 10, 'text-anchor': 'end', 'font-family': 'IBM Plex Mono' }, document.createTextNode('$' + Math.round(ymin))));
  for (const s of series) {
    if (!s.points.length) continue;
    const d = s.points.map((p, i) => `${i ? 'L' : 'M'}${px(i, s.points.length).toFixed(1)},${py(p.cumulative).toFixed(1)}`).join(' ');
    root.appendChild(svg('path', { d, fill: 'none', stroke: s.color, 'stroke-width': s.dashed ? 1.5 : 2, 'stroke-dasharray': s.dashed ? '4 4' : '' }));
    const last = s.points[s.points.length - 1];
    root.appendChild(svg('text', { x: px(s.points.length - 1, s.points.length) + 5, y: py(last.cumulative) + 3, fill: s.color, 'font-size': 10, 'font-family': 'IBM Plex Mono' }, document.createTextNode(fmoney(last.cumulative))));
  }
  return h('div', { class: 'svgwrap' }, root);
}

function blendBar(blend) {
  const total = blend.reduce((a, b) => a + (b.weight || 0), 0) || 1;
  const bar = h('div', { class: 'blendbar' }, ...blend.map(b =>
    h('span', { style: { width: (100 * (b.weight || 0) / total) + '%', background: WEIGHT_COLORS[b.key] }, title: `${b.label} ${fnum(b.weight, 2)}` })));
  const legend = h('div', { class: 'legend' }, ...blend.map(b =>
    h('div', { class: 'it' }, h('span', { class: 'sw', style: { background: WEIGHT_COLORS[b.key] } }),
      `${b.label} `, h('span', { class: 'num' }, fnum(b.weight, 2)))));
  return h('div', {}, bar, legend);
}

// --------------------------------------------------------------- app shell
const SCREENS = [
  { id: 'parameters', title: 'Parameters', sub: 'Tune the model — weights, adjustments, gates', key: '1', group: 'r' },
  { id: 'backtest', title: 'Backtest', sub: 'Replay a config against history', key: '2', group: 'r' },
  { id: 'configs', title: 'Configs', sub: 'Save, promote, and compare configurations', key: '3', group: 'r' },
  { id: 'live', title: 'Live', sub: "Today's real pick sheet per sport", key: '4', group: 'r' },
  { id: 'runs', title: 'Runs', sub: 'Every daily pipeline run, stage by stage', key: '5', group: 'o' },
  { id: 'picks', title: 'Picks & Predictions', sub: 'Every game, every pick, and its reasoning', key: '6', group: 'o' },
  { id: 'database', title: 'Database', sub: 'The SQLite mirror — teams, games, frames', key: '7', group: 'o' },
  { id: 'performance', title: 'Performance / ROI', sub: 'Graded results, P&L and ROI', key: '8', group: 'o' },
  { id: 'weights', title: 'Model weights', sub: 'What the model is doing right now', key: '9', group: 'o' },
];

function buildNav() {
  const mk = (s) => h('div', { class: 'nav-item', 'data-screen': s.id, onclick: () => go(s.id) },
    h('span', {}, s.title), h('span', { class: 'key' }, s.key));
  clear($('#nav-researcher')); SCREENS.filter(s => s.group === 'r').forEach(s => $('#nav-researcher').appendChild(mk(s)));
  clear($('#nav-observ')); SCREENS.filter(s => s.group === 'o').forEach(s => $('#nav-observ').appendChild(mk(s)));
}

function renderLiveChips() {
  const map = (state.meta && state.meta.live_map) || {};
  const chips = SPORTS.map(s => h('div', { class: 'chip' },
    h('span', { class: 'dot ' + (map[s] ? 'green' : 'muted') }),
    h('span', { class: 'sport' }, s),
    h('span', { class: 'cfg' }, map[s] || 'factory')));
  clear($('#live-chips')); chips.forEach(c => $('#live-chips').appendChild(c));
  const list = SPORTS.map(s => h('div', { class: 'row' },
    h('span', { class: 'dot ' + (map[s] ? 'green' : 'muted') }),
    h('span', { class: 'sport' }, s), h('span', { class: 'cfg' }, map[s] || 'factory')));
  clear($('#nav-live-list')); list.forEach(c => $('#nav-live-list').appendChild(c));
}

let current = 'parameters';
function go(id) { location.hash = '#/' + id; }
async function route() {
  const id = (location.hash.replace(/^#\//, '') || 'parameters').split('?')[0];
  const screen = SCREENS.find(s => s.id === id) || SCREENS[0];
  current = screen.id;
  document.querySelectorAll('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.screen === screen.id));
  $('#screen-title').textContent = screen.title;
  $('#screen-sub').textContent = screen.sub;
  const content = clear($('#content'));
  content.appendChild(h('div', { class: 'empty' }, h('span', { class: 'spin' }), ' loading…'));
  try {
    await RENDER[screen.id](content);
  } catch (e) {
    clear(content).appendChild(h('div', { class: 'empty' }, h('div', { class: 'big neg' }, 'Failed to load'), String(e.message || e)));
  }
}

// ================================================================ SCREENS
const RENDER = {};

// ---------------------------------------------------------------- helpers
function panel(title, headExtra, body) {
  return h('div', { class: 'panel' },
    h('div', { class: 'head' }, h('h2', {}, title), headExtra ? h('span', { class: 'spacer' }) : null, headExtra),
    h('div', { class: 'body' }, body));
}
function tile(k, v, sub, cls) {
  return h('div', { class: 'tile' }, h('div', { class: 'k' }, k), h('div', { class: 'v ' + (cls || '') }, v), sub ? h('div', { class: 'sub' }, sub) : null);
}
function betBadge(lean) {
  const m = { STRONG: 'strong', LEAN: 'lean', 'TOSS-UP': 'tossup', OVER: 'over', UNDER: 'under', PASS: 'tossup' };
  return h('span', { class: 'badge ' + (m[lean] || 'tossup') }, lean);
}

// =============================================================== 1. PARAMETERS
function factoryFor(sport) { return state.defaults[sport]; }
function ensureDraft(sport) {
  if (!state.editor.drafts[sport]) {
    const base = state.editor.loadedName && state.editor.loadedParams ? state.editor.loadedParams[sport] : state.liveParams[sport];
    state.editor.drafts[sport] = JSON.parse(JSON.stringify(base));
  }
  return state.editor.drafts[sport];
}
function modifiedCount() {
  let n = 0;
  for (const sport of SPORTS) {
    const d = state.editor.drafts[sport]; if (!d) continue;
    const f = factoryFor(sport);
    for (const k of WEIGHT_KEYS) if (Math.abs(d.weights[k] - f.weights[k]) > 1e-9) n++;
    for (const k of ['home_adv', 'form_base', 'form_range', 'k']) if (Math.abs(d[k] - f[k]) > 1e-9) n++;
    for (const [g, k] of [['ml_gates', 'strong'], ['ml_gates', 'lean'], ['totals', 'over'], ['totals', 'under']])
      if (Math.abs(d[g][k] - f[g][k]) > 1e-9) n++;
  }
  return n;
}

RENDER.parameters = async (content) => {
  if (!state.defaults) state.defaults = await api('/config/defaults');
  if (!state.liveParams) state.liveParams = await api('/config/live');
  clear(content);
  const sport = state.editor.activeSport;
  ensureDraft(sport);

  const header = h('div', { style: { display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '14px' } },
    h('div', { class: 'tabs' }, ...SPORTS.map(s => h('div', { class: 'tab ' + (s === sport ? 'active' : ''), onclick: () => { state.editor.activeSport = s; RENDER.parameters(content); } }, s))),
    h('span', { class: 'note' }, state.editor.loadedName ? h('span', {}, 'editing ', h('b', { class: 'mono' }, state.editor.loadedName)) : 'unsaved draft'),
    (() => { const n = modifiedCount(); return n ? h('span', { class: 'badge lock' }, `${n} modified`) : null; })());
  content.appendChild(header);

  content.appendChild(weightsPanel(sport, content));
  content.appendChild(h('div', { class: 'grid cols-3' }, adjustPanel(sport, content), winProbPanel(sport, content), gatesPanel(sport, content)));
  content.appendChild(footerBar(content));
};

function fieldReset(cond, onReset) {
  return cond ? h('button', { class: 'icon', title: 'reset to factory default', onclick: onReset }, '↺') : h('span', {});
}

function weightsPanel(sport, content) {
  const d = state.editor.drafts[sport], f = factoryFor(sport);
  const sum = WEIGHT_KEYS.reduce((a, k) => a + d.weights[k], 0);
  const good = Math.abs(sum - 1) < 1e-6;
  const blend = WEIGHT_KEYS.map(k => ({ key: k, label: { season: 'Season', common: 'Common opp', form: 'Form', home: 'Home', h2h: 'H2H' }[k], weight: d.weights[k] }));
  const labels = { season: 'Season scoring', common: 'Common opponents', form: 'Recent form (L10)', home: 'Home', h2h: 'Head-to-head' };
  const rows = WEIGHT_KEYS.map(k => {
    const dirty = Math.abs(d.weights[k] - f.weights[k]) > 1e-9;
    const set = (v) => { d.weights[k] = Math.max(0, Math.min(1, v)); RENDER.parameters(content); };
    return h('div', { class: 'wrow' },
      h('span', { class: 'nm' }, labels[k]),
      h('input', { type: 'range', min: 0, max: 1, step: 0.01, value: d.weights[k], oninput: (e) => set(parseFloat(e.target.value)) }),
      h('input', { type: 'number', min: 0, max: 1, step: 0.01, value: d.weights[k].toFixed(2), onchange: (e) => set(parseFloat(e.target.value) || 0) }),
      h('span', { class: 'def' }, 'def ' + f.weights[k].toFixed(2)),
      dirty ? h('span', { class: 'dirtydot', title: 'modified' }) : h('span', {}),
      fieldReset(dirty, () => set(f.weights[k])));
  });
  const sigma = h('div', { class: 'sigma' }, 'Σ ',
    h('span', { class: 'val ' + (good ? 'good' : 'bad') }, sum.toFixed(3)),
    good ? h('span', { class: 'badge over' }, 'balanced') : h('button', { class: 'sm', onclick: () => { const s = sum || 1; WEIGHT_KEYS.forEach(k => d.weights[k] = d.weights[k] / s); RENDER.parameters(content); } }, 'renormalize →'));
  const banner = good ? null : h('div', { class: 'note amber', style: { marginTop: '2px' } }, 'weights must sum to 1.000 — renormalize scales all five proportionally');
  return panel('Method weights', null, h('div', {}, sigma, banner, ...rows, h('div', { style: { marginTop: '10px' } }, blendBar(blend))));
}

function numRow(label, value, def, step, onChange, dirty, onReset, dp) {
  return h('div', { class: 'field-row' },
    h('span', { class: 'nm' }, label),
    h('input', { type: 'number', step, value: dp != null ? Number(value).toFixed(dp) : value, onchange: (e) => onChange(parseFloat(e.target.value)) }),
    h('span', { class: 'def' }, 'def ' + (dp != null ? Number(def).toFixed(dp) : def), dirty ? ' ' : ''),
    dirty ? h('span', { style: { display: 'flex', gap: '4px', alignItems: 'center' } }, h('span', { class: 'dirtydot' }), fieldReset(true, onReset)) : h('span', {}));
}

function adjustPanel(sport, content) {
  const d = state.editor.drafts[sport], f = factoryFor(sport);
  const units = { MLB: 'runs', NHL: 'goals', NBA: 'pts', WNBA: 'pts' }[sport];
  const re = () => RENDER.parameters(content);
  return panel('Score adjustments', null, h('div', {},
    numRow(`Home advantage (${units})`, d.home_adv, f.home_adv, sport === 'NBA' || sport === 'WNBA' ? 0.5 : 0.05, v => { d.home_adv = v; re(); }, Math.abs(d.home_adv - f.home_adv) > 1e-9, () => { d.home_adv = f.home_adv; re(); }, 2),
    numRow('Form base', d.form_base, f.form_base, 0.05, v => { d.form_base = v; re(); }, Math.abs(d.form_base - f.form_base) > 1e-9, () => { d.form_base = f.form_base; re(); }, 2),
    numRow('Form range', d.form_range, f.form_range, 0.05, v => { d.form_range = v; re(); }, Math.abs(d.form_range - f.form_range) > 1e-9, () => { d.form_range = f.form_range; re(); }, 2)));
}
function winProbPanel(sport, content) {
  const d = state.editor.drafts[sport], f = factoryFor(sport);
  const re = () => RENDER.parameters(content);
  return panel('Win probability', null, h('div', {},
    numRow('Steepness k', d.k, f.k, 0.05, v => { d.k = v; re(); }, Math.abs(d.k - f.k) > 1e-9, () => { d.k = f.k; re(); }, 2),
    h('div', { class: 'note', style: { marginTop: '8px' } }, 'Higher k → margins convert to win probability more steeply.')));
}
function gatesPanel(sport, content) {
  const d = state.editor.drafts[sport], f = factoryFor(sport);
  const re = () => RENDER.parameters(content);
  const dp = sport === 'MLB' || sport === 'NHL' ? 1 : 0;
  return panel('Pick gates', null, h('div', {},
    numRow('ML STRONG ≥', d.ml_gates.strong, f.ml_gates.strong, 0.01, v => { d.ml_gates.strong = v; re(); }, Math.abs(d.ml_gates.strong - f.ml_gates.strong) > 1e-9, () => { d.ml_gates.strong = f.ml_gates.strong; re(); }, 2),
    numRow('ML LEAN ≥', d.ml_gates.lean, f.ml_gates.lean, 0.01, v => { d.ml_gates.lean = v; re(); }, Math.abs(d.ml_gates.lean - f.ml_gates.lean) > 1e-9, () => { d.ml_gates.lean = f.ml_gates.lean; re(); }, 2),
    numRow('Total OVER ≥', d.totals.over, f.totals.over, dp ? 0.1 : 1, v => { d.totals.over = v; re(); }, Math.abs(d.totals.over - f.totals.over) > 1e-9, () => { d.totals.over = f.totals.over; re(); }, dp),
    numRow('Total UNDER ≤', d.totals.under, f.totals.under, dp ? 0.1 : 1, v => { d.totals.under = v; re(); }, Math.abs(d.totals.under - f.totals.under) > 1e-9, () => { d.totals.under = f.totals.under; re(); }, dp)));
}

function footerBar(content) {
  const bar = h('div', { class: 'footerbar' });
  const nameInput = h('input', { type: 'text', placeholder: 'config-name', value: state.editor.loadedName || '', style: { width: '180px' } });
  const validateAndSave = async (overwrite) => {
    const name = (overwrite ? state.editor.loadedName : nameInput.value || '').trim();
    if (!name) { toast('name required', 'err'); return; }
    // validate every sport draft via the backend validator
    for (const s of SPORTS) {
      ensureDraft(s);
      const v = await api('/config/validate', { method: 'POST', body: { sport: s, params: state.editor.drafts[s] } });
      if (!v.ok) { toast(`${s}: ${v.errors[0]}`, 'err'); return; }
    }
    try {
      await api('/configs', { method: 'POST', body: { name, params: state.editor.drafts, overwrite } });
      state.editor.loadedName = name;
      state.editor.loadedParams = JSON.parse(JSON.stringify(state.editor.drafts));
      toast(`saved ${name}`);
    } catch (e) { toast(e.message, 'err'); }
  };
  bar.appendChild(nameInput);
  bar.appendChild(h('button', { onclick: () => validateAndSave(false) }, 'Save as new config'));
  bar.appendChild(h('button', { disabled: !state.editor.loadedName, onclick: () => validateAndSave(true) }, state.editor.loadedName ? `Save over '${state.editor.loadedName}'` : 'Save over…'));
  bar.appendChild(h('button', { class: 'ghost', onclick: () => { state.editor.drafts = {}; RENDER.parameters(content); toast('reverted to loaded values'); } }, 'Discard changes'));
  bar.appendChild(h('button', { class: 'ghost', onclick: () => { SPORTS.forEach(s => state.editor.drafts[s] = JSON.parse(JSON.stringify(factoryFor(s)))); RENDER.parameters(content); toast('all sports reset to factory'); } }, 'Reset all to defaults'));
  bar.appendChild(h('span', { class: 'spacer', style: { marginLeft: 'auto' } }));
  bar.appendChild(h('button', { class: 'primary', onclick: () => go('backtest') }, 'Run backtest with these values →'));
  return bar;
}

// =============================================================== 2. BACKTEST
RENDER.backtest = async (content) => {
  const st = await api('/backtest/status');
  clear(content);
  if (!st.available) {
    content.appendChild(panel('Backtest', h('span', { class: 'badge draft' }, 'unavailable'),
      h('div', { class: 'empty' },
        h('div', { class: 'big' }, 'Backtester not yet available'),
        h('p', { style: { maxWidth: '560px', margin: '0 auto 14px' } }, st.reason),
        h('p', { class: 'note' }, 'The design keeps this screen intact — it unlocks automatically once ',
          h('span', { class: 'mono' }, 'python -m pipeline backtest'), ' lands. No fabricated numbers until then.'))));
    // still show the setup shell (disabled) so the layout is recognisable
    content.appendChild(setupShell());
    return;
  }
  content.appendChild(h('div', { class: 'empty' }, 'Backtester available — results view TODO'));
};
function setupShell() {
  const box = (l, ...k) => h('label', { class: 'fld' }, l, ...k);
  const p = panel('Setup', null, h('div', { class: 'grid cols-3' },
    box('Parameter source', h('select', { disabled: true }, h('option', {}, 'Editor draft'))),
    box('From', h('input', { type: 'date', disabled: true })),
    box('To', h('input', { type: 'date', disabled: true }))));
  return p;
};

// =============================================================== 3. CONFIGS
let cfgCompare = [];
RENDER.configs = async (content) => {
  const data = await api('/configs');
  clear(content);
  if (!data.configs.length) {
    content.appendChild(panel('Configs', null, h('div', { class: 'empty' },
      h('div', { class: 'big' }, 'No saved configs yet'),
      h('p', {}, 'Tune values on the Parameters screen and "Save as new config" to create one.'),
      h('button', { class: 'primary', style: { marginTop: '12px' }, onclick: () => go('parameters') }, 'Open the editor →'))));
    return;
  }
  const rows = data.configs.map(c => {
    const badges = [];
    (c.live_sports || []).forEach(s => badges.push(h('span', { class: 'badge live' }, h('span', { class: 'dot green' }), `LIVE · ${s}`)));
    if (c.locked) badges.push(h('span', { class: 'badge lock' }, 'LOCK'));
    if (c.archived) badges.push(h('span', { class: 'badge archived' }, 'archived'));
    if (!badges.length) badges.push(h('span', { class: 'badge draft' }, 'draft'));
    const selected = cfgCompare.includes(c.name);
    return h('tr', { class: c.archived ? 'dim' : '', style: selected ? { boxShadow: 'inset 2px 0 0 var(--blue)' } : {} },
      h('td', { class: 'mono' }, c.name),
      h('td', {}, c.note || h('span', { class: 'faint' }, '—')),
      h('td', { class: 'num' }, (c.created || '').slice(0, 10)),
      h('td', {}, h('div', { style: { display: 'flex', gap: '4px', flexWrap: 'wrap' } }, ...badges)),
      h('td', {}, h('div', { style: { display: 'flex', gap: '4px', flexWrap: 'wrap' } },
        h('button', { class: 'sm', onclick: () => loadIntoEditor(c) }, 'load'),
        h('button', { class: 'sm', onclick: () => dupConfig(c.name) }, 'duplicate'),
        h('button', { class: 'sm', onclick: () => { toggleCompare(c.name); RENDER.configs(content); } }, selected ? 'comparing' : 'compare'),
        h('button', { class: 'sm', onclick: () => openSetLive(c, data) }, 'set live'),
        h('button', { class: 'sm', onclick: () => archiveConfig(c.name, !c.archived) }, c.archived ? 'restore' : 'archive'))));
  });
  const table = h('div', { class: 'table-scroll' }, h('table', { class: 'dt' },
    h('thead', {}, h('tr', {}, ...['Name', 'Note', 'Created', 'Status', 'Actions'].map(x => h('th', {}, x)))),
    h('tbody', {}, ...rows)));
  content.appendChild(h('div', { class: 'panel pad0' }, h('div', { class: 'head' }, h('h2', {}, 'Configurations'), h('span', { class: 'spacer' }), h('button', { class: 'sm primary', onclick: () => go('parameters') }, '+ new')), table));

  if (cfgCompare.length === 2) content.appendChild(compareView(data));

  // promotion history
  if (data.promotions.length) {
    content.appendChild(panel('Promotion history', null, h('div', { class: 'table-scroll' }, h('table', { class: 'dt' },
      h('thead', {}, h('tr', {}, ...['When', 'Sport', 'Promotion', ''].map(x => h('th', {}, x)))),
      h('tbody', {}, ...data.promotions.map((p, i) => {
        const isLatestForSport = data.promotions.findIndex(x => x.sport === p.sport) === i;
        return h('tr', {},
          h('td', { class: 'num' }, (p.date || '').replace('T', ' ').replace('Z', '')),
          h('td', {}, p.sport),
          h('td', { class: 'mono' }, p.new, p.old ? h('span', { class: 'faint' }, ` ← ${p.old}`) : h('span', { class: 'faint' }, ' ← factory')),
          h('td', {}, isLatestForSport && p.old ? h('button', { class: 'sm', onclick: () => rollback(p.sport) }, 'roll back') : ''));
      }))))));
  }
};
function toggleCompare(name) { const i = cfgCompare.indexOf(name); if (i >= 0) cfgCompare.splice(i, 1); else { cfgCompare.push(name); if (cfgCompare.length > 2) cfgCompare.shift(); } }
function compareView(data) {
  const [a, b] = cfgCompare.map(n => data.configs.find(c => c.name === n));
  if (!a || !b) return h('div', {});
  const diffs = [];
  for (const s of SPORTS) {
    const pa = a.params[s], pb = b.params[s]; if (!pa || !pb) continue;
    const walk = (oa, ob, prefix) => {
      for (const k of Object.keys(oa)) {
        if (typeof oa[k] === 'object' && oa[k] !== null) walk(oa[k], ob[k] || {}, prefix + k + '.');
        else if (Math.abs((oa[k] || 0) - (ob[k] || 0)) > 1e-9) diffs.push({ sport: s, field: prefix + k, a: oa[k], b: ob[k] });
      }
    };
    walk(pa, pb, '');
  }
  return panel(`Compare: ${a.name} vs ${b.name}`, h('button', { class: 'sm', onclick: () => { cfgCompare = []; route(); } }, 'clear'),
    diffs.length ? h('div', { class: 'table-scroll' }, h('table', { class: 'dt' },
      h('thead', {}, h('tr', {}, h('th', {}, 'Sport'), h('th', {}, 'Field'), h('th', { class: 'num' }, a.name), h('th', { class: 'num' }, b.name))),
      h('tbody', {}, ...diffs.map(d => h('tr', {}, h('td', {}, d.sport), h('td', { class: 'mono' }, d.field),
        h('td', { class: 'num blue' }, fnum(d.a, 2)), h('td', { class: 'num amber' }, fnum(d.b, 2)))))))
      : h('div', { class: 'empty' }, 'these two configs are identical'));
}
async function loadIntoEditor(c) {
  state.editor.loadedName = c.name; state.editor.loadedParams = c.params;
  state.editor.drafts = JSON.parse(JSON.stringify(c.params));
  toast(`loaded ${c.name} into editor`); go('parameters');
}
async function dupConfig(name) {
  const nn = prompt('New config name:', name + '-copy'); if (!nn) return;
  try { await api(`/configs/${name}/duplicate`, { method: 'POST', body: { new_name: nn } }); toast(`duplicated → ${nn}`); route(); }
  catch (e) { toast(e.message, 'err'); }
}
async function archiveConfig(name, archived) {
  try { await api(`/configs/${name}/archive`, { method: 'POST', body: { archived } }); toast(archived ? `archived ${name}` : `restored ${name}`); route(); }
  catch (e) { toast(e.message, 'err'); }
}
async function rollback(sport) {
  try { await api('/configs/rollback', { method: 'POST', body: { sport } }); await refreshMeta(); toast(`rolled back ${sport}`); route(); }
  catch (e) { toast(e.message, 'err'); }
}
function openSetLive(c, data) {
  const root = $('#modal-root');
  const sportSel = { v: (c.live_sports && c.live_sports[0]) || 'MLB' };
  const render = () => {
    const sport = sportSel.v;
    const currentLive = data.live_map[sport];
    const cand = c.params[sport], cur = currentLive ? (data.configs.find(x => x.name === currentLive) || {}).params?.[sport] : state.defaults ? state.defaults[sport] : null;
    const diffRows = [];
    if (cur) { const walk = (oa, ob, p) => { for (const k of Object.keys(oa)) { if (typeof oa[k] === 'object') walk(oa[k], ob[k] || {}, p + k + '.'); else if (Math.abs((oa[k] || 0) - (ob[k] || 0)) > 1e-9) diffRows.push({ f: p + k, cur: ob[k], cand: oa[k] }); } }; walk(cand, cur, ''); }
    clear(root).appendChild(h('div', { class: 'modal-bg', onclick: (e) => { if (e.target.classList.contains('modal-bg')) clear(root); } },
      h('div', { class: 'modal' },
        h('div', { class: 'm-head' }, h('h3', {}, `Set '${c.name}' live`)),
        h('div', { class: 'm-body' },
          h('div', { class: 'tabs', style: { marginBottom: '12px' } }, ...SPORTS.map(s => h('div', { class: 'tab ' + (s === sport ? 'active' : ''), onclick: () => { sportSel.v = s; render(); } }, s))),
          h('p', { class: 'note', style: { marginBottom: '10px' } }, currentLive ? h('span', {}, 'Replaces ', h('b', { class: 'mono' }, currentLive), ` as the live ${sport} config.`) : `No config is live for ${sport} yet — this compares against factory defaults.`),
          diffRows.length ? h('table', { class: 'dt' }, h('thead', {}, h('tr', {}, h('th', {}, 'Field'), h('th', { class: 'num' }, 'current'), h('th', { class: 'num' }, 'candidate'))),
            h('tbody', {}, ...diffRows.map(d => h('tr', {}, h('td', { class: 'mono' }, d.f), h('td', { class: 'num faint' }, fnum(d.cur, 2)), h('td', { class: 'num amber' }, fnum(d.cand, 2)))))) : h('div', { class: 'note' }, 'identical to the currently live values.'),
          h('p', { class: 'note amber', style: { marginTop: '12px' } }, 'Promoting locks this config — it becomes immutable.')),
        h('div', { class: 'm-foot' },
          h('button', { onclick: () => clear(root) }, 'Cancel'),
          h('button', { class: 'primary', onclick: async () => { try { await api(`/configs/${c.name}/set-live`, { method: 'POST', body: { sport } }); clear(root); await refreshMeta(); toast(`${c.name} is live for ${sport}`); route(); } catch (e) { toast(e.message, 'err'); } } }, `Set live for ${sport}`)))));
  };
  render();
}

// =============================================================== 4. LIVE
RENDER.live = async (content) => {
  const [data, perf] = await Promise.all([api('/live'), api('/performance')]);
  clear(content);
  content.appendChild(h('div', { class: 'grid cols-2' }, ...SPORTS.map(s => liveCard(data[s]))));
  // grade summary strip
  const o = perf.overall;
  content.appendChild(panel('Grading summary (all graded picks)', null,
    perf.has_grades ? h('div', { class: 'grid cols-4' },
      tile('Record', `${o.wins}–${o.losses}–${o.pushes}`, `${o.graded} graded`),
      tile('ROI', fpct(o.roi), null, signClass(o.roi)),
      tile('P&L', fmoney(o.pnl), 'flat $100', signClass(o.pnl)),
      tile('Void / pending', `${o.voids} / ${o.pending}`, 'not in record'))
      : h('div', { class: 'empty' }, 'No graded picks yet — grades appear here once yesterday\'s picks settle (run the daily pipeline after finals).')));
};
function liveCard(card) {
  const off = !card.latest_date;
  const picks = card.picks || [];
  return h('div', { class: 'panel' },
    h('div', { class: 'head' }, h('h2', {}, card.sport),
      h('span', { class: 'spacer' }),
      h('span', { class: 'chip' }, h('span', { class: 'dot ' + (card.live_config ? 'green' : 'muted') }), h('span', { class: 'cfg' }, card.live_config || 'factory'))),
    h('div', { class: 'body' },
      off ? h('div', { class: 'empty' }, h('div', {}, 'Off-season — no slate'), h('p', { class: 'note' }, 'No predictions file for this sport yet.'))
        : h('div', {},
          h('div', { class: 'note', style: { marginBottom: '8px' } }, `latest slate ${card.latest_date} · ${card.counts.published} published / ${card.counts.games} games`),
          h('div', { class: 'table-scroll', style: { maxHeight: '260px' } }, h('table', { class: 'dt' },
            h('thead', {}, h('tr', {}, h('th', {}, 'Matchup'), h('th', {}, 'Pred'), h('th', { class: 'num' }, 'p'), h('th', {}, 'ML'), h('th', {}, 'Total'))),
            h('tbody', {}, ...card.predictions.slice(0, 30).map(p => h('tr', {},
              h('td', { class: 'mono' }, `${p.away.abbrev} @ ${p.home.abbrev}`),
              h('td', { class: 'num' }, p.status === 'ok' ? `${p.disp_away}-${p.disp_home}` : '—'),
              h('td', { class: 'num' }, fnum(p.win_prob, 2)),
              h('td', {}, p.published_ml ? betBadge(p.ml_lean) : h('span', { class: 'faint' }, p.ml_lean)),
              h('td', {}, p.published_total ? betBadge(p.total_lean) : h('span', { class: 'faint' }, p.total_lean))))))))));
}

// =============================================================== 5. RUNS
RENDER.runs = async (content) => {
  const { runs } = await api('/runs');
  clear(content);
  if (!runs.length) { content.appendChild(panel('Runs', null, h('div', { class: 'empty' }, 'No daily reports yet. Trigger one below or run ', h('span', { class: 'mono' }, 'python3 run_daily.py'), '.'))); }
  content.appendChild(runTrigger(content));
  for (const r of runs) content.appendChild(await runRow(r));
};
function runTrigger(content) {
  const dateI = h('input', { type: 'date', value: state.meta.today });
  const skip = h('input', { type: 'checkbox', checked: true });
  const offline = h('input', { type: 'checkbox' });
  const btn = h('button', { class: 'primary', onclick: async () => {
    btn.disabled = true;
    try {
      const job = await api('/jobs/run-daily', { method: 'POST', body: { date: dateI.value || undefined, skip_odds: skip.checked, offline: offline.checked, allow_partial: true } });
      toast('run started'); watchJob(job.id, content, 'runs');
    } catch (e) { toast(e.message, 'err'); btn.disabled = false; }
  } }, 'Run daily →');
  return panel('Trigger a run', null, h('div', { style: { display: 'flex', gap: '16px', alignItems: 'flex-end', flexWrap: 'wrap' } },
    h('label', { class: 'fld' }, 'Date', dateI),
    h('label', { class: 'fld', style: { flexDirection: 'row', alignItems: 'center', gap: '6px', textTransform: 'none' } }, skip, 'skip odds'),
    h('label', { class: 'fld', style: { flexDirection: 'row', alignItems: 'center', gap: '6px', textTransform: 'none' } }, offline, 'offline'),
    btn, h('span', { id: 'run-job-out', class: 'note' })));
}
async function runRow(r) {
  const wrap = h('div', { class: 'panel' });
  const head = h('div', { class: 'head expander' },
    h('h2', {}, r.date),
    h('span', { class: 'spacer' }),
    h('span', { class: 'note' }, `${r.predictions} predictions · ${r.picks} picks · `),
    h('span', { class: 'badge ok' }, `${r.tally.ok} ok`),
    r.tally.failed ? h('span', { class: 'badge failed' }, `${r.tally.failed} fail`) : null,
    h('span', { class: 'badge skipped' }, `${r.tally.skipped} skip`));
  const body = h('div', { class: 'body', style: { display: 'none' } });
  let loaded = false;
  head.addEventListener('click', async () => {
    const showing = body.style.display !== 'none';
    body.style.display = showing ? 'none' : 'block';
    if (!loaded && !showing) { loaded = true; clear(body).appendChild(h('div', { class: 'note' }, 'loading…')); clear(body).appendChild(await runDetail(r.date)); }
  });
  wrap.appendChild(head); wrap.appendChild(body);
  return wrap;
}
async function runDetail(date) {
  const rep = await api('/runs/' + date);
  const box = h('div', {});
  box.appendChild(h('div', { class: 'note', style: { marginBottom: '10px' } }, rep.generated));
  // stage grid per sport
  const stages = ['ingest', 'grade', 'predict', 'odds'];
  const grid = h('div', { class: 'stagegrid' }, h('div', { class: 'hd' }, ''), ...stages.map(s => h('div', { class: 'hd' }, s)));
  for (const sport of SPORTS) {
    grid.appendChild(h('div', { style: { fontWeight: 600 } }, sport));
    for (const stg of stages) {
      const cell = (rep.grid[sport] || {})[stg];
      if (!cell) { grid.appendChild(h('div', {}, h('span', { class: 'faint' }, '·'))); continue; }
      grid.appendChild(h('div', { class: 'stagecell' },
        h('span', { class: 'st ' + cell.status }, cell.status),
        h('span', { class: 'dt', title: cell.detail }, cell.rows != null ? `${cell.rows} rows` : (cell.today != null ? `${cell.today} today` : cell.detail.slice(0, 22)))));
    }
  }
  box.appendChild(grid);
  // predict output
  if (rep.sections.predict.length) {
    box.appendChild(h('h2', { style: { fontSize: '10px', letterSpacing: '1.2px', textTransform: 'uppercase', color: 'var(--muted)', margin: '16px 0 6px' } }, 'Pick sheet output'));
    for (const blk of rep.sections.predict) box.appendChild(h('pre', { class: 'trace' }, `# ${blk.sport}\n` + blk.output));
  }
  // failures
  if (rep.failures.length) {
    box.appendChild(h('h2', { style: { fontSize: '10px', letterSpacing: '1.2px', textTransform: 'uppercase', color: 'var(--red)', margin: '16px 0 6px' } }, `Failures (${rep.failures.length})`));
    for (const f of rep.failures) box.appendChild(h('details', {}, h('summary', { class: 'expander', style: { color: 'var(--red)', marginBottom: '4px' } }, f.title), h('pre', { class: 'trace' }, f.output)));
  }
  return box;
}

// =============================================================== 6. PICKS
RENDER.picks = async (content) => {
  if (!state.picks.date) {
    const dates = state.meta.pick_dates[state.picks.sport] || [];
    state.picks.date = dates[0] || state.meta.today;
  }
  clear(content);
  const dates = state.meta.pick_dates[state.picks.sport] || [];
  const sportTabs = h('div', { class: 'tabs' }, ...SPORTS.map(s => h('div', { class: 'tab ' + (s === state.picks.sport ? 'active' : ''), onclick: () => { state.picks.sport = s; state.picks.date = (state.meta.pick_dates[s] || [])[0] || null; RENDER.picks(content); } }, s)));
  const dateSel = h('select', { onchange: (e) => { state.picks.date = e.target.value; RENDER.picks(content); } },
    ...(dates.length ? dates : []).map(d => h('option', { value: d, selected: d === state.picks.date }, d)));
  const controls = h('div', { style: { display: 'flex', gap: '12px', alignItems: 'center', marginBottom: '14px' } }, sportTabs, h('span', { class: 'spacer', style: { marginLeft: 'auto' } }), h('span', { class: 'note' }, 'date'), dates.length ? dateSel : h('span', { class: 'note' }, '(no slate)'));
  content.appendChild(controls);

  if (!dates.length || !state.picks.date) { content.appendChild(panel('Predictions', null, h('div', { class: 'empty' }, `No predictions on file for ${state.picks.sport}. Run predict for a date to populate this.`))); return; }
  const data = await api(`/picks?sport=${state.picks.sport}&date=${state.picks.date}`);
  content.appendChild(h('div', { class: 'grid cols-4', style: { marginBottom: '16px' } },
    tile('Games', data.counts.games), tile('Published picks', data.counts.published),
    tile('Toss-ups (ML)', data.counts.toss_up), tile('No prediction', data.counts.no_prediction)));

  const table = h('table', { class: 'dt' },
    h('thead', {}, h('tr', {}, h('th', {}, 'Matchup'), h('th', { class: 'num' }, 'Pred'), h('th', { class: 'num' }, 'Total'), h('th', { class: 'num' }, 'Spread'), h('th', { class: 'num' }, 'Win p'), h('th', {}, 'Conf'), h('th', {}, 'ML'), h('th', {}, 'Total pick'), h('th', {}, ''))),
    h('tbody', {}));
  const tb = table.querySelector('tbody');
  data.predictions.forEach(p => {
    const noPred = p.status !== 'ok';
    const tr = h('tr', { class: 'clickable' + (noPred ? ' dim' : '') },
      h('td', {}, h('span', { class: 'mono' }, `${p.away.abbrev} @ ${p.home.abbrev}`), noPred ? h('span', { class: 'badge draft', style: { marginLeft: '6px' } }, 'no prediction') : ''),
      h('td', { class: 'num' }, noPred ? '—' : `${p.disp_away}-${p.disp_home}`),
      h('td', { class: 'num' }, fnum(p.pred_total, 1)),
      h('td', { class: 'num' }, fnum(p.pred_spread, 2)),
      h('td', { class: 'num' }, fnum(p.win_prob, 2)),
      h('td', {}, p.pred_confidence ? h('span', { class: 'badge draft' }, p.pred_confidence) : ''),
      h('td', {}, noPred ? '' : (p.published_ml ? betBadge(p.ml_lean) : h('span', { class: 'faint' }, p.ml_lean))),
      h('td', {}, noPred ? '' : (p.published_total ? betBadge(p.total_lean) : h('span', { class: 'faint' }, p.total_lean))),
      h('td', {}, noPred ? '' : h('button', { class: 'sm', onclick: (e) => { e.stopPropagation(); toggleReasoning(tr, p); } }, 'why ▾')));
    tr.addEventListener('click', () => toggleReasoning(tr, p));
    tb.appendChild(tr);
  });
  content.appendChild(h('div', { class: 'panel pad0' }, h('div', { class: 'head' }, h('h2', {}, `${state.picks.sport} · ${state.picks.date}`), h('span', { class: 'spacer' }), h('span', { class: 'note' }, 'click a row for reasoning ingredients')), h('div', { class: 'table-scroll' }, table)));
};

async function toggleReasoning(tr, p) {
  const next = tr.nextElementSibling;
  if (next && next.classList.contains('reasoning-row')) { next.remove(); return; }
  const drawer = h('tr', { class: 'reasoning-row' }, h('td', { colspan: 9, class: 'drawer', style: { padding: '0' } }, h('div', { style: { padding: '14px 16px' } }, h('span', { class: 'spin' }), ' loading reasoning…')));
  tr.after(drawer);
  try {
    const r = await api(`/reasoning?sport=${state.picks.sport}&date=${state.picks.date}&game_id=${encodeURIComponent(p.game_id)}`);
    clear(drawer.firstChild).appendChild(reasoningPanel(r));
  } catch (e) { clear(drawer.firstChild).appendChild(h('div', { style: { padding: '14px' }, class: 'note neg' }, e.message)); }
}
function reasoningPanel(r) {
  const pr = r.prediction;
  const feat = (side, label) => {
    const f = r.features[side];
    if (!f) return h('div', {}, h('div', { class: 'k' }, label), h('div', { class: 'note' }, 'no frame row'));
    return h('div', {}, h('div', { class: 'k', style: { color: 'var(--muted)', textTransform: 'uppercase', fontSize: '10px', letterSpacing: '1px', marginBottom: '6px' } }, label),
      h('div', { class: 'kv' },
        h('span', { class: 'k' }, 'Season SPG/SAPG'), h('span', { class: 'v' }, `${fnum(f.season_scoring.spg, 2)} / ${fnum(f.season_scoring.sapg, 2)} (${fint(f.season_scoring.gp)} gp)`),
        h('span', { class: 'k' }, 'Common opp for/against'), h('span', { class: 'v' }, `${fnum(f.common_opponents.wrf, 2)} / ${fnum(f.common_opponents.wra, 2)} (${fint(f.common_opponents.games)} g)`),
        h('span', { class: 'k' }, 'L10 form'), h('span', { class: 'v' }, `${fint(f.last10_form.wins)}-${fint(f.last10_form.losses)}${f.last10_form.otl ? '-' + fint(f.last10_form.otl) : ''} (${fnum(f.last10_form.l10_pct, 2)})`),
        h('span', { class: 'k' }, 'H2H scored/allowed'), h('span', { class: 'v' }, `${fnum(f.head_to_head.avg_scored, 2)} / ${fnum(f.head_to_head.avg_allowed, 2)} (${fint(f.head_to_head.games)} g)`),
        h('span', { class: 'k' }, 'Travel'), h('span', { class: 'v' }, `${fint(f.travel_km)} km`)));
  };
  const details = r.method_details_present
    ? h('pre', { class: 'trace' }, typeof r.method_details === 'string' ? r.method_details : JSON.stringify(r.method_details, null, 2))
    : h('div', { class: 'note', style: { fontStyle: 'italic' } }, 'detailed reasoning not recorded for this run — reconstructed from the active config weights and this matchup\'s frame features below.');
  return h('div', {},
    h('div', { class: 'grid cols-3', style: { marginBottom: '14px' } },
      h('div', {}, h('div', { class: 'k', style: { color: 'var(--muted)', textTransform: 'uppercase', fontSize: '10px', letterSpacing: '1px', marginBottom: '6px' } }, 'How the blend is weighted'), blendBar(r.blend),
        h('div', { class: 'kv', style: { marginTop: '10px' } }, h('span', { class: 'k' }, 'home adv'), h('span', { class: 'v' }, fnum(r.adjustments.home_adv, 2)), h('span', { class: 'k' }, 'form base/range'), h('span', { class: 'v' }, `${fnum(r.adjustments.form_base, 2)}/${fnum(r.adjustments.form_range, 2)}`), h('span', { class: 'k' }, 'k'), h('span', { class: 'v' }, fnum(r.adjustments.k, 2)))),
      feat('away', r.away_label + ' (away)'), feat('home', r.home_label + ' (home)')),
    h('div', { class: 'grid cols-2', style: { marginBottom: '10px' } },
      h('div', { class: 'kv' },
        h('span', { class: 'k' }, 'Predicted score'), h('span', { class: 'v' }, `${fnum(pr.pred_away, 2)} – ${fnum(pr.pred_home, 2)} (disp ${pr.disp_away}-${pr.disp_home})`),
        h('span', { class: 'k' }, 'Total / spread'), h('span', { class: 'v' }, `${fnum(pr.pred_total, 2)} / ${fnum(pr.pred_spread, 2)}`),
        h('span', { class: 'k' }, 'Win prob'), h('span', { class: 'v' }, `${fnum(pr.win_prob, 2)} (${pr.pred_confidence})`),
        h('span', { class: 'k' }, 'Leans'), h('span', { class: 'v' }, `ML ${pr.ml_lean} · TOTAL ${pr.total_lean}`))),
    details);
}

// =============================================================== 7. DATABASE
let dbTable = 'teams', dbOffset = 0;
RENDER.database = async (content) => {
  const status = await api('/db/status');
  clear(content);
  const counts = status.counts;
  const empty = Object.values(counts).every(v => v === 0);
  content.appendChild(panel('Database status', h('span', { class: 'note mono' }, status.target),
    h('div', {}, h('div', { class: 'grid cols-3' },
      tile('teams', counts.teams), tile('games', counts.games), tile('frames', counts.frames)),
      empty ? h('div', { class: 'note', style: { marginTop: '12px' } }, 'The mirror is empty — it\'s a rebuildable copy of the CSVs. Load it below (runs ', h('span', { class: 'mono' }, 'python3 -m pipeline.db load'), ').') : null,
      h('div', { style: { marginTop: '12px' } }, h('button', { class: 'primary sm', id: 'dbload', onclick: async (e) => { e.target.disabled = true; try { const j = await api('/jobs/db-load', { method: 'POST', body: {} }); toast('db load started'); watchJob(j.id, content, 'database'); } catch (er) { toast(er.message, 'err'); e.target.disabled = false; } } }, 'Load / refresh DB →')))));

  const tabs = h('div', { class: 'tabs' }, ...['teams', 'games', 'frames'].map(t => h('div', { class: 'tab ' + (t === dbTable ? 'active' : ''), onclick: () => { dbTable = t; dbOffset = 0; RENDER.database(content); } }, t)));
  const holder = h('div', { class: 'panel pad0' }, h('div', { class: 'head' }, h('h2', {}, 'Browse'), h('span', { class: 'spacer' }), tabs), h('div', { class: 'body', id: 'db-browse' }, h('div', { class: 'note' }, 'loading…')));
  content.appendChild(holder);
  await loadDbTable();
};
async function loadDbTable() {
  const limit = 25;
  const data = await api(`/db/table/${dbTable}?limit=${limit}&offset=${dbOffset}`);
  const target = $('#db-browse'); if (!target) return;
  if (!data.rows.length) { clear(target).appendChild(h('div', { class: 'empty' }, `no rows in ${dbTable} — load the DB first`)); return; }
  const table = h('table', { class: 'dt' }, h('thead', {}, h('tr', {}, ...data.columns.map(c => h('th', { class: 'num' }, c)))),
    h('tbody', {}, ...data.rows.map(row => h('tr', {}, ...data.columns.map(c => h('td', { class: 'num' }, String(row[c] == null ? '' : row[c]).slice(0, 60)))))));
  const pager = h('div', { style: { display: 'flex', gap: '10px', alignItems: 'center', marginTop: '10px' } },
    h('button', { class: 'sm', disabled: dbOffset === 0, onclick: () => { dbOffset = Math.max(0, dbOffset - limit); loadDbTable(); } }, '← prev'),
    h('span', { class: 'note num' }, `${dbOffset + 1}–${Math.min(dbOffset + limit, data.total)} of ${data.total}`),
    h('button', { class: 'sm', disabled: dbOffset + limit >= data.total, onclick: () => { dbOffset += limit; loadDbTable(); } }, 'next →'));
  clear(target).appendChild(h('div', { class: 'table-scroll' }, table)); target.appendChild(pager);
}

// =============================================================== 8. PERFORMANCE
RENDER.performance = async (content) => {
  const perf = await api('/performance');
  clear(content);
  if (!perf.has_grades) {
    content.appendChild(panel('Performance / ROI', null, h('div', { class: 'empty' },
      h('div', { class: 'big' }, 'No graded picks yet'),
      h('p', {}, 'ROI and P&L appear once picks are graded. Grades are written by the daily pipeline when it grades yesterday\'s picks against finals — nothing is fabricated here.'))));
    return;
  }
  const o = perf.overall;
  content.appendChild(h('div', { class: 'grid cols-5', style: { marginBottom: '16px' } },
    tile('Record', `${o.wins}–${o.losses}–${o.pushes}`, `${o.graded} graded`),
    tile('ROI', fpct(o.roi), 'flat $100', signClass(o.roi)),
    tile('Total P&L', fmoney(o.pnl), null, signClass(o.pnl)),
    tile('Void', o.voids, 'stake returned'),
    tile('Pending', o.pending, 'not settled')));
  content.appendChild(panel('Cumulative P&L (flat $100)', null,
    lineChart([{ points: perf.curve.map((c, i) => ({ x: i, cumulative: c.cumulative })), color: '#3FB77E' }])));
  const breakdown = (title, obj) => panel(title, null, h('div', { class: 'table-scroll' }, h('table', { class: 'dt' },
    h('thead', {}, h('tr', {}, h('th', {}, ''), h('th', { class: 'num' }, 'REC'), h('th', { class: 'num' }, 'ROI'), h('th', { class: 'num' }, 'P&L'))),
    h('tbody', {}, ...Object.entries(obj).map(([k, b]) => h('tr', {}, h('td', {}, k || '—'),
      h('td', { class: 'num' }, `${b.wins}-${b.losses}-${b.pushes}`),
      h('td', { class: 'num ' + signClass(b.roi) }, fpct(b.roi)),
      h('td', { class: 'num ' + signClass(b.pnl) }, fmoney(b.pnl))))))));
  content.appendChild(h('div', { class: 'grid cols-3' },
    breakdown('By sport', perf.by_sport), breakdown('By bet type', perf.by_bet_type), breakdown('By confidence', perf.by_confidence)));
};

// =============================================================== 9. WEIGHTS
RENDER.weights = async (content) => {
  const data = await api('/model-weights');
  clear(content);
  content.appendChild(h('div', { class: 'grid cols-2' }, ...SPORTS.map(s => {
    const w = data[s], p = w.params;
    return h('div', { class: 'panel' },
      h('div', { class: 'head' }, h('h2', {}, s), h('span', { class: 'spacer' }),
        h('span', { class: 'chip' }, h('span', { class: 'dot ' + (w.live_config ? 'green' : 'muted') }), h('span', { class: 'cfg' }, w.live_config || 'factory'))),
      h('div', { class: 'body' },
        blendBar(w.blend),
        h('div', { class: 'kv', style: { marginTop: '14px' } },
          h('span', { class: 'k' }, 'home advantage'), h('span', { class: 'v' }, fnum(p.home_adv, 2)),
          h('span', { class: 'k' }, 'form base / range'), h('span', { class: 'v' }, `${fnum(p.form_base, 2)} / ${fnum(p.form_range, 2)}`),
          h('span', { class: 'k' }, 'win-prob k'), h('span', { class: 'v' }, fnum(p.k, 2)),
          h('span', { class: 'k' }, 'ML gates strong / lean'), h('span', { class: 'v' }, `${fnum(p.ml_gates.strong, 2)} / ${fnum(p.ml_gates.lean, 2)}`),
          h('span', { class: 'k' }, 'totals over / under'), h('span', { class: 'v' }, `${fnum(p.totals.over, 1)} / ${fnum(p.totals.under, 1)}`))));
  })));
};

// ------------------------------------------------------------- job polling
async function watchJob(id, content, screen) {
  const out = $('#run-job-out');
  const poll = async () => {
    let job;
    try { job = await api('/jobs/' + id); } catch (e) { return; }
    const tail = (job.output || '').split('\n').slice(-1)[0] || '';
    if (out) out.textContent = `${job.status}: ${tail}`.slice(0, 80);
    if (job.status === 'running' || job.status === 'queued') { setTimeout(poll, 1200); return; }
    toast(job.status === 'succeeded' ? 'run complete' : 'run failed: ' + (tail || ''), job.status === 'succeeded' ? 'ok' : 'err');
    await refreshMeta();
    if (current === screen) route();
  };
  poll();
}

// ------------------------------------------------------------- bootstrap
async function refreshMeta() { state.meta = await api('/meta'); renderLiveChips(); state.liveParams = null; }

async function boot() {
  buildNav();
  await refreshMeta();
  window.addEventListener('hashchange', route);
  document.addEventListener('keydown', (e) => {
    if (/input|textarea|select/i.test((e.target.tagName || ''))) return;
    const s = SCREENS.find(x => x.key === e.key); if (s) go(s.id);
  });
  route();
}
boot();
