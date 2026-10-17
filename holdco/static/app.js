'use strict';
/* Holdco dashboard: plain JavaScript, no build step. */

// ---------------------------------------------------------------- utilities
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const plural = (n, one, many) => `${n} ${n === 1 ? one : (many || one + 's')}`;

async function api(method, path, body) {
  const opt = { method, headers: {} };
  if (method !== 'GET') opt.headers['X-Holdco'] = '1';
  if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  let data = {};
  try { data = await r.json(); } catch (_) { /* empty */ }
  if (!r.ok) {
    const err = new Error(data.error || `${r.status} ${r.statusText}`);
    err.status = r.status;
    err.data = data;   // the founding preflight needs the body's checks, not only the message
    throw err;
  }
  return data;
}
const GET = (p) => api('GET', p);
const fmtBytes = (n) => (n >= 1e9 ? `${(n / 1e9).toFixed(1)} GB` : n >= 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1e3))} KB`);

function rel(iso) {
  if (!iso) return '';
  const t = new Date(iso);
  const s = (Date.now() - t.getTime()) / 1000;
  if (s < 45) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  if (s < 86400 * 6) return t.toLocaleDateString(undefined, { weekday: 'short' }) + ' ' + t.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
  return t.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}
const when = (iso) => `<time data-t="${esc(iso)}" title="${esc(iso ? new Date(iso).toLocaleString() : '')}">${rel(iso)}</time>`;
setInterval(() => $$('time[data-t]').forEach((t) => { t.textContent = rel(t.dataset.t); }), 30000);

function toast(msg, bad = false) {
  const t = document.createElement('div');
  t.className = 'toast' + (bad ? ' bad' : '');
  t.textContent = msg;
  $('#toasts').appendChild(t);
  setTimeout(() => t.remove(), bad ? 6000 : 3200);
}
const fail = (e) => toast(e.message || String(e), true);

function debounce(fn, ms) {
  let id;
  return (...a) => { clearTimeout(id); id = setTimeout(() => fn(...a), ms); };
}

// Minimal, safe Markdown (escapes everything first). A link's address may not contain "(": the bare-URL pass below
// runs over the HTML the link pass made, and a "(https://" inside an href would put a second href in it.
function inline(s) {
  return esc(s)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[\s(])\*([^*\s][^*]*?)\*(?=[\s).,!?:;]|$)/g, '$1<em>$2</em>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s()"<>]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
}
function md(src) {
  const lines = String(src || '').replace(/\r/g, '').split('\n');
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (/^```/.test(line)) {
      const buf = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      i++;
      out.push(`<pre><code>${esc(buf.join('\n'))}</code></pre>`);
      continue;
    }
    const h = line.match(/^(#{1,4})\s+(.*)/);
    if (h) { const n = Math.min(h[1].length, 3); out.push(`<h${n}>${inline(h[2])}</h${n}>`); i++; continue; }
    if (/^\s*([-*_])\s*\1\s*\1[\s\-*_]*$/.test(line)) { out.push('<hr>'); i++; continue; }
    if (/^\s*\|.*\|\s*$/.test(line)) {
      const rows = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(lines[i++]);
      const cells = (r) => r.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
      const body = rows.filter((r) => !/^\s*\|[\s:|-]+\|\s*$/.test(r));
      const head = cells(body.shift() || '');
      out.push('<table><thead><tr>' + head.map((c) => `<th>${inline(c)}</th>`).join('') + '</tr></thead><tbody>' +
        body.map((r) => '<tr>' + cells(r).map((c) => `<td>${inline(c)}</td>`).join('') + '</tr>').join('') + '</tbody></table>');
      continue;
    }
    if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {
      const ordered = /^\s*\d/.test(line);
      const items = [];
      while (i < lines.length && /^\s*([-*+]|\d+[.)])\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*([-*+]|\d+[.)])\s+/, '').replace(/^\[( |x)\]\s*/i, (m, x) => (x.trim() ? '☑ ' : '☐ ')));
        i++;
      }
      const tag = ordered ? 'ol' : 'ul';
      out.push(`<${tag}>` + items.map((t) => `<li>${inline(t)}</li>`).join('') + `</${tag}>`);
      continue;
    }
    if (/^>\s?/.test(line)) {
      const buf = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) buf.push(lines[i++].replace(/^>\s?/, ''));
      out.push(`<blockquote>${inline(buf.join(' '))}</blockquote>`);
      continue;
    }
    if (!line.trim()) { i++; continue; }
    const buf = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|```|>|\s*([-*+]|\d+[.)])\s+|\s*\|)/.test(lines[i])) buf.push(lines[i++]);
    if (!buf.length) buf.push(lines[i++]);
    out.push(`<p>${buf.map(inline).join('<br>')}</p>`);
  }
  return `<div class="md">${out.join('')}</div>`;
}

// ---------------------------------------------------------------- avatars
const ROLE_COLORS = {
  ceo: '#2d45e0', product: '#4058c9', research: '#0d7f80', discovery: '#127a62', engineering: '#6246d8', qa: '#52606d',
  design: '#a8458f', marketing: '#c2357a', seo: '#8f5a1d', social: '#c8366a', lifecycle: '#b8551a', ads: '#b02a37',
  partnerships: '#94661a', sales: '#cc5a17', support: '#1b7f4e', success: '#2b8a3e', ops: '#8a6a12', legal: '#4b5563',
  analyst: '#1f6fbf',
};
const EXTRA_COLORS = ['#3a6fd0', '#b5446e', '#2a8a7f', '#8f652a', '#5653e0', '#b83b2e'];
function roleColor(role) {
  if (ROLE_COLORS[role]) return ROLE_COLORS[role];
  let h = 0;
  for (const c of role || '') h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return EXTRA_COLORS[h % EXTRA_COLORS.length];
}
function avatar(agent, size = '', ring = true) {
  if (!agent) return `<span class="sys" aria-hidden="true">•</span>`;
  const working = ring && agent.status === 'working' ? ' working' : '';
  return `<span class="av ${size}${working}" style="--c:${roleColor(agent.preset || agent.role)}" title="${esc(agent.name)}, ${esc(agent.title || agent.role)}">${esc((agent.name || '?')[0].toUpperCase())}</span>`;
}

// ---------------------------------------------------------------- modal
function modal({ title, body, foot = '', wide = false, onClose }) {
  const root = $('#modal-root');
  root.innerHTML = `<div class="overlay" data-act="overlay"><div class="modal ${wide ? 'wide' : ''}" role="dialog" aria-modal="true" aria-label="${esc(title)}">
    <div class="modal-head"><h2>${esc(title)}</h2><button class="btn ghost sm" data-act="close-modal">Close</button></div>
    <div class="modal-body">${body}</div>${foot ? `<div class="modal-foot">${foot}</div>` : ''}</div></div>`;
  S.onModalClose = onClose || null;
  const first = $('input, textarea, select', root) || $('.modal', root);
  setTimeout(() => first && first.focus(), 30);
  return $('.modal', root);
}
function closeModal() {
  $('#modal-root').innerHTML = '';
  if (S.onModalClose) { const f = S.onModalClose; S.onModalClose = null; f(); }
}
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && $('#modal-root').innerHTML) closeModal(); });

// ---------------------------------------------------------------- app state
const S = {
  st: null,            // /api/state
  agents: {},          // id -> agent
  view: null,
  feeds: new Set(),
  models: null,
  offline: false,
  connectors: null,    // /api/connectors, cached for the definitions (labels, agents_can, tools)
};

const refreshState = async () => {
  try {
    S.st = await GET('/api/state');
    S.agents = Object.fromEntries(S.st.agents.map((a) => [a.id, a]));
    renderSide();
    renderKillBanner();
    if (S.view && S.view.onState) S.view.onState();
  } catch (e) { /* server restarting */ }
};
const stateSoon = debounce(refreshState, 900);

async function loadModels(force = false) {
  if (S.models && !force) return S.models;
  try { const r = await GET('/api/llm/models'); S.models = r.ok ? r.models : []; } catch (_) { S.models = []; }
  return S.models;
}

// ---------------------------------------------------------------- sidebar
const LOGO = `<svg width="26" height="26" viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="8" fill="var(--accent)"/><path d="M9.5 8v16M22.5 8v16M9.5 16h13" stroke="var(--accent-ink)" stroke-width="3.2" stroke-linecap="round"/></svg>`;

function renderSide() {
  const st = S.st;
  const side = $('#side');
  if (!st) { side.innerHTML = `<div class="brand">${LOGO} Holdco</div>`; return; }
  const p = route();
  const at = (cond) => (cond ? ' on' : '');
  const working = st.agents.filter((a) => a.status === 'working').length;
  const cos = st.companies.filter((c) => c.status !== 'archived');
  const archived = st.companies.filter((c) => c.status === 'archived');
  const coLink = (c) => {
    const busy = st.agents.filter((a) => a.company_id === c.id && a.status === 'working').length;
    return `<a class="co-link${at(p[0] === 'c' && +p[1] === c.id)}" href="#/c/${c.id}">
      <span class="dot ${esc(c.status)}"></span><span class="name">${esc(c.name)}</span>
      ${c.stats.approvals ? `<span class="count">${c.stats.approvals}</span>` : busy ? `<span class="busy">${busy} working</span>` : ''}</a>`;
  };
  side.innerHTML = `
    <div class="brand">${LOGO} Holdco</div>
    <nav class="nav" aria-label="Main">
      <a href="#/" class="${at(!p.length)}">Portfolio</a>
      <a href="#/inbox" class="${at(p[0] === 'inbox')}">Inbox ${st.approvals ? `<span class="count">${st.approvals}</span>` : ''}</a>
      <a href="#/library" class="${at(p[0] === 'library')}">Agent library</a>
      <a href="#/context" class="${at(p[0] === 'context')}">Your context</a>
      <a href="#/doctor" class="${at(p[0] === 'doctor')}">Doctor${healthDot(st.doctor)}</a>
    </nav>
    <div class="side-label">Companies</div>
    <nav class="nav" aria-label="Companies">
      ${cos.map(coLink).join('')}
      <a href="#/new" class="${at(p[0] === 'new')}">+ Start a company</a>
    </nav>
    ${archived.length ? `<div class="side-label">Archived</div><nav class="nav">${archived.map(coLink).join('')}</nav>` : ''}
    <div class="side-foot">
      <nav class="nav"><a href="#/settings" class="${at(p[0] === 'settings')}">Settings</a>
        <a href="#/bench" class="${at(p[0] === 'bench')}" title="Holdco Bench: how well each model does the agents' work">Model scores</a></nav>
      <button class="engine ${st.scheduler_enabled && !S.offline ? 'on' : ''}" data-act="toggle-autopilot" style="border:0;text-align:left;cursor:pointer;font:inherit" title="Start or stop all agents">
        <span class="pulse"></span>
        <span>${S.offline ? 'Reconnecting to Holdco…' : st.scheduler_enabled ? `Autopilot on<br><span class="faint">${working ? plural(working, 'agent') + ' working' : 'Agents idle'}</span>` : 'Autopilot off<br><span class="faint">Agents only work when you run them</span>'}</span>
      </button>
      ${st.kill_switch ? '<button class="btn primary kill" data-act="kill-off" title="Turn the kill switch off">Resume agents</button>'
        : '<button class="btn danger kill" data-act="kill-on" title="Stop every agent and freeze outbound actions">Stop everything</button>'}
    </div>`;
}

// Doctor summary from /api/state: green when all checks pass, amber for warnings, red when something fails.
function healthDot(d) {
  if (!d || !d.checked_at) return '';
  const level = d.bad > 0 ? 'bad' : d.warn > 0 ? 'warn' : 'ok';
  const title = d.bad > 0 ? `${plural(d.bad, 'check')} failing` : d.warn > 0 ? plural(d.warn, 'warning') : 'All checks pass';
  return ` <span class="health ${level}" title="${esc(title)}" aria-label="${esc(title)}"></span>`;
}

// ---------------------------------------------------------------- kill switch
// render() adds the #kill-banner holder above every page; refreshState repaints it from /api/state.
function renderKillBanner() {
  const box = $('#kill-banner');
  if (!box) return;
  box.innerHTML = S.st && S.st.kill_switch
    ? `<div class="banner bad"><div class="grow"><b>Kill switch is on.</b> All agents are stopped and outbound actions are frozen.</div><button class="btn primary" data-act="kill-off">Resume</button></div>`
    : '';
}
function confirmKill() {
  modal({
    title: 'Stop everything?',
    body: `<p>The kill switch stops every agent at its next step and freezes outbound actions: nothing is sent, published, run or bought until you resume. Requests keep waiting in your Inbox and cannot be approved while it is on; questions can still be answered.</p>
      <p class="muted small">Autopilot is not changed. Your phone bot keeps working, so /resume from Telegram also turns it off.</p>`,
    foot: `<button class="btn" data-act="close-modal">Cancel</button><button class="btn danger" data-act="kill-confirm">Stop everything</button>`,
  });
}
async function setKill(on, btn) {
  if (btn) btn.disabled = true;
  try {
    await api('POST', '/api/kill', { on });
    closeModal();
    toast(on ? 'Kill switch on. Agents stop at their next step.' : 'Kill switch off. Agents may work again.');
    await refreshState();
  } catch (e) { fail(e); if (btn) btn.disabled = false; }
}

// ---------------------------------------------------------------- live feed
function feedItem(ev, showCompany) {
  const a = S.agents[ev.agent_id];
  const co = showCompany ? (S.st?.companies.find((c) => c.id === ev.company_id)) : null;
  let msg = esc(ev.message);
  if (ev.kind === 'file' && ev.data?.path && ev.company_id) {
    const path = String(ev.data.path);
    const href = path.startsWith('context/') ? `#/c/${ev.company_id}/context/${encodeURIComponent(path.slice(8))}`
      : `#/c/${ev.company_id}/files/${encodeURIComponent(path)}`;
    msg = msg.replace(esc(path), `<a href="${href}">${esc(path)}</a>`);
  }
  if (ev.kind === 'approval') msg += ` <a href="#/inbox">Review</a>`;
  msg = eventBadge(ev) + msg;
  const icon = a ? avatar(a, '', false) : ev.data?.by === 'owner' ? '<span class="av owner" aria-hidden="true">Y</span>' : '<span class="sys" aria-hidden="true">•</span>';
  return `<li class="k-${esc(ev.kind)}" data-id="${ev.id}">${icon}
    <div class="line"><span class="who">${esc(a ? a.name : (ev.data?.by === 'owner' ? 'You' : 'Holdco'))}</span>
    ${co ? `<span class="co"> at <a href="#/c/${co.id}">${esc(co.name)}</a></span>` : ''}
    <span class="when">${when(ev.created_at)}</span><div class="msg">${msg}</div></div></li>`;
}

function mountFeed(el, companyId = 0) {
  const feed = { el, companyId, oldest: 0 };
  el.innerHTML = '<ul class="feed"><li class="feed-empty">Loading activity…</li></ul>';
  const load = async (before = 0) => {
    const r = await GET(`/api/events?limit=60&exclude=think,result${companyId ? `&company_id=${companyId}` : ''}${before ? `&before=${before}` : ''}`);
    const ul = $('ul', el);
    if (!before) ul.innerHTML = '';
    if (!r.events.length && !before) ul.innerHTML = '<li class="feed-empty">Nothing yet. Activity shows up here as agents work.</li>';
    ul.insertAdjacentHTML('beforeend', r.events.map((e) => feedItem(e, !companyId)).join(''));
    feed.oldest = r.events.length ? r.events[r.events.length - 1].id : feed.oldest;
    $('.more', el)?.remove();
    if (r.events.length === 60) {
      ul.insertAdjacentHTML('afterend', '<div class="more" style="padding:6px 18px 14px"><button class="btn sm ghost">Show older</button></div>');
      $('.more button', el).onclick = () => load(feed.oldest).catch(fail);
    }
  };
  feed.add = (ev) => {
    if (companyId && ev.company_id !== companyId) return;
    if (ev.kind === 'think' || ev.kind === 'result') return;
    const ul = $('ul', el);
    if (!ul) return;
    $('.feed-empty', ul)?.remove();
    ul.insertAdjacentHTML('afterbegin', feedItem(ev, !companyId));
    ul.firstElementChild.classList.add('new');
    while (ul.children.length > 300) ul.lastElementChild.remove();
  };
  S.feeds.add(feed);
  load().catch(fail);
  return feed;
}

// ---------------------------------------------------------------- presets
async function loadPresets() {
  if (!S.presets) S.presets = await GET('/api/presets');
  return S.presets;
}
const presetByKey = (key) => (S.presets?.presets || []).find((p) => p.key === key);
const cadence = (h) => (h <= 6 ? `every ${h}h` : h < 48 ? 'daily' : h < 100 ? `every ${Math.round(h / 24)} days` : h <= 170 ? 'weekly' : h <= 340 ? 'every 2 weeks' : 'monthly');

function presetCard(p, opts = {}) {
  const hired = opts.hired && opts.hired.has(p.key);
  return `<article class="preset">
    <div class="preset-head">${avatar({ name: p.name, role: p.key, title: p.title }, '', false)}<div><b>${esc(p.title)}</b><div class="small faint">${esc(p.name)}</div></div></div>
    <p class="small muted">${esc(p.summary)}</p>
    <div class="preset-meta">${p.routines.length ? `<span class="badge">${plural(p.routines.length, 'recurring duty', 'recurring duties')}</span>` : ''}<span class="badge">${esc(p.reasoning)} reasoning</span></div>
    <div class="actions"><button class="btn sm" data-act="preset-info" data-key="${p.key}">Playbook</button>
      ${opts.hire ? (hired ? '<span class="small faint">On the team</span>' : `<button class="btn sm primary" data-act="hire-preset" data-key="${p.key}">Hire ${esc(p.name)}</button>`) : ''}</div>
  </article>`;
}

function presetGallery(data, opts = {}) {
  return data.categories.map((cat) => {
    const items = data.presets.filter((p) => p.category === cat);
    return items.length ? `<section class="gallery-group"><h3>${esc(cat)}</h3><div class="presets">${items.map((p) => presetCard(p, opts)).join('')}</div></section>` : '';
  }).join('');
}

function showPreset(key, opts = {}) {
  const p = presetByKey(key);
  if (!p) return;
  const list = (items, tag = 'ul') => `<${tag}>${items.map((i) => `<li>${inline(i)}</li>`).join('')}</${tag}>`;
  modal({
    title: `${p.title} (${p.name})`,
    wide: true,
    body: `<div class="md playbook"><p class="muted">${esc(p.mission)}</p>
      <h3>What done looks like</h3>${list(p.done)}
      <h3>How they work</h3>${list(p.workflow, 'ol')}
      <h3>Rules</h3>${list(p.rules)}
      <h3>Measured on</h3><p>${esc(p.kpis.join('; '))}</p>
      ${p.routines.length ? `<h3>Recurring duties</h3><ul>${p.routines.map((r) => `<li><b>${esc(r.title)}</b>, ${cadence(r.every_hours)}${r.requires ? ` (once ${r.requires === 'imap' ? 'receiving' : 'sending'} email is connected)` : ''}</li>`).join('')}</ul>` : ''}
      <h3>Tools</h3><p>${p.tools.map((t) => `<code>${esc(t)}</code>`).join(' ')}</p>
      <h3>Model settings</h3><p>Reasoning effort: ${esc(p.reasoning)} (used by gpt-oss models). Style: ${esc(p.creativity)}.</p>
      <h3>Based on</h3><ul class="small">${p.sources.map((u) => `<li><a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(u.replace(/^https?:\/\/(www\.)?/, '').slice(0, 90))}</a></li>`).join('')}</ul></div>`,
    foot: opts.hire ? `<button class="btn" data-act="close-modal">Close</button><button class="btn primary" data-act="hire-preset" data-key="${p.key}">Hire ${esc(p.name)}</button>` : '',
  });
}

function templatePicker(data, selected) {
  return `<fieldset class="templates"><legend>Starting team</legend><div class="tpl-grid">${Object.entries(data.templates).map(([k, t]) => `
    <label class="tpl"><input type="radio" name="template" value="${k}" ${k === selected ? 'checked' : ''}>
      <span class="tpl-body"><span class="tpl-title"><b>${esc(t.label)}</b>${t.recommended ? ' <span class="badge accent">Recommended</span>' : ''}</span>
      <span class="small muted">${esc(t.description)}</span>
      <span class="chips">${t.team.map((m) => avatar({ name: m.name, role: m.key, title: m.title }, 'sm', false)).join('')}</span></span></label>`).join('')}</div></fieldset>`;
}

// ---------------------------------------------------------------- router
function route() {
  return (location.hash || '#/').slice(1).split('/').filter(Boolean).map(decodeURIComponent);
}

async function render() {
  const p = route();
  S.feeds.clear();
  $('#side').classList.remove('open');
  closeModal();
  const main = $('#main');
  let view;
  if (!p.length) view = Portfolio();
  else if (p[0] === 'new') view = NewCompany();
  else if (p[0] === 'inbox') view = Inbox();
  else if (p[0] === 'settings') view = Settings(p[1]);
  else if (p[0] === 'library') view = Library();
  else if (p[0] === 'context') view = YourContext(p[1]);
  else if (p[0] === 'doctor') view = Doctor();
  else if (p[0] === 'bench') view = Bench(p[1]);
  else if (p[0] === 'c' && p[1]) view = CompanyView(+p[1], p[2] || 'overview', p[3]);
  else { location.hash = '#/'; return; }
  S.view = view;
  main.innerHTML = '';
  if (window.innerWidth <= 760) main.insertAdjacentHTML('beforeend', `<div class="mobile-bar" style="margin:-18px -16px 16px"><button class="btn sm" data-act="menu">Menu</button><b>Holdco</b></div>`);
  main.insertAdjacentHTML('beforeend', '<div id="kill-banner"></div>');
  renderKillBanner();
  const holder = document.createElement('div');
  main.appendChild(holder);
  try { await view.mount(holder); } catch (e) { holder.innerHTML = `<div class="panel empty"><h2>Something went wrong</h2><p>${esc(e.message)}</p></div>`; }
  renderSide();
}
window.addEventListener('hashchange', render);

// ---------------------------------------------------------------- portfolio
function Portfolio() {
  let el;
  const draw = () => {
    const st = S.st;
    if (!st || !el) return;
    const cos = st.companies.filter((c) => c.status !== 'archived');
    const working = st.agents.filter((a) => a.status === 'working').length;
    $('#pf-sub', el).textContent = cos.length
      ? `${plural(cos.length, 'company', 'companies')}, ${plural(working, 'agent')} working right now.`
      : 'Companies run by your local AI team.';
    const bad = (st.doctor && st.doctor.bad) || 0;
    $('#pf-doctor', el).innerHTML = bad > 0
      ? `<div class="banner bad"><div class="grow"><b>${plural(bad, 'check')} failing.</b> Something your agents depend on is not working.</div><a class="btn" href="#/doctor">Open Doctor</a></div>`
      : '';
    const list = $('#pf-list', el);
    if (!cos.length) return;
    list.innerHTML = cos.map((c) => {
      const s = c.stats;
      const status = { active: ['good', 'Running'], founding: ['accent', 'Founding'], paused: ['warn', 'Paused'] }[c.status] || ['', c.status];
      return `<a class="co-row" href="#/c/${c.id}">
        <div><div class="title"><h3>${esc(c.name)}</h3><span class="badge ${status[0]}">${status[1]}</span><span class="badge">${esc(c.stage)}</span></div>
          <p class="muted" style="margin:4px 0 8px">${esc(c.mission || c.idea.split('\n')[0])}</p>
          <div class="facts"><span><b class="num">${s.agents_working}</b> of ${s.agents_total} agents working</span><span><b class="num">${s.tasks_open}</b> open tasks</span><span><b class="num">${s.done_today}</b> done today</span></div></div>
        <div class="right">${s.approvals ? `<span class="badge warn">${plural(s.approvals, 'request')} for you</span>` : ''}<span class="faint small">${s.last_event ? 'Active ' + rel(s.last_event) : ''}</span></div></a>`;
    }).join('');
  };
  // Phase 2: the latest portfolio brief (company_id 0), built in code once a day, pinned above the companies.
  const loadBrief = async () => {
    const r = await GET('/api/briefs/latest?company_id=0').catch(() => null);
    const box = el && $('#pf-brief', el);
    if (!box) return;
    const b = r && r.daily;
    box.innerHTML = b ? `<section class="panel brief-panel"><div class="panel-head"><h2>Portfolio brief</h2><span class="small faint">${when(b.created_at)}</span></div>
      <div class="panel-body">${briefBody(b)}</div></section>` : '';
  };
  const loadBriefSoon = debounce(() => loadBrief().catch(() => {}), 800);
  return {
    async mount(holder) {
      el = holder;
      const hasCos = S.st && S.st.companies.some((c) => c.status !== 'archived');
      el.innerHTML = `<div class="page-head"><div><h1>Portfolio</h1><p class="sub" id="pf-sub"></p></div>
        <div class="actions"><a class="btn primary" href="#/new">Start a company</a></div></div>
        <div id="pf-doctor"></div><div id="pf-brief"></div>
        ${hasCos ? `<div class="cols"><section class="panel" id="pf-list"></section>
          <section class="panel"><div class="panel-head"><h2>Live</h2></div><div id="pf-feed"></div></section></div>`
        : `<section class="panel empty"><h2>Start your first company</h2>
          <p>Describe an idea, or let your agents suggest a few. A CEO agent researches the market, writes a charter and hands work to six specialists: research, engineering, marketing, sales, support and operations. They keep working around the clock on your GPU, and anything that leaves your computer (emails, posts, deploys, purchases) waits in your inbox for a yes.</p>
          <div id="model-check" class="banner" style="margin:18px 0"><span class="spinner"></span> Checking your model server…</div>
          <a class="btn primary" href="#/new">Start a company</a></section>`}`;
      if (hasCos) mountFeed($('#pf-feed', el), 0);
      else checkModel($('#model-check', el));
      draw();
      if (hasCos) await loadBrief();
    },
    onState: draw,
    live(ev) { if (ev.type === 'briefs' || (ev.type === 'event' && ev.kind === 'brief')) loadBriefSoon(); },
  };
}

async function checkModel(box) {
  const [m, st] = await Promise.all([GET('/api/llm/models').catch(() => ({ ok: false, error: 'No response' })), GET('/api/settings')]);
  const model = st.settings.default_model;
  if (!m.ok) {
    box.className = 'banner bad';
    box.innerHTML = `<div class="grow"><b>Your model server isn't reachable.</b><br><span class="small">${esc(m.error)}</span>
      <ol class="steps small"><li>Install Ollama from <a href="https://ollama.com/download" target="_blank" rel="noopener">ollama.com/download</a> and start it.</li>
      <li>In a terminal run <code>ollama pull ${esc(model)}</code></li><li>Come back here; this check reruns when you reload.</li></ol></div>
      <a class="btn" href="#/settings">Model settings</a>`;
  } else if (!m.models.includes(model)) {
    box.className = 'banner warn';
    box.innerHTML = `<div class="grow"><b>Connected, but ${esc(model)} isn't installed.</b><br><span class="small">Run <code>ollama pull ${esc(model)}</code>, or pick one of your ${m.models.length} installed models in Settings.</span></div><a class="btn" href="#/settings">Model settings</a>`;
  } else {
    box.className = 'banner';
    box.innerHTML = `<div class="grow"><b>Ready.</b> Connected to your model server; ${esc(model)} will run your agents.</div>`;
  }
}

// ---------------------------------------------------------------- new company
function NewCompany() {
  let el, mode = 'idea', about = '', presetsData = null, lastBody = null;
  const chosenTemplate = () => (el && $('input[name=template]:checked', el)?.value) || presetsData?.default_template || 'launch';
  // POST /api/companies answers 409 with the failing Doctor rows when a dependency is down; "Found anyway" forces it.
  const showPreflight = (err) => {
    const box = $('#preflight', el);
    if (!box) return;
    const checks = (err.data && err.data.checks) || [];
    const fix = (f) => (!f ? '' : f.kind === 'link' ? ` <a class="small" href="${esc(f.href || '#/doctor')}">${esc(f.label)}</a>` : ` <span class="small mono muted">${esc(f.label)}</span>`);
    box.innerHTML = `<div style="padding:18px 22px 0"><div class="banner bad" style="margin:0;align-items:flex-start"><div class="grow"><b>Holdco found a problem that would stop founding.</b>
      <ul class="rows preflight">${checks.map((r) => { const s = CHECK_STATUS[r.status] || ['', esc(r.status)]; return `<li><span class="badge ${s[0]}">${s[1]}</span><div class="grow"><b>${esc(r.label)}</b> <span class="small muted">${esc(r.detail)}</span>${fix(r.fix)}</div></li>`; }).join('')}</ul></div>
      <div class="actions"><a class="btn" href="#/doctor">Open Doctor</a><button class="btn danger" type="button" data-act="found-anyway">Found anyway</button></div></div></div>`;
    box.scrollIntoView({ block: 'nearest' });
  };
  const found = async (body) => {
    lastBody = body;
    const r = await api('POST', '/api/companies', body);
    await refreshState();
    location.hash = `#/c/${r.company.id}`;
  };
  const ideaForm = () => `
    <form class="form-stack panel-body" data-form="found" style="padding:20px 22px">
      <label class="field">The idea<span class="hint">What you'd sell, to whom. A sentence or a page, both work.</span>
        <textarea name="idea" rows="5" required minlength="8" placeholder="A subscription that sends small cafés a weekly, AI-written social media plan with ready-to-post images."></textarea></label>
      <div class="form-grid">
        <label class="field">Company name<span class="hint">Optional. The CEO will pick one if you leave it blank.</span><input type="text" name="name" maxlength="80"></label>
      </div>
      <label class="field">About you<span class="hint">Skills, budget, hours per week, anything off-limits. Agents plan around this.</span>
        <textarea name="context" rows="3" placeholder="I'm a designer, I can spend $200 to start and about 5 hours a week. No cold calling.">${esc(about)}</textarea></label>
      ${presetsData ? templatePicker(presetsData, presetsData.default_template) : ''}
      <div><button class="btn primary" type="submit">Found this company</button></div>
    </form>`;
  const findForm = () => `
    <form class="form-stack panel-body" data-form="ideas" style="padding:20px 22px">
      <div class="form-grid">
        <label class="field">What are you into?<textarea name="interests" rows="2" placeholder="Dogs, coffee, indie games"></textarea></label>
        <label class="field">Skills and background<textarea name="skills" rows="2" placeholder="Marketing, some Python"></textarea></label>
      </div>
      <div class="form-grid">
        <label class="field">Starting budget<input type="text" name="budget" placeholder="$300"></label>
        <label class="field">Hours a week you can give<input type="text" name="hours" placeholder="5"></label>
      </div>
      ${presetsData ? templatePicker(presetsData, presetsData.default_template) : ''}
      <div class="actions"><button class="btn primary" type="submit">Suggest three ideas</button><span class="faint small" id="ideas-wait"></span></div>
      <div id="ideas"></div>
    </form>`;
  const draw = () => {
    el.innerHTML = `<div class="page-head"><div><h1>Start a company</h1><p class="sub">Your CEO agent takes it from here: market research, a charter, a team and a first week of work.</p></div></div>
      <section class="panel"><div class="choice" role="tablist">
        <button role="tab" aria-selected="${mode === 'idea'}" class="${mode === 'idea' ? 'on' : ''}" data-act="mode" data-mode="idea"><b>I have an idea</b>Describe it and the team starts building.</button>
        <button role="tab" aria-selected="${mode === 'find'}" class="${mode === 'find' ? 'on' : ''}" data-act="mode" data-mode="find"><b>Suggest ideas for me</b>Tell the AI about you and pick from three.</button>
      </div><div id="preflight"></div>${mode === 'idea' ? ideaForm() : findForm()}</section>`;
  };
  return {
    async mount(holder) { el = holder; presetsData = await loadPresets().catch(() => null); draw(); },
    async act(name, t) {
      if (name === 'mode') { mode = t.dataset.mode; draw(); }
      if (name === 'found-anyway' && lastBody) {
        t.disabled = true;
        t.innerHTML = '<span class="spinner"></span> Founding…';
        try { await found({ ...lastBody, force: true }); } catch (e) { fail(e); t.disabled = false; t.textContent = 'Found anyway'; }
      }
    },
    async submit(name, form) {
      const f = Object.fromEntries(new FormData(form));
      const btn = $('button[type=submit]', form);
      if (name === 'found' || name === 'found-idea') {
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner"></span> Founding…';
        try {
          await found({ idea: f.idea, name: f.name || '', context: f.context || '', template: chosenTemplate() });
        } catch (e) {
          if (e.data && e.data.checks) showPreflight(e); else fail(e);
          btn.disabled = false; btn.textContent = 'Found this company';
        }
      }
      if (name === 'ideas') {
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner"></span> Thinking…';
        $('#ideas-wait', el).textContent = 'Local models can take a minute.';
        about = [f.skills && `Skills: ${f.skills}`, f.budget && `Budget: ${f.budget}`, f.hours && `Hours per week: ${f.hours}`, f.interests && `Interests: ${f.interests}`].filter(Boolean).join('. ');
        try {
          const r = await api('POST', '/api/ideas', f);
          $('#ideas', el).innerHTML = `<div class="ideas">${r.ideas.map((i, n) => `
            <article class="idea"><h3>${esc(i.name)}</h3><p>${esc(i.one_liner)}</p>
              <dl><dt>Customer</dt><dd>${esc(i.customer)}</dd><dt>Offer</dt><dd>${esc(i.offer)}</dd>
              <dt>Why it fits you</dt><dd>${esc(i.why_it_fits)}</dd><dt>First week</dt><dd>${esc(i.first_week)}</dd></dl>
              <button class="btn primary" type="button" data-act="pick" data-n="${n}">Found ${esc(i.name)}</button></article>`).join('')}</div>`;
          this.ideas = r.ideas;
        } catch (e) { fail(e); }
        btn.disabled = false;
        btn.textContent = 'Suggest three more';
        $('#ideas-wait', el).textContent = '';
      }
    },
    async actPick(t) {
      const i = this.ideas[+t.dataset.n];
      t.disabled = true;
      t.innerHTML = '<span class="spinner"></span> Founding…';
      try {
        const idea = `${i.name}: ${i.one_liner}\nCustomer: ${i.customer}\nOffer: ${i.offer}\nFirst week: ${i.first_week}`;
        await found({ idea, name: i.name, context: about, template: chosenTemplate() });
      } catch (e) {
        if (e.data && e.data.checks) showPreflight(e); else fail(e);
        t.disabled = false; t.textContent = `Found ${i.name}`;
      }
    },
  };
}

// ---------------------------------------------------------------- phase 1: risk tags and connected services
// Every tool and every request carries a risk tag (docs/phases/phase-1-connect.md, item 4): read and write stay
// inside Holdco, publish and contact reach other people, spend and irreversible always wait for you.
const TAG_INFO = {
  read: ['accent', 'Only reads; runs without asking'],
  write: ['accent', 'Changes data'],
  publish: ['warn', 'Publishes something other people can see'],
  contact: ['warn', 'Messages or acts toward other people'],
  unclassified: ['warn', 'Holdco has no rule for this tool, so it asks you first'],
  spend: ['bad', 'Moves money; always asks you'],
  irreversible: ['bad', 'Cannot be undone; always asks you'],
};
function tagBadge(tag) {
  if (!tag) return '';
  const [cls, title] = TAG_INFO[tag] || ['warn', 'Unknown risk tag; Holdco asks you first'];
  return `<span class="badge ${cls}" title="${esc(title)}">${esc(tag)}</span>`;
}
const KIND_LABEL = { api: 'API', 'mcp-stdio': 'MCP on this PC', 'mcp-http': 'Hosted MCP', builtin: 'Built in' };
const isOn = (v) => v === true || /^(1|true|yes|on)$/i.test(String(v ?? '').trim());
const fmtValue = (v) => {
  const n = Number(v);
  return v !== null && v !== '' && Number.isFinite(n) ? n.toLocaleString() : String(v ?? '');
};
const sentence = (s) => { const t = String(s ?? '').trim(); return !t || /[.!?:]$/.test(t) ? t : `${t}.`; };
const andList = (xs) => (xs.length < 2 ? xs.join('') : `${xs.slice(0, -1).join(', ')} and ${xs[xs.length - 1]}`);

// GET /api/connectors, cached: the Inbox and the Overview only need the definitions (labels, agents_can, tools).
async function loadConnectors(force = false) {
  if (!S.connectors || force) S.connectors = await GET('/api/connectors');
  return S.connectors;
}
const connectorDef = (key) => (S.connectors && S.connectors.definitions && S.connectors.definitions[key]) || null;
function connectorLabel(key) {
  const d = connectorDef(key);
  if (d && d.label) return d.label;
  const k = String(key || '');
  return k ? k[0].toUpperCase() + k.slice(1) : 'a connected service';
}

// Secret inputs (data-secret) show the mask when a value is stored, and sending the mask back keeps it. An input
// that was empty and is still empty is not sent, so a key saved elsewhere meanwhile (a connector card) survives.
function dropUntouchedSecrets(form, values) {
  $$('input[data-secret]', form).forEach((inp) => {
    if (inp.name && inp.name in values && !inp.value && !inp.defaultValue) delete values[inp.name];
  });
}
// After a save the secret inputs take the stored state (the mask, or empty), so clearing one later deletes it.
// untouchedOnly leaves alone an input the owner is editing.
function syncSecrets(form, values, untouchedOnly = false) {
  if (!form || !values) return;
  $$('input[data-secret]', form).forEach((inp) => {
    if (!(inp.name in values) || (untouchedOnly && inp.value !== inp.defaultValue)) return;
    inp.defaultValue = values[inp.name] || '';
    inp.value = values[inp.name] || '';
  });
}

// ---------------------------------------------------------------- phase 2: briefs, decisions, task contracts
// docs/phases/phase-2-organize.md. CHECK_TOOLS is kept in step with contracts.CHECK_TOOLS, EXEMPT_CREATORS with
// rhythms.EXEMPT_CREATORS (tasks the weekly review never cancels for serving no goal).
const CHECK_TOOLS = ['browser_check', 'audit_page', 'review_text', 'check_contrast', 'fetch_url'];
const EXEMPT_CREATORS = ['owner', 'routine', 'inbox'];
const OPEN_STATUSES = ['todo', 'in_progress', 'blocked', 'waiting'];
const capFirst = (s) => { const t = String(s ?? ''); return t ? t[0].toUpperCase() + t.slice(1) : t; };
const shortDate = (iso) => { const t = new Date(iso); return Number.isNaN(t.getTime()) ? String(iso || '') : t.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }); };
const fmtHours = (n, digits = 1) => String(Number((Number(n) || 0).toFixed(digits)));

// A decision's deadline: "auto in 31 h" when it is reversible (the recommended option is taken then), "waits for
// you" when it is not (it never resolves on its own).
function decisionDeadline(p) {
  if (p.reversible === false) return { cls: 'warn', text: 'waits for you', title: 'It cannot be undone, so it never resolves on its own.' };
  const t = Date.parse(p.deadline_at || '');
  if (!Number.isFinite(t)) return { cls: '', text: 'no deadline', title: '' };
  const h = (t - Date.now()) / 3600000;
  const text = h <= 0 ? 'auto any minute' : h < 1 ? `auto in ${Math.max(1, Math.round(h * 60))} min` : h < 72 ? `auto in ${Math.round(h)} h` : `auto in ${Math.round(h / 24)} days`;
  return { cls: 'accent', text, title: `If you have not answered by ${new Date(t).toLocaleString()}, the recommended option is taken.` };
}

// Briefs are Markdown the model wrote (the portfolio brief is built in code): md() escapes everything first.
const BRIEF_LABEL = { daily: 'Daily brief', weekly: 'Weekly memo', monthly: 'Monthly check' };
const briefBody = (b) => `<div class="brief">${md(b.body)}</div>`;
function showBrief(b, title) {
  modal({ title: title || BRIEF_LABEL[b.kind] || 'Brief', wide: true, body: `<p class="faint small" style="margin-top:0">Written ${when(b.created_at)}</p>${briefBody(b)}` });
}

// ---------------------------------------------------------------- phase 3: bench, trust, verification, sandbox
// docs/phases/phase-3-verify.md. Scores are fractions (0.82); the owner reads percentages.
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const isNum = (v) => v !== null && v !== undefined && v !== '' && Number.isFinite(Number(v));
const fmtShare = (v) => (isNum(v) ? `${Math.round(Number(v) * 100)}%` : 'n/a');
const fmtCount = (v) => (Number(v) || 0).toLocaleString();

// "Pass 27 of 33 (82%), tool calls valid 96%, 41 tokens/s, 6.2 steps per scenario"; a score the run could not
// measure (no agent scenario among those run, no timing) is left out.
function benchScores(r) {
  const parts = [`Pass ${fmtCount(r.passed)} of ${fmtCount(r.scenarios)} (${fmtShare(r.pass_rate)})`];
  if (isNum(r.tool_validity)) parts.push(`tool calls valid ${fmtShare(r.tool_validity)}`);
  if (isNum(r.tokens_per_second)) parts.push(`${Math.round(Number(r.tokens_per_second)).toLocaleString()} tokens/s`);
  if (isNum(r.steps_avg)) parts.push(`${Number(r.steps_avg).toFixed(1)} steps per scenario`);
  return parts.join(', ');
}
// bench_runs rows start with notes "running" and no finished_at; one that never finished (Holdco stopped mid-bench)
// keeps them, so "running" is only believed while /api/bench/status says this run is the one going.
const benchRunning = (r, st) => !!st && !!st.running && st.run_id === r.id;
const benchUnfinished = (r, st) => !r.finished_at && !benchRunning(r, st);
const benchProgress = (st) => (st.scenario ? `Running scenario ${Math.min((Number(st.done) || 0) + 1, Number(st.total) || 1)} of ${fmtCount(st.total)}: ${st.scenario}`
  : st.total && st.done >= st.total ? 'Scoring the results…' : 'Starting…');
const benchNotes = (r) => String(r.notes || '').trim().replace(/^running$/, '');

// POST /api/bench/run. Only one bench runs at a time: on a 409 the page follows the one already going.
async function benchStart(model, only) {
  const body = { model };
  if (Array.isArray(only) && only.length) body.only = only;
  try {
    return (await api('POST', '/api/bench/run', body)).run_id;
  } catch (e) {
    if (e.status !== 409) throw e;
    const st = await GET('/api/bench/status');
    if (!st.running || !st.run_id) throw e;
    toast(`A test of ${st.model || 'another model'} is already running; showing its progress.`);
    return st.run_id;
  }
}
// Polls /api/bench/status while run runId goes, calling onTick(status), then returns its bench_runs row; null when
// alive() turns false (the owner left the page).
async function benchFollow(runId, alive, onTick) {
  for (;;) {
    let st = null;
    try { st = await GET('/api/bench/status'); } catch (_) { /* Holdco restarting: keep waiting */ }
    if (!alive()) return null;
    if (st && (!st.running || st.run_id !== runId)) {
      const row = await GET(`/api/bench/results/${runId}`);
      if (st.error && st.run_id === runId) row.error = st.error;
      return row;
    }
    if (st) onTick(st);
    await sleep(1500);
  }
}

// The trust ladder (trust.py): the action a request counts toward. A connector call counts by its risk tag.
const LEVEL_LABEL = (cap) => [[0, 'Ask me'], [1, `On its own, up to ${cap} a day`], [2, 'On its own']];
function ladderAction(a) {
  if (a.action !== 'connector_call') return a.action;
  const tag = String((a.payload || {}).tag || a.tag || '');
  return ['write', 'publish', 'contact'].includes(tag) ? `connector_${tag}` : '';
}
const streakText = (n) => `${plural(n, 'approval')} in a row without edits`;

// Why the owner is asked (actions.gate stores it in the payload): outside content read in the session (_taint lists
// the sources), a legal commitment, a first email to a person, or a daily cap that ran out.
const TAINT_PREFIX = 'asked because this session read outside content';
function whyAsked(p) {
  const why = String(p._why_asked || '').trim();
  const taint = Array.isArray(p._taint) ? p._taint.map((x) => String(x).trim()).filter(Boolean) : [];
  if (!why && !taint.length) return '';
  const tainted = taint.length && (!why || why.toLowerCase().startsWith(TAINT_PREFIX));
  const text = tainted ? `Asked because this session read outside content: ${taint.join(', ')}.` : sentence(capFirst(why));
  const title = tainted ? 'An agent read a web page or an email in this session, and text there could be steering it. Outward actions wait for you, whatever the trust level.' : '';
  return `<p class="why small"${title ? ` title="${esc(title)}"` : ''}><span class="badge warn">Why you are asked</span> ${esc(text)}</p>`;
}

// Where run_code runs, from the Doctor's sandbox row ("Code sandbox: docker. ..." when one was found).
function sandboxOf(checks) {
  const r = (Array.isArray(checks) ? checks : []).find((c) => c && c.key === 'sandbox');
  if (!r) return { state: 'unknown', runtime: '' };
  if (r.status !== 'ok') return { state: 'off', runtime: '' };
  const m = /Code sandbox:\s*([\w.-]+?)\.?(\s|$)/i.exec(String(r.detail || ''));
  return { state: 'on', runtime: m ? capFirst(m[1]) : '' };
}

// The verification gate's results (tasks.verification: a JSON list of {kind, passed, detail, mode}).
function gateResults(t) {
  let v = t ? t.verification : null;
  if (typeof v === 'string') { try { v = v.trim() ? JSON.parse(v) : []; } catch (_) { v = []; } }
  return Array.isArray(v) ? v.filter((r) => r && typeof r === 'object') : [];
}
const gateKind = (k) => (/_/.test(String(k || '')) ? `<code>${esc(k)}</code>` : esc(capFirst(k || 'check')));
function gateBadge(t) {
  const rs = gateResults(t);
  if (!rs.length) return '';
  const bad = rs.find((r) => !r.passed);
  if (!bad) return `<span class="badge good gate" title="${esc(`Verified: ${rs.map((r) => r.kind).join(', ')} passed`)}">Checks passed</span>`;
  return `<span class="badge bad gate" title="${esc(`${capFirst(bad.kind || 'check')} failed: ${bad.detail || ''}`)}">Check failed</span>`;
}
function gateList(t) {
  const rs = gateResults(t);
  if (!rs.length) return '';
  return `<h3 style="margin:16px 0 4px">Verification</h3><p class="small faint" style="margin:0">What Holdco checked before the task could be marked done.</p>
    <ul class="rows gate-rows">${rs.map((r) => {
    const unchecked = r.mode === 'not checked';
    const badge = unchecked ? '<span class="badge">NOT CHECKED</span>' : r.passed ? '<span class="badge good">PASS</span>' : '<span class="badge bad">FAIL</span>';
    const mode = r.mode === 'static' ? ' <span class="faint small" title="Playwright is not set up, so Holdco read the page instead of opening it in a browser">(static check)</span>'
      : r.mode && !unchecked ? ` <span class="faint small">(${esc(r.mode)})</span>` : '';
    return `<li>${badge}<div class="grow"><b>${gateKind(r.kind)}</b>${mode}${r.detail ? `<div class="small muted">${esc(r.detail)}</div>` : ''}</div></li>`;
  }).join('')}</ul>`;
}

// ---------------------------------------------------------------- inbox
const ACTION_VERB = {
  send_email: 'wants to send an email', social_post: 'wants to publish a post', deploy_site: 'wants to publish the website',
  http_post: 'wants to send data to an outside service', run_code: 'wants to run code on this PC',
  spend_money: 'wants you to buy something', question: 'has a question', connector_call: 'wants to use a connected service',
  decision: 'needs you to decide',
};

const ACTION_LABEL = {
  send_email: 'Send email', social_post: 'Social post', deploy_site: 'Deploy website', http_post: 'Outside request',
  run_code: 'Run code', spend_money: 'Purchase', question: 'Question', connector_call: 'Connected service', decision: 'Decision',
};

const platformName = (p) => ({ x: 'X', twitter: 'X', linkedin: 'LinkedIn', reddit: 'Reddit', instagram: 'Instagram', facebook: 'Facebook', threads: 'Threads', bluesky: 'Bluesky', tiktok: 'TikTok', youtube: 'YouTube' }[(p || '').toLowerCase()] || p || 'social media');

function requestFields(a) {
  const p = a.payload || {};
  const ro = (label, v) => `<label class="field">${label}<div class="box">${esc(v)}</div></label>`;
  switch (a.action) {
    case 'send_email':
      return `<div class="form-grid"><label class="field">To<input type="email" data-k="to" value="${esc(p.to)}"></label>
        <label class="field">Subject<input type="text" data-k="subject" value="${esc(p.subject)}"></label></div>
        <label class="field">Message<textarea data-k="body" rows="8">${esc(p.body)}</textarea></label>`;
    case 'social_post':
      return `<label class="field">Post for ${esc(platformName(p.platform))}<textarea data-k="content" rows="4">${esc(p.content)}</textarea></label>
        ${p.link ? ro('Link', p.link) : ''}`;
    case 'deploy_site':
      return `<p class="muted">Publishes everything in the company's <code>site/</code> folder to Netlify. <a href="/preview/${esc(a.company_slug || '')}/" target="_blank" rel="noopener" data-preview="${a.company_id}">Preview the site first</a>.</p>`;
    case 'http_post':
      return `${ro('Request', `${p.method || 'POST'} ${p.url}`)}<label class="field">Body<pre class="box">${esc(JSON.stringify(p.body, null, 2))}</pre></label>`;
    case 'run_code': {
      // Phase 3: in a Docker or Podman container when the Doctor found one; "Allow network" sets payload.network.
      const sb = S.sandbox || { state: 'unknown' };
      const where = sb.state === 'on' ? `<span class="badge good" title="Only the company folder, no internet unless you allow it, 1 GB of memory and 1 CPU">runs in a sandbox (${esc(sb.runtime || 'a container')})</span>`
        : sb.state === 'off' ? '<span class="badge warn" title="Install Docker Desktop or Podman to isolate agents\' code">runs unsandboxed on this PC</span> <a class="small" href="#/doctor">How to fix</a>'
          : '<span class="badge" title="The Doctor has not checked for Docker or Podman yet">sandbox not checked yet</span> <a class="small" href="#/doctor">Open Doctor</a>';
      const net = sb.state === 'off' ? '<span class="small faint">Without a sandbox the code can also reach the internet.</span>'
        : `<label class="check small"><input type="checkbox" data-k="network"${isOn(p.network) ? ' checked' : ''}><span>Allow network <span class="faint">(the code may reach the internet; off keeps it offline)</span></span></label>`;
      return `<label class="field">${esc(p.language === 'python' ? 'Python script' : 'Shell command')} (runs in the company folder, ${esc(p.timeout || 60)}s limit)<textarea class="code" data-k="code" rows="${Math.min(16, Math.max(3, (p.code || '').split('\n').length + 1))}">${esc(p.code)}</textarea></label>
        <div class="sandbox-row">${where}${net}</div>`;
    }
    case 'spend_money':
      return `<div class="form-grid">${ro('Item', p.item)}${ro('Cost', `$${p.amount_usd}`)}${p.vendor ? ro('Vendor', p.vendor) : ''}</div>
        ${p.url ? `<p><a href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">${esc(p.url)}</a></p>` : ''}${ro('Why', p.reason)}
        <p class="faint small">Holdco never spends money. Approving tells the agent you'll make the purchase yourself.</p>`;
    case 'question': {
      // Watchdog escalations carry options (one button each, the recommended one first in line), context and a pattern.
      const opts = Array.isArray(p.options) ? p.options.map((o) => String(o)).filter((o) => o.trim()) : [];
      const rec = String(p.recommended || '');
      return `${p.pattern ? '<div><span class="badge accent">Holdco watchdog</span></div>' : ''}
        <div class="box md">${md(p.question)}</div>
        ${p.context ? `<p class="muted small" style="margin:0;white-space:pre-wrap">${esc(p.context)}</p>` : ''}
        ${opts.length ? `<div class="opts">${opts.map((o) => `<button class="btn ${o === rec ? 'primary' : ''}" type="button" data-act="decide" data-d="answer" data-answer="${esc(o)}">${o === rec ? '✓ ' : ''}${esc(o)}${o === rec ? ' (recommended)' : ''}</button>`).join('')}</div>` : ''}
        <label class="field">${opts.length ? 'Or answer in your own words' : 'Your answer'}<textarea data-k="answer" rows="3"></textarea></label>`;
    }
    case 'connector_call': {
      // A tool of a connected service (Phase 1). The arguments are shown, not edited: approve or reject as asked.
      const def = connectorDef(p.connector);
      const label = connectorLabel(p.connector);
      const tool = String(p.tool || '');
      const spec = def && Array.isArray(def.tools) ? def.tools.find((x) => x.tool === tool || x.name === tool) : null;
      const can = def && def.setup && Array.isArray(def.setup.agents_can) ? def.setup.agents_can : [];
      return `<div class="form-grid">${ro('Service', label)}
          <label class="field">Tool<div class="box"><code>${esc(tool || '?')}</code> ${tagBadge(p.tag || a.tag)}</div></label></div>
        ${spec && spec.description ? `<p class="muted small" style="margin:0">${esc(spec.description)}</p>` : ''}
        <label class="field">Arguments<pre class="box">${esc(JSON.stringify(p.args ?? {}, null, 2))}</pre></label>
        ${can.length ? `<p class="small muted" style="margin:0"><b>What agents can do with ${esc(label)}:</b> ${esc(can.join('; '))}</p>` : ''}`;
    }
    default:
      return `<pre class="box">${esc(JSON.stringify(p, null, 2))}</pre>`;
  }
}

function requestCard(a) {
  const agent = S.agents[a.agent_id] || { name: a.agent_name || 'An agent', role: a.agent_role || '' };
  const q = a.action === 'question';
  const approveLabel = { spend_money: "Approve, I'll buy it", send_email: 'Approve and send', social_post: 'Approve and post', deploy_site: 'Approve and deploy', run_code: 'Approve and run', connector_call: 'Approve and run' }[a.action] || 'Approve';
  const verb = a.action === 'connector_call' ? `wants to use ${esc(connectorLabel((a.payload || {}).connector))}` : ACTION_VERB[a.action] || esc(a.action);
  // Phase 3: the trust ladder's streak for this action (filled in by the Inbox from GET /trust), and why it asks.
  const ladder = q ? '' : ladderAction(a);
  return `<article class="req" data-req="${a.id}">
    <div class="req-head">${avatar(agent)}<div class="what"><b>${esc(agent.name)}</b> <span class="muted">at <a href="#/c/${a.company_id}">${esc(a.company_name)}</a> ${verb}</span>
      <div class="faint small req-meta"><span>Request #${esc(a.id)}</span>${tagBadge(a.tag)}<span>${when(a.created_at)}</span>${ladder ? `<span class="streak" data-streak="${esc(a.company_id)}:${esc(ladder)}"></span>` : ''}</div></div></div>
    <div class="fields">${q ? '' : whyAsked(a.payload || {})}${requestFields(a)}</div>
    <div class="foot">${q ? `<button class="btn primary" data-act="decide" data-d="answer">Send answer</button>`
      : `<input type="text" data-k="feedback" placeholder="Note for the agent (optional)">
         <button class="btn" data-act="decide" data-d="reject">Reject</button>
         <button class="btn good" data-act="decide" data-d="approve">${approveLabel}</button>`}</div>
  </article>`;
}

// Phase 2: "which of these should we do?" One button per option (the recommended one primary), the context folded,
// and when it resolves on its own. Answered through the same route as a question: the option is the answer and the
// note goes as feedback; dismissing it (reject without an answer) records no choice.
function decisionCard(a) {
  const agent = S.agents[a.agent_id] || { name: a.agent_name || 'An agent', role: a.agent_role || '' };
  const p = a.payload || {};
  const opts = Array.isArray(p.options) ? p.options.map((o) => String(o)).filter((o) => o.trim()) : [];
  const rec = String(p.recommended || '');
  const due = decisionDeadline(p);
  const after = p.reversible === false ? 'It cannot be undone, so it waits for your answer.'
    : rec && due.text.startsWith('auto') ? `If you don't answer, "${esc(rec)}" is taken ${esc(due.text.replace(/^auto /, ''))}.` : '';
  return `<article class="req dec" data-req="${a.id}">
    <div class="req-head">${avatar(agent)}<div class="what"><b>${esc(agent.name)}</b> <span class="muted">at <a href="#/c/${a.company_id}">${esc(a.company_name)}</a> ${ACTION_VERB.decision}</span>
      <div class="faint small req-meta"><span>Decision #${esc(a.id)}</span><span class="badge ${due.cls}" title="${esc(due.title)}">${esc(due.text)}</span><span>${when(a.created_at)}</span></div></div></div>
    <div class="fields"><div class="box md">${md(p.question)}</div>
      ${p.context ? `<details class="dec-context"><summary class="small">Context</summary><p class="small muted">${esc(p.context)}</p></details>` : ''}
      <div class="opts">${opts.map((o) => `<button class="btn ${o === rec ? 'primary' : ''}" type="button" data-act="decide" data-d="answer" data-answer="${esc(o)}">${o === rec ? '✓ ' : ''}${esc(o)}${o === rec ? ' (recommended)' : ''}</button>`).join('')}</div>
      ${after ? `<p class="small faint" style="margin:0">${after}</p>` : ''}
      <label class="field">Note for the team (optional)<textarea data-k="feedback" rows="2" placeholder="Why you chose it, or what to do next"></textarea></label></div>
    <div class="foot"><button class="btn sm ghost" data-act="decide" data-d="reject" title="Close it without choosing; the team decides within its own authority">Dismiss without choosing</button></div>
  </article>`;
}

async function decideRequest(card, decision, preset = '') {
  const id = +card.dataset.req;
  const payload = {};
  $$('[data-k]', card).forEach((f) => { payload[f.dataset.k] = f.type === 'checkbox' ? f.checked : f.value; });
  const { feedback = '', answer: typed = '' } = payload;
  delete payload.feedback; delete payload.answer;
  const answer = preset || typed;   // an option button answers with its exact text
  if (decision === 'answer' && !answer.trim()) { toast('Write an answer first.', true); return; }
  $$('button', card).forEach((b) => { b.disabled = true; });
  try {
    const r = await api('POST', `/api/approvals/${id}`, { decision, payload, feedback, answer });
    const a = r.approval;
    const dec = a.action === 'decision';
    const msg = { done: 'Done.', rejected: dec ? 'Dismissed.' : feedback ? 'Rejected. The agent will revise it.' : 'Rejected.', answered: dec ? `Decided: ${a.result}` : 'Answer sent.', failed: `Couldn't complete it: ${a.result} You can retry it from Recent decisions.` }[a.status] || a.status;
    toast(msg.slice(0, 220), a.status === 'failed');
    card.remove();
    refreshState();
    if (S.view && S.view.reloadDecided) S.view.reloadDecided();
  } catch (e) { fail(e); $$('button', card).forEach((b) => { b.disabled = false; }); }
}

// The one-line summary the server stores for an approval, rebuilt from an audit row's payload.
function auditSummary(a) {
  const p = a.payload || {};
  switch (a.action) {
    case 'send_email': return `Email to ${p.to || '?'}: ${p.subject || ''}`;
    case 'social_post': return `${platformName(p.platform)} post: ${String(p.content || '').slice(0, 90)}`;
    case 'deploy_site': return 'Published the website from site/';
    case 'http_post': return `${p.method || 'POST'} ${p.url || ''}`;
    case 'run_code': return `Ran ${p.language || 'python'}: ${String(p.code || '').trim().split('\n')[0].slice(0, 90)}`;
    case 'spend_money': return `Buy ${p.item || '?'} for $${p.amount_usd}${p.vendor ? ` (${p.vendor})` : ''}`;
    case 'connector_call': return `${connectorLabel(p.connector)} ${p.tool || '?'}(${JSON.stringify(p.args ?? {}).slice(0, 90)})${p.tag ? ` [${p.tag}]` : ''}`;
    default: return a.action;
  }
}

function Inbox() {
  let el;
  const trust = {};   // company id -> action -> its trust row (Phase 3), for the streak on each card
  const slugOf = (cid) => (S.st?.companies.find((c) => c.id === cid) || {}).slug || '';
  const loadAudit = async () => {
    const r = await GET('/api/audit?limit=50');
    const who = (by) => {
      const lvl = /^trust:(\d)/.exec(by || '');
      if (lvl) return lvl[1] === '1' ? 'the trust ladder (on its own, up to a daily cap)' : 'the trust ladder (on its own)';
      return by === 'owner' ? 'you' : by === 'policy:auto' ? 'the auto policy' : by || '?';
    };
    $('#audit', el).innerHTML = r.audit.length ? r.audit.map((a) => `
      <li><span class="badge ${/^FAILED/.test(a.result || '') ? 'bad' : 'good'}">${esc(ACTION_LABEL[a.action] || a.action)}</span><div class="grow">
        <div>${esc(auditSummary(a))}</div>
        <div class="faint small">${esc(a.company_name || 'Holdco')}, ${when(a.created_at)}. Proposed by ${esc(a.proposed_by || 'an agent')}, allowed by ${esc(who(a.approved_by))}.</div>
        ${a.result ? `<div class="small muted" style="margin-top:4px">${esc(String(a.result).slice(0, 120))}</div>` : ''}</div></li>`).join('')
      : '<li class="faint">Nothing has run yet. Approved and automatic actions are listed here.</li>';
  };
  // GET /api/approvals returns decisions with the rest (Phase 2): they get their own panel above the requests.
  // Cards already shown stay as they are, so a note being typed survives a refresh.
  const fill = (list, rows, card) => {
    const have = new Set($$('[data-req]', list).map((n) => +n.dataset.req));
    const want = new Set(rows.map((a) => a.id));
    $$('[data-req]', list).forEach((n) => { if (!want.has(+n.dataset.req)) n.remove(); });
    rows.filter((a) => !have.has(a.id)).reverse().forEach((a) => {
      a.company_slug = slugOf(a.company_id);
      list.insertAdjacentHTML('afterbegin', card(a));
    });
  };
  // Phase 3: the streak of every card on the trust ladder ("12 approvals in a row without edits"); none at 0.
  const loadTrust = async (requests) => {
    const ids = [...new Set(requests.filter((a) => a.action !== 'question' && ladderAction(a)).map((a) => a.company_id))];
    const got = await Promise.all(ids.map((cid) => GET(`/api/companies/${cid}/trust`).catch(() => null)));
    ids.forEach((cid, i) => {
      if (got[i] && Array.isArray(got[i].actions)) trust[cid] = { rows: Object.fromEntries(got[i].actions.map((t) => [t.action, t])), promoteAt: got[i].promote_at };
    });
  };
  const paintStreaks = () => {
    $$('[data-streak]', el).forEach((s) => {
      const [cid, action] = s.dataset.streak.split(':');
      const co = trust[cid];
      const row = co ? co.rows[action] : null;
      const n = row && row.on_ladder ? Number(row.streak) || 0 : 0;
      s.textContent = n ? streakText(n) : '';
      s.title = n ? `Approving without changes adds one; an edit starts again at zero.${co.promoteAt ? ` At ${co.promoteAt} Holdco asks whether agents may do this on their own.` : ''}` : '';
    });
  };
  // run_code cards say whether the code runs in a sandbox (the Doctor's row; GET /api/doctor is the cached result).
  const loadSandbox = async (requests) => {
    if (!requests.some((a) => a.action === 'run_code')) return;
    const d = await GET('/api/doctor').catch(() => null);
    if (d) S.sandbox = sandboxOf(d.checks);
  };
  const loadPending = async () => {
    const r = await GET('/api/approvals');
    const decisions = r.approvals.filter((a) => a.action === 'decision');
    const requests = r.approvals.filter((a) => a.action !== 'decision');
    await Promise.all([loadTrust(requests), loadSandbox(requests)]);
    fill($('#dec-list', el), decisions, decisionCard);
    fill($('#req-list', el), requests, requestCard);
    paintStreaks();
    $('#dec-panel', el).classList.toggle('hidden', !decisions.length);
    const empty = $('#req-empty', el);
    empty.classList.toggle('hidden', requests.length > 0);
    empty.innerHTML = decisions.length ? '<p class="faint" style="margin:0">No requests waiting. The decisions above need you.</p>'
      : `<h2>You're all caught up</h2><p>When an agent wants to email someone, post, deploy the site, run code, spend money or use a connected service, it shows up here first. Decisions your team needs from you appear above them.</p>`;
  };
  const loadDecided = async () => {
    const r = await GET('/api/approvals?status=decided');
    const badge = { done: 'good', answered: 'good', rejected: '', failed: 'bad', running: 'accent' };
    const by = { deadline: '<span class="badge" title="Nobody answered in time, so the recommended option was taken">decided by the deadline</span> ', telegram: '<span class="badge">from your phone</span> ' };
    $('#decided', el).innerHTML = r.approvals.length ? r.approvals.slice(0, 30).map((a) => `
      <li>${avatar(S.agents[a.agent_id] || { name: a.agent_name || '?', role: a.agent_role })}<div class="grow">
        <div><span class="badge ${badge[a.status] || ''}">${esc(a.action === 'decision' && a.status === 'rejected' ? 'dismissed' : a.status)}</span> ${by[a.decided_by] || ''}${esc(a.summary)}</div>
        <div class="faint small">${esc(a.company_name)}, ${when(a.decided_at)}${a.feedback ? `. Your note: ${esc(a.feedback)}` : ''}</div>
        ${a.result ? `<div class="small muted" style="margin-top:4px">${inline(a.result.slice(0, 400))}</div>` : ''}</div>
        ${a.status === 'failed' ? `<button class="btn sm" data-act="retry" data-id="${a.id}">Retry</button>` : ''}</li>`).join('')
      : '<li class="faint">Nothing decided yet.</li>';
  };
  return {
    async mount(holder) {
      el = holder;
      el.innerHTML = `<div class="page-head"><div><h1>Inbox</h1><p class="sub">Decisions your team needs from you, then everything your companies want to send, publish, run or buy. Edit anything before you approve it.</p></div></div>
        <div class="stack"><section class="panel hidden" id="dec-panel"><div class="panel-head"><h2>Decisions</h2><span class="small faint">Pick an option. If one can be undone, the recommended option is taken at its deadline.</span></div><div id="dec-list"></div></section>
          <section class="panel"><div class="panel-head"><h2>Requests</h2></div><div id="req-list"></div>
          <div id="req-empty" class="empty hidden"></div></section>
          <section class="panel"><div class="panel-head"><h2>Recent decisions</h2></div><ul class="decided" id="decided" style="padding-top:8px"></ul></section>
          <section class="panel"><div class="panel-head"><h2>Audit log</h2><span class="small faint">Every outbound action that ran, and who allowed it.</span></div><ul class="rows" id="audit" style="padding-top:8px"></ul></section></div>`;
      await loadConnectors().catch(() => null);   // connector_call cards name the service and what agents can do with it
      await Promise.all([loadPending(), loadDecided(), loadAudit()]);
    },
    live(ev) {
      if (ev.type === 'approvals') { loadPending().catch(() => {}); loadDecided().catch(() => {}); loadAudit().catch(() => {}); }
      if (ev.type === 'trust') loadPending().catch(() => {});   // a level or streak changed elsewhere
    },
    reloadDecided: () => { loadDecided().catch(() => {}); loadAudit().catch(() => {}); },
    async act(name, t) {
      if (name === 'retry') {
        try { await api('POST', `/api/approvals/${t.dataset.id}/retry`); toast('Back in your inbox.'); await Promise.all([loadPending(), loadDecided()]); refreshState(); } catch (e) { fail(e); }
      }
    },
  };
}

// ---------------------------------------------------------------- library
function Library() {
  return {
    async mount(el) {
      const data = await loadPresets();
      el.innerHTML = `<div class="page-head"><div><h1>Agent library</h1><p class="sub">${data.presets.length} researched roles, each with a playbook, rules, recurring duties and model settings. Pick a team template when you start a company, or hire individual roles from a company's Team tab.</p></div></div>
        <section class="panel" style="margin-bottom:22px"><div class="panel-head"><h2>Team templates</h2></div><div class="panel-body">
          <div class="tpl-grid">${Object.values(data.templates).map((t) => `<div class="tpl static"><span class="tpl-body"><span class="tpl-title"><b>${esc(t.label)}</b>${t.recommended ? ' <span class="badge accent">Default</span>' : ''}</span><span class="small muted">${esc(t.description)}</span>
            <span class="chips">${t.team.map((m) => avatar({ name: m.name, role: m.key, title: m.title }, 'sm', false)).join('')}</span></span></div>`).join('')}</div></div></section>
        ${presetGallery(data)}`;
    },
    act(name, t) { if (name === 'preset-info') showPreset(t.dataset.key); },
  };
}

// ---------------------------------------------------------------- doctor
const CHECK_STATUS = { ok: ['good', 'OK'], warn: ['warn', 'Warning'], bad: ['bad', 'Failing'] };

function Doctor() {
  let el, checks = [], backups = null, timer = 0;
  const view = {};
  const fixHtml = (f) => {
    if (!f) return '';
    if (f.kind === 'link') return `<a class="btn sm" href="${esc(f.href || '#/')}">${esc(f.label)}</a>`;
    if (f.kind === 'action') return `<button class="btn sm" type="button" data-act="fix" data-fix="${esc(f.action || '')}">${esc(f.label)}</button>`;
    return `<span class="muted small mono">${esc(f.label)}</span>`;
  };
  // A connector:<instance id> row (Phase 1) links to its card: the company's Settings tab or the global Settings
  // page, with the instance in the address so the page scrolls to it. Older connector:<name>:<company> rows keep theirs.
  const fixOf = (r) => {
    const f = r.fix;
    if (!/^connector:\d+$/.test(String(r.key || '')) || !r.instance_id) return f;
    const page = f && f.kind === 'link' && /^#\/(c\/\d+\/)?settings$/.test(f.href || '') ? f.href
      : r.company_id ? `#/c/${r.company_id}/settings` : '#/settings';
    return { kind: 'link', label: (f && f.label) || 'Connector settings', href: `${page}/connector-${r.instance_id}` };
  };
  const drawChecks = () => {
    const newest = checks.map((r) => r.checked_at).filter(Boolean).sort().pop();
    $('#doc-when', el).innerHTML = newest ? `Checked ${when(newest)}` : '';
    const n = { ok: 0, warn: 0, bad: 0 };
    checks.forEach((r) => { n[r.status] = (n[r.status] || 0) + 1; });
    $('#doc-sum', el).innerHTML = checks.length ? `<span class="badge good">${n.ok} ok</span> <span class="badge warn">${n.warn} warn</span> <span class="badge bad">${n.bad} failing</span>` : '';
    $('#doc-list', el).innerHTML = checks.length ? checks.map((r) => {
      const s = CHECK_STATUS[r.status] || ['', esc(r.status)];
      return `<li><span class="badge ${s[0]}" style="min-width:74px;justify-content:center">${s[1]}</span>
        <div class="grow"><b>${esc(r.label)}</b><div class="small muted doc-detail">${esc(r.detail)}</div></div>
        <div class="fix">${fixHtml(fixOf(r))}</div></li>`;
    }).join('') : '<li class="faint">No checks have run yet.</li>';
  };
  const drawBackups = () => {
    const box = $('#doc-backups', el);
    if (!backups) { box.innerHTML = '<li class="faint">Loading…</li>'; return; }
    box.innerHTML = backups.backups.length ? backups.backups.slice(0, 10).map((b) => `<li><div class="grow"><code class="small">${esc(b.name)}</code></div><span class="small muted num">${fmtBytes(b.size)}</span><span class="small faint">${when(b.created_at)}</span></li>`).join('')
      : '<li class="faint">No backups yet. One is written daily, or press Back up now.</li>';
    $('#doc-keep', el).innerHTML = `Keeping the newest ${plural(backups.keep, 'backup')} in <code class="small" style="overflow-wrap:anywhere">${esc(backups.dir || backups.folder || '')}</code>. To restore: stop Holdco, unzip one over the data folder, start again.`;
  };
  const load = async () => {
    const [d, b] = await Promise.all([GET('/api/doctor'), GET('/api/backups').catch(() => null)]);
    if (!el || S.view !== view) return;
    checks = d.checks || [];
    if (b) backups = b;
    drawChecks();
    drawBackups();
  };
  const run = async (deep, btn) => {
    const buttons = $$('.doc-actions button', el);
    buttons.forEach((b) => { b.disabled = true; });
    const label = btn.innerHTML;
    btn.innerHTML = `<span class="spinner"></span> ${deep ? 'Checking, up to a minute…' : 'Checking…'}`;
    try {
      const d = await api('POST', '/api/doctor/run', { deep });
      checks = d.checks || [];
      drawChecks();
      refreshState();
    } catch (e) { fail(e); }
    btn.innerHTML = label;
    buttons.forEach((b) => { b.disabled = false; });
  };
  const backupNow = async (btn) => {
    btn.disabled = true;
    const label = btn.innerHTML;
    btn.innerHTML = '<span class="spinner"></span> Backing up…';
    try {
      const r = await api('POST', '/api/backup', {});
      toast(`Backup written: ${r.name} (${fmtBytes(r.size)}).`);
      backups = await GET('/api/backups');
      drawBackups();
      const d = await api('POST', '/api/doctor/run', { deep: false });
      checks = d.checks || [];
      drawChecks();
      refreshState();
    } catch (e) { fail(e); }
    btn.innerHTML = label;
    btn.disabled = false;
  };
  Object.assign(view, {
    async mount(holder) {
      el = holder;
      el.innerHTML = `<div class="page-head"><div><h1>Doctor</h1><p class="sub">Everything your agents depend on, checked every hour. Fix what is failing first.</p></div>
        <div class="actions doc-actions"><span id="doc-when" class="small faint"></span><button class="btn" data-act="run">Run checks</button><button class="btn" data-act="deep">Deep check (model + live search)</button><button class="btn" data-act="backup">Back up now</button></div></div>
        <div class="cols"><section class="panel"><div class="panel-head"><h2>Checks</h2><span id="doc-sum"></span></div><ul class="rows doc-rows" id="doc-list" style="padding-top:8px"><li class="faint"><span class="spinner"></span> Running checks…</li></ul></section>
        <section class="panel"><div class="panel-head"><h2>Backups</h2></div><ul class="rows" id="doc-backups" style="padding-top:8px"></ul><p class="small muted" id="doc-keep" style="padding:0 18px 14px"></p></section></div>`;
      await load();
      timer = setInterval(() => { if (S.view !== view) { clearInterval(timer); return; } load().catch(() => {}); }, 60000);
    },
    live(ev) { if (ev.type === 'settings' || ev.type === 'doctor' || ev.type === 'connectors') load().catch(() => {}); },
    async act(name, t) {
      if (name === 'fix' && t.dataset.fix === 'reindex') {
        t.disabled = true;
        t.innerHTML = '<span class="spinner"></span> Indexing…';
        try { const r = await api('POST', '/api/index/rebuild', {}); toast(r.message); } catch (e) { fail(e); t.disabled = false; t.textContent = 'Index now'; }
        return undefined;
      }
      if (name === 'run') return run(false, t);
      if (name === 'deep') return run(true, t);
      if (name === 'backup' || (name === 'fix' && t.dataset.fix === 'backup')) return backupNow(t);
      return undefined;
    },
  });
  return view;
}

// ---------------------------------------------------------------- phase 1: connector cards
// One card per connector definition (docs/phases/phase-1-connect.md, item 5), grouped by category. Used by the
// company Settings tab (scope company) and the global Settings page (scope global).
// opts: { scope: 'company' | 'global', cid: the company (company scope), focus: an instance id to scroll to,
//         settings: () => the global settings (masked), for a field kept as a global setting ("setting"),
//         onSaved: (definition) => called after a save }
const connectorFocus = (arg) => { const m = /^connector-(\d+)$/.exec(arg || ''); return m ? +m[1] : 0; };

function ConnectorCards(opts) {
  let el = null, data = null, oauthId = 0;
  const company = opts.scope === 'company';
  const guides = new Map();   // key -> the owner's open or closed choice for the setup guide
  const dirty = new Set();    // keys of cards with unsaved typing: a refresh redraws only their status
  const mine = (i) => (company ? i.company_id === opts.cid : i.company_id == null);
  const own = (key) => (data ? data.instances.find((i) => i.key === key && mine(i)) : null) || null;
  const defsOf = (scope) => Object.values(data.definitions).filter((d) => (d.scope || 'company') === scope);
  const flag = (d, k) => (d[k] !== undefined ? d[k] : (d.setup || {})[k]);   // beta, enabled_by_default
  const labelOf = (key) => ((data && data.definitions[key]) || {}).label || key;

  const healthBadge = (inst) => {
    if (!inst) return '<span class="badge">Not set up</span>';
    if (!inst.enabled) return '<span class="badge">Off</span>';
    const s = CHECK_STATUS[inst.status];
    return s ? `<span class="badge ${s[0]}">${s[1]}</span>` : '<span class="badge">Not checked yet</span>';
  };
  const modeBadge = (mode) => (mode === 'live' ? '<span class="badge warn" title="Real money and real customers">Live mode</span>'
    : mode === 'test' ? '<span class="badge accent" title="Test data only">Test mode</span>' : '');
  const stateHtml = (d, inst) => healthBadge(inst) + (inst ? modeBadge(inst.mode) : '')
    + (d.oauth && inst && inst.signed_in ? '<span class="badge good">Signed in</span>' : '');
  const metricTile = (key, m) => {
    const name = String(m.name || '');
    const short = name.startsWith(`${key}.`) ? name.slice(key.length + 1) : name;
    const title = [m.note, m.created_at ? `Read ${new Date(m.created_at).toLocaleString()}` : ''].filter(Boolean).join('. ');
    return `<div class="kpi" title="${esc(title)}"><div class="v">${esc(fmtValue(m.value))}</div><div class="k">${esc(short.replace(/[._]/g, ' '))}</div></div>`;
  };
  const statusHtml = (d, inst) => {
    if (!inst) return '<p class="small faint" style="margin:0">Not connected. Fill in the fields and press Save and test.</p>';
    const metrics = Array.isArray(inst.metrics) ? inst.metrics : [];
    return `<p class="conn-detail">${inst.detail ? esc(sentence(inst.detail)) : 'No check has run yet.'}${inst.last_check ? ` <span class="faint">Checked ${when(inst.last_check)}.</span>` : ''}</p>
      ${metrics.length ? `<div class="kpis conn-metrics">${metrics.map((m) => metricTile(d.key, m)).join('')}</div>`
        : d.has_metrics && inst.enabled ? '<p class="small faint" style="margin:0">No numbers yet. Once a check passes, Test reads them; after that Holdco reads them every hour.</p>' : ''}`;
  };
  const toolsHtml = (d) => {
    const tools = Array.isArray(d.tools) ? d.tools : [];
    const blocked = Array.isArray(d.blocked) ? d.blocked : [];
    if (!tools.length && !blocked.length) return '';
    const plain = tools.every((t) => !t.description);   // MCP tools come from the classification map, without text
    const item = (t) => `<code>${esc(t.tool || t.name)}</code> ${tagBadge(t.tag)}`;
    return `<div><b class="small">Tools agents can load</b>${!tools.length ? ''
      : plain ? `<div class="conn-chips">${tools.map((t) => `<span class="conn-chip">${item(t)}</span>`).join('')}</div>`
        : `<ul>${tools.map((t) => `<li>${item(t)}${t.description ? `<div class="faint small">${esc(t.description)}</div>` : ''}</li>`).join('')}</ul>`}
      ${blocked.length ? `<p class="small muted" style="margin:6px 0 0">Never available: ${blocked.map((b) => `<code>${esc(b)}</code>`).join(', ')}</p>` : ''}</div>`;
  };
  const guideHtml = (d, inst) => {
    const s = d.setup || {};
    const perms = Array.isArray(s.permissions) ? s.permissions : [];
    const docs = /^https?:\/\//.test(s.docs_url || '') ? s.docs_url : '';
    if (!s.where_to_get_key && !perms.length && !s.test_mode && !docs) return '';
    const open = guides.has(d.key) ? guides.get(d.key) : !inst;   // open until the card is set up
    return `<details class="conn-guide" data-guide="${esc(d.key)}"${open ? ' open' : ''}><summary class="small">How to connect it</summary><div class="conn-guide-body">
      ${s.where_to_get_key ? `<p><b>Where to get the key.</b> ${esc(s.where_to_get_key)}</p>` : ''}
      ${perms.length ? `<p style="margin-bottom:2px"><b>Permissions to tick</b></p><ul>${perms.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}
      ${s.test_mode ? `<p><b>Test mode.</b> ${esc(s.test_mode)}</p>` : ''}
      ${docs ? `<p><a href="${esc(docs)}" target="_blank" rel="noopener noreferrer">${esc(d.label || d.key)} documentation</a></p>` : ''}</div></details>`;
  };
  const fieldsHtml = (d, inst) => {
    const cfg = (inst && inst.config) || {};
    const globals = (opts.settings && opts.settings()) || {};
    const fields = ((d.setup || {}).fields || []).filter((f) => f && f.key && !f.hidden);
    const value = (f) => {
      const v = cfg[f.key];
      return (v === undefined || v === null || v === '') && f.setting ? globals[f.setting] : v;
    };
    const inputs = fields.filter((f) => f.type !== 'bool').map((f) => `<label class="field">${esc(f.label || f.key)}${f.hint ? `<span class="hint">${esc(f.hint)}</span>` : ''}
      <input type="${f.secret ? 'password' : 'text'}" data-field="${esc(f.key)}" data-kind="${f.secret ? 'secret' : 'text'}" value="${esc(value(f) ?? '')}" placeholder="${esc(f.placeholder || '')}" autocomplete="${f.secret ? 'new-password' : 'off'}" spellcheck="false"></label>`);
    const bools = fields.filter((f) => f.type === 'bool').map((f) => `<label class="check"><input type="checkbox" data-field="${esc(f.key)}" data-kind="bool"${isOn(value(f)) ? ' checked' : ''}>
      <span>${esc(f.label || f.key)}${f.hint ? `<br><span class="faint small">${esc(f.hint)}</span>` : ''}</span></label>`);
    return (inputs.length ? `<div class="form-grid">${inputs.join('')}</div>` : '') + bools.join('');
  };
  const card = (d) => {
    const inst = own(d.key);
    const s = d.setup || {};
    const beta = !!flag(d, 'beta');
    const enabled = inst ? !!inst.enabled : flag(d, 'enabled_by_default') !== false;
    const can = Array.isArray(s.agents_can) ? s.agents_can : [];
    const note = d.note || s.note || '';
    return `<article class="conn" id="conn-${esc(d.key)}" data-conn="${esc(d.key)}"${inst ? ` data-inst="${esc(inst.id)}"` : ''}>
      <div class="conn-head"><div class="conn-title"><h3>${esc(d.label || d.key)}</h3><span class="badge">${esc(d.category || 'Other')}</span>
          ${d.kind ? `<span class="badge" title="How Holdco talks to it">${esc(KIND_LABEL[d.kind] || d.kind)}</span>` : ''}${beta ? '<span class="badge warn">Beta</span>' : ''}</div>
        <div class="conn-state" data-conn-state>${stateHtml(d, inst)}</div></div>
      ${note ? `<p class="small conn-note ${beta ? 'warn-text' : 'muted'}">${esc(note)}</p>` : ''}
      <div class="conn-info">${can.length ? `<div><b class="small">What agents can do</b><ul>${can.map((x) => `<li>${esc(x)}</li>`).join('')}</ul></div>` : ''}${toolsHtml(d)}</div>
      ${guideHtml(d, inst)}
      <form class="form-stack conn-form" data-form="connector-${esc(d.key)}" data-key="${esc(d.key)}">
        ${fieldsHtml(d, inst)}
        <div class="actions">
          <label class="check small conn-enabled"><input type="checkbox" name="conn_enabled"${enabled ? ' checked' : ''}><span>Enabled</span></label>
          <button class="btn primary" type="submit">Save and test</button>
          ${inst ? `<button class="btn" type="button" data-act="conn-test" data-id="${esc(inst.id)}">Test</button>` : ''}
          ${d.oauth && inst && !inst.signed_in ? `<button class="btn" type="button" data-act="conn-signin" data-id="${esc(inst.id)}">Sign in</button>` : ''}
          ${inst ? `<button class="btn danger" type="button" data-act="conn-remove" data-id="${esc(inst.id)}">Remove</button>` : ''}
        </div></form>
      <div class="conn-status" data-conn-status>${statusHtml(d, inst)}</div>
    </article>`;
  };
  // Read-only rows: on a company tab, the global instances that apply to it; on the global page, every company's own.
  const extraHtml = () => {
    const row = (i, href) => `<li><span class="conn-row-state">${healthBadge(i)}${modeBadge(i.mode)}</span>
      <div class="grow"><b>${esc(i.label || labelOf(i.key))}</b>${company ? '' : ` <span class="muted">at ${esc(i.company_name || `company ${i.company_id}`)}</span>`}
        <div class="small muted">${esc(sentence(i.detail) || 'Not checked yet.')}${i.last_check ? ` <span class="faint">Checked ${when(i.last_check)}.</span>` : ''}</div></div>
      <a class="btn sm" href="${esc(href)}">Open</a></li>`;
    if (company) {
      const shared = data.instances.filter((i) => i.company_id == null && (data.definitions[i.key] || {}).scope === 'global');
      const names = andList(defsOf('global').map((d) => d.label || d.key));
      return `<h3 class="conn-cat">Set up once for every company</h3>${shared.length
        ? `<ul class="rows conn-rows">${shared.map((i) => row(i, `#/settings/connector-${i.id}`)).join('')}</ul>`
        : `<p class="small faint">Nothing yet.${names ? ` ${esc(names)} ${defsOf('global').length === 1 ? 'is' : 'are'} set up on the <a href="#/settings">Settings page</a> and apply to every company.` : ''}</p>`}`;
    }
    const per = data.instances.filter((i) => i.company_id != null);
    const names = andList(defsOf('company').map((d) => d.label || d.key));
    return `<h3 class="conn-cat">Set up per company</h3>${per.length
      ? `<ul class="rows conn-rows">${per.map((i) => row(i, `#/c/${i.company_id}/settings/connector-${i.id}`)).join('')}</ul>`
      : `<p class="small faint">None yet.${names ? ` ${esc(names)} belong to one company each: set them up in that company's Settings tab.` : ''}</p>`}`;
  };
  const body = () => {
    const list = defsOf(opts.scope);
    const cats = [...new Set(list.map((d) => d.category || 'Other'))];
    return (list.length ? cats.map((c) => `<div class="conn-group"><h3 class="conn-cat">${esc(c)}</h3><div class="conn-cards">${list.filter((d) => (d.category || 'Other') === c).map(card).join('')}</div></div>`).join('')
      : '<p class="faint">No connectors of this kind in this version of Holdco.</p>') + `<div class="conn-group" id="conn-extra">${extraHtml()}</div>`;
  };
  // Cards the owner is typing in keep their fields; everything else is redrawn from the server's answer.
  const paint = () => {
    const list = el && $('#conn-list', el);
    if (!list || !data) return;
    const cards = $$('[data-conn]', list);
    if (!cards.length) { list.innerHTML = body(); return; }
    cards.forEach((node) => {
      const d = data.definitions[node.dataset.conn];
      if (!d) { node.remove(); return; }
      const inst = own(d.key);
      if (!dirty.has(d.key)) { node.outerHTML = card(d); return; }
      const st = $('[data-conn-state]', node);
      const ss = $('[data-conn-status]', node);
      if (st) st.innerHTML = stateHtml(d, inst);
      if (ss) ss.innerHTML = statusHtml(d, inst);
    });
    const extra = $('#conn-extra', list);
    if (extra) extra.innerHTML = extraHtml();
  };
  const load = async () => {
    const r = await GET(company ? `/api/connectors?company_id=${opts.cid}` : '/api/connectors');
    data = { definitions: r.definitions || {}, instances: Array.isArray(r.instances) ? r.instances : [] };
    S.connectors = r;
    paint();
  };
  const loadSoon = debounce(() => load().catch(() => {}), 700);
  const reveal = (id) => {
    const node = el && $(`[data-inst="${+id}"]`, el);
    if (!node) return;
    node.classList.add('flash');
    node.scrollIntoView({ block: 'start', behavior: 'smooth' });
    setTimeout(() => node.classList.remove('flash'), 2600);
  };
  const say = (label, h, extra = '') => {
    const detail = h && h.detail ? ` ${sentence(h.detail)}` : '';
    if (h && h.status === 'ok') toast(`${label} works.${detail}${extra}`);
    else if (h && h.status === 'warn') toast(`${label} saved with a warning.${detail}`);
    else toast(`${label}: the check failed.${detail}`, true);
  };
  const busy = (btn, text) => {
    const was = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = `<span class="spinner"></span> ${esc(text)}`;
    return () => { btn.disabled = false; btn.innerHTML = was; };
  };
  const slowText = (d, saving) => (d && d.kind === 'mcp-stdio' ? 'Starting it; the first time can take two minutes…' : saving ? 'Saving and testing…' : 'Testing…');
  // After an OAuth sign-in in another tab: when the owner comes back, reload; once signed in, test right away.
  const onFocus = async () => {
    window.removeEventListener('focus', onFocus);
    const id = oauthId;
    if (!id || !el || el.isConnected === false) { oauthId = 0; return; }
    try {
      await load();
      const inst = data.instances.find((i) => i.id === id);
      if (!inst || !inst.signed_in) { window.addEventListener('focus', onFocus); return; }   // not finished yet
      oauthId = 0;
      const r = await api('POST', `/api/connectors/${id}/test`, {});
      say(`Signed in. ${inst.label || labelOf(inst.key)}`, r.health);
      await load();
    } catch (e) { oauthId = 0; fail(e); }
  };
  const signIn = async (id, btn) => {
    // The tab opens inside the click, so the browser does not block it; the address comes from the server.
    let win = null;
    try { win = window.open('', '_blank'); if (win) win.opener = null; } catch (_) { win = null; }
    const done = busy(btn, 'Opening…');
    try {
      const r = await api('POST', `/api/connectors/${id}/oauth/start`, {});
      if (!/^https?:\/\//.test(r.url || '')) throw new Error('The server did not return a sign-in address.');
      if (win) win.location.href = r.url;
      else modal({ title: 'Sign in', body: `<p>Your browser blocked the new tab. <a href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">Open the sign-in page</a>, finish there, then come back to this tab.</p>` });
      toast('Finish signing in in the new tab, then come back here.');
      if (!oauthId) window.addEventListener('focus', onFocus);
      oauthId = id;
    } catch (e) { if (win) win.close(); fail(e); }
    done();
  };
  return {
    owns: (name) => /^conn-/.test(name),
    ownsForm: (name) => /^connector-/.test(name),
    async mount(holder) {
      el = holder;
      el.innerHTML = `<section class="panel conn-panel" id="connectors"><div class="panel-head"><h2>Connectors</h2>
          <span class="small faint">${company ? 'Accounts this company uses.' : 'Shared by every company.'} Keys stay in the vault on this PC and never reach a prompt.</span></div>
        <div class="panel-body" id="conn-list"><span class="spinner"></span> Loading connectors…</div></section>`;
      const touch = (e) => {
        const t = e.target;
        if (!t || !t.closest || !t.closest('form.conn-form')) return;
        const c = t.closest('[data-conn]');
        if (c) dirty.add(c.dataset.conn);
      };
      el.addEventListener('input', touch);
      el.addEventListener('change', touch);
      el.addEventListener('toggle', (e) => { const g = e.target; if (g && g.dataset && g.dataset.guide) guides.set(g.dataset.guide, g.open); }, true);
      try { await load(); } catch (e) { $('#conn-list', el).innerHTML = `<p class="warn-text" style="margin:0">Connectors could not load: ${esc(e.message)}</p>`; }
      if (opts.focus) reveal(opts.focus);
    },
    // Health checks, Doctor runs and OAuth sign-ins change what the cards show.
    live(ev) {
      const type = String(ev.type || '');
      if (/connector|doctor/.test(type) || (type === 'event' && /connector/.test(String(ev.kind || '')))) loadSoon();
    },
    async act(name, t) {
      const id = +t.dataset.id;
      const inst = data ? data.instances.find((i) => i.id === id) : null;
      const label = inst ? inst.label || labelOf(inst.key) : 'This connector';
      if (name === 'conn-jump') { const p = el && $('#connectors', el); if (p) p.scrollIntoView({ block: 'start', behavior: 'smooth' }); return; }
      if (name === 'conn-signin') { await signIn(id, t); return; }
      if (name === 'conn-test') {
        const done = busy(t, slowText(inst && data.definitions[inst.key], false));
        try {
          const r = await api('POST', `/api/connectors/${id}/test`, {});
          const n = Array.isArray(r.metrics) ? r.metrics.length : 0;
          say(label, r.health, n ? ` Read ${plural(n, 'number')}.` : '');
          await load();
        } catch (e) { fail(e); }
        done();
        return;
      }
      if (name === 'conn-remove') {
        modal({
          title: `Remove ${label}?`,
          body: `<p>Holdco deletes this connection and its keys from the vault on this PC. Agents lose access to it at once; numbers it already read stay in the history.</p>
            ${company ? '' : '<p class="muted small">It applies to every company, so they all lose it.</p>'}`,
          foot: `<button class="btn" data-act="close-modal">Cancel</button><button class="btn danger" data-act="conn-remove-yes" data-id="${esc(id)}">Remove</button>`,
        });
        return;
      }
      if (name === 'conn-remove-yes') {
        t.disabled = true;
        try {
          await api('DELETE', `/api/connectors/${id}`, {});
          closeModal();
          if (inst) { dirty.delete(inst.key); guides.delete(inst.key); }
          toast(`${label} removed.`);
          await load();
        } catch (e) { fail(e); t.disabled = false; }
      }
    },
    // data-form="connector-<key>": POST /api/connectors. A secret left as the mask keeps the stored key; an empty
    // one that was set deletes it; hidden fields (api_base) are never sent, so the server keeps them.
    async submit(name, form) {
      const key = form.dataset.key || name.replace(/^connector-/, '');
      const d = data && data.definitions[key];
      if (!d) return;
      const inst = own(key);
      const config = {};
      $$('[data-field]', form).forEach((inp) => {
        const k = inp.dataset.field;
        if (inp.dataset.kind === 'bool') config[k] = !!inp.checked;
        else if (inp.dataset.kind === 'secret') { if (inp.value || inp.defaultValue) config[k] = inp.value; }
        else config[k] = String(inp.value || '').trim();
      });
      const on = $('[name=conn_enabled]', form);
      const payload = { key, company_id: company ? opts.cid : null, enabled: !!(on && on.checked), config };
      if (inst) payload.id = inst.id;
      const btn = $('button[type=submit]', form);
      const done = btn ? busy(btn, slowText(d, true)) : () => {};
      try {
        const r = await api('POST', '/api/connectors', payload);
        dirty.delete(key);
        say(d.label || key, r.health);
        await load();
        if (opts.onSaved) opts.onSaved(d);
      } catch (e) { fail(e); }
      done();
    },
  };
}

// ---------------------------------------------------------------- settings
// Phase 3, section 9: how full the model's context got over the latest finished sessions (GET /api/settings).
function contextUseHtml(cu) {
  if (!cu) return '';
  if (!cu.n) return '<p class="small faint" style="margin:0">Context use: no finished session has reported its prompt size yet.</p>';
  const ctx = Number(cu.num_ctx) || 0;
  const share = ctx ? Math.min(100, Math.round((100 * (Number(cu.p95) || 0)) / ctx)) : 0;
  return `<div class="meter${cu.warn ? ' over' : ''}"><div class="meter-head"><span><b>Context use:</b> p50 ${fmtCount(cu.p50)}, p95 ${fmtCount(cu.p95)} of ${fmtCount(ctx)} tokens (last ${plural(cu.n, 'run')})</span>
      ${cu.warn ? '<span class="badge warn">warn</span>' : ''}</div>
    <div class="bar" role="meter" aria-valuemin="0" aria-valuemax="${esc(ctx)}" aria-valuenow="${esc(cu.p95)}" aria-label="Largest prompts against the context window"><span style="width:${share}%"></span></div>
    <p class="small ${cu.warn ? 'warn-text' : 'faint'}" style="margin:0">${cu.warn ? 'Agents are near the window; raise num_ctx (Context window above) or shorten prompts.'
      : 'The largest prompt each session sent: half of them stayed under p50, 95 in 100 under p95.'}</p></div>`;
}

function Settings(arg) {
  let el, data;
  const view = {};
  // Phase 3 bench: the run this page started or found running, its latest status, and the newest finished runs.
  let bench = { runId: 0, status: null, row: null, error: '' }, benchRuns = [];
  // A connector field kept as a global setting (Brave's key) is also in this page's form: keep that input in step.
  const conns = ConnectorCards({
    scope: 'global', focus: connectorFocus(arg), settings: () => (data ? data.settings : {}),
    onSaved: async (d) => {
      const keys = ((d.setup || {}).fields || []).map((f) => f && f.setting).filter(Boolean);
      if (!keys.length) return;
      try {
        const s = await GET('/api/settings');
        data = { ...data, settings: s.settings };
        syncSecrets($('form[data-form="settings"]', el), Object.fromEntries(keys.map((k) => [k, s.settings[k]])), true);
      } catch (_) { /* the form keeps what it shows */ }
    },
  });
  const sel = (name, value, opts) => `<select name="${name}">${opts.map(([v, l]) => `<option value="${esc(v)}" ${String(v) === String(value) ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select>`;
  const modelSel = (name, value, models, blank) => {
    const opts = [...(blank ? [['', blank]] : []), ...models.map((m) => [m, m])];
    if (value && !models.includes(value)) opts.push([value, `${value} (not installed)`]);
    return sel(name, value, opts);
  };
  const draw = (models) => {
    const s = data.settings;
    const pol = s.policies;
    $('#set-main', el).innerHTML = `<div class="page-head"><div><h1>Settings</h1><p class="sub">Applies to every company. Each company can override approvals in its own settings.</p></div>
      <div class="actions"><button class="btn" type="button" data-act="conn-jump">Connectors</button></div></div>
    <form class="panel" data-form="settings">
      <div class="section"><h2>Model</h2><p class="muted">Holdco talks to a model server on this PC. Ollama is the easiest; LM Studio, vLLM and llama.cpp work through the OpenAI-compatible option.</p>
        <div class="form-grid">
          <label class="field">Server type${sel('llm_backend', s.llm_backend, [['ollama', 'Ollama'], ['openai', 'OpenAI-compatible']])}</label>
          <label class="field">Server address<input type="text" name="llm_base_url" value="${esc(s.llm_base_url)}"></label>
          <label class="field">API key<span class="hint">Only if your server needs one.</span><input type="password" name="llm_api_key" value="${esc(s.llm_api_key)}" autocomplete="off" data-secret></label>
        </div>
        <div class="form-grid" style="margin-top:14px">
          <label class="field">Agent model<span class="hint">Used by every agent unless you set one per agent.</span>${models.length ? modelSel('default_model', s.default_model, models) : `<input type="text" name="default_model" value="${esc(s.default_model)}">`}</label>
          <label class="field">CEO model<span class="hint">Optional stronger model for planning.</span>${models.length ? modelSel('ceo_model', s.ceo_model, models, 'Same as agent model') : `<input type="text" name="ceo_model" value="${esc(s.ceo_model)}">`}</label>
          <label class="field">Small model<span class="hint">Optional; (none) uses the agent model. Sorts incoming email, shortens old tool results and spots duplicate notes.</span>${models.length ? modelSel('small_model', s.small_model, models, '(none)') : `<input type="text" name="small_model" value="${esc(s.small_model)}" placeholder="(none)" autocomplete="off">`}</label>
          <label class="field">Context window (tokens)<span class="hint">Ollama only. 32768 suits a 5090.</span><input type="number" name="num_ctx" min="4096" step="1024" value="${esc(s.num_ctx)}"></label>
        </div>
        <div id="ctx-use" class="ctx-use"></div>
        <div class="form-grid" style="margin-top:14px">
          <label class="field">Agents working at once<span class="hint">Match OLLAMA_NUM_PARALLEL.</span><input type="number" name="max_concurrent_runs" min="1" max="8" value="${esc(s.max_concurrent_runs)}"></label>
          <label class="field">Tool calls per session<input type="number" name="max_steps_per_run" min="4" max="60" value="${esc(s.max_steps_per_run)}"></label>
          <label class="field">Sampling<span class="hint">Automatic uses each model maker's recommended settings.</span>${sel('sampling', s.sampling || 'auto', [['auto', 'Automatic'], ['manual', 'Manual temperature']])}</label>
          <label class="field">Manual temperature<input type="number" name="temperature" min="0" max="1.5" step="0.1" value="${esc(s.temperature)}"></label>
          <label class="field">Tool calling${sel('tool_mode', s.tool_mode, [['auto', 'Automatic'], ['native', 'Native only'], ['json', 'Prompt-based (for models without tools)']])}</label>
        </div>
        <div class="actions" style="margin-top:14px"><button class="btn" type="button" data-act="test-llm">Test connection</button><span id="llm-result" class="small"></span></div>
        <div class="bench-test"><div class="actions"><button class="btn" type="button" data-act="bench-run">Test this model</button>
          <span class="small faint">Scores the agent model picked above on Holdco Bench: scripted jobs with pretend customers and services. It takes a while and changes no settings.</span></div>
          <div id="bench-box" class="bench-box small" aria-live="polite"></div></div>
      </div>
      <div class="section"><h2>Autopilot</h2>
        <label class="check"><input type="checkbox" name="scheduler_enabled" ${s.scheduler_enabled ? 'checked' : ''}><span>Keep agents working around the clock<br><span class="faint small">When off, agents only work when you press Run or chat with them.</span></span></label>
        <div class="form-grid" style="margin-top:14px"><label class="field">What agents call you<input type="text" name="owner_name" value="${esc(s.owner_name)}"></label></div>
      </div>
      <div class="section"><h2>Phone notifications</h2><p class="muted">Approve requests and answer questions from your phone. A Telegram bot gives you buttons; ntfy only notifies.</p>
        <div class="form-grid">
          <label class="field">Telegram bot token<input type="password" name="telegram_bot_token" value="${esc(s.telegram_bot_token)}" autocomplete="off" data-secret></label>
          <label class="field">Telegram chat id<input type="text" name="telegram_chat_id" value="${esc(s.telegram_chat_id)}" inputmode="numeric" autocomplete="off"></label>
          <label class="field">Notify me about${sel('notify_level', s.notify_level || 'important', [['important', 'Requests, questions and errors'], ['all', 'Everything, including finished tasks']])}</label>
        </div>
        <div class="form-grid" style="margin-top:14px">
          <label class="field">ntfy topic<span class="hint">Optional. Subscribe to the same topic in the ntfy app; pick a name nobody would guess.</span><input type="text" name="ntfy_topic" value="${esc(s.ntfy_topic)}" autocomplete="off"></label>
          <label class="field">ntfy server<input type="text" name="ntfy_server" value="${esc(s.ntfy_server)}" placeholder="https://ntfy.sh"></label>
        </div>
        <details style="margin-top:12px"><summary class="small muted" style="cursor:pointer">How to set up the Telegram bot</summary>
          <ol class="steps small" style="margin-top:8px">
            <li>In Telegram, open <b>@BotFather</b>, send <code>/newbot</code>, follow the prompts and paste the token it gives you above.</li>
            <li>Open your new bot and send it any message, so Telegram lets it reply to you.</li>
            <li>Find your chat id: message <b>@userinfobot</b>, or open <code>https://api.telegram.org/bot&lt;token&gt;/getUpdates</code> in a browser and read <code>chat.id</code>. Or leave the chat id empty, save, and press Send test message: Holdco reads it from your message.</li>
            <li>Paste the chat id, save, and press Send test message. A message with an OK button should arrive.</li>
          </ol></details>
        <div class="actions" style="margin-top:14px"><button class="btn" type="button" data-act="test-notify">Send test message</button><span id="notify-result" class="small"></span></div>
      </div>
      <div class="section"><h2>Reliability</h2><p class="muted">What happens when work keeps failing, and how many daily backups to keep. The Doctor page shows the current state.</p>
        <div class="form-grid">
          <label class="field">Attempts before a task is escalated<span class="hint">After this many failed attempts the task is blocked and you get one question.</span><input type="number" name="max_task_attempts" min="1" max="5" value="${esc(s.max_task_attempts)}"></label>
          <label class="field">Backups to keep<span class="hint">Older backups are deleted; each holds the database and every company folder.</span><input type="number" name="backup_keep" min="1" max="90" value="${esc(s.backup_keep)}"></label>
        </div>
      </div>
      <div class="section"><h2>Approvals</h2><p class="muted">What happens when an agent tries something that leaves your computer.</p>
        <div class="form-grid">${Object.entries(data.policy_actions).map(([k, label]) => `
          <label class="field">${esc(label)}${data.locked.includes(k) ? `<div class="box small">Always asks you</div>`
            : sel(`policy_${k}`, pol[k], [['approve', 'Ask me first'], ['auto', 'Let agents do it'], ['off', 'Never allow']])}</label>`).join('')}</div>
      </div>
      <div class="section"><h2>Context and review</h2><p class="muted">What every company should know about you lives on <a href="#/context">Your context</a>. These models are optional.</p>
        <div class="form-grid">
          <label class="field">Review model<span class="hint">Used for the critic pass after the charter and for QA reviews. Pick the strongest model you trust for judgment.</span>${models.length ? modelSel('review_model', s.review_model, models, 'Same as CEO/agent model') : `<input type="text" name="review_model" value="${esc(s.review_model)}" placeholder="Same as CEO/agent model">`}</label>
          <label class="field">Embedding model<span class="hint">Optional. An Ollama embedding model such as nomic-embed-text makes search find meaning, not just words. Run: ollama pull nomic-embed-text</span><input type="text" name="embedding_model" value="${esc(s.embedding_model)}" placeholder="nomic-embed-text" autocomplete="off"></label>
        </div>
      </div>
      <div class="section"><h2>Web search</h2>
        <div class="form-grid">
          <label class="field">Search with${sel('search_provider', s.search_provider, [['duckduckgo', 'DuckDuckGo (no key)'], ['searxng', 'SearXNG (self-hosted)'], ['brave', 'Brave Search API']])}</label>
          <label class="field">SearXNG address<input type="text" name="searxng_url" value="${esc(s.searxng_url)}" placeholder="http://127.0.0.1:8888"></label>
          <label class="field">Brave API key<span class="hint">The Brave Search card under Connectors keeps the same key and can test it.</span><input type="password" name="brave_api_key" value="${esc(s.brave_api_key)}" autocomplete="off" data-secret></label>
        </div>
      </div>
      <div class="section actions" style="justify-content:space-between"><span class="faint small">Data folder: <code>${esc(data.data_dir)}</code></span><button class="btn primary" type="submit">Save settings</button></div>
    </form>`;
    paintContext();
    paintBench();
  };
  const paintContext = () => { const box = el && $('#ctx-use', el); if (box) box.innerHTML = contextUseHtml(data.context_use); };
  // The agent model as picked in the form (saved or not): what "Test this model" scores.
  const pickedModel = () => {
    const f = el && $('form[data-form="settings"]', el);
    return String((f && f.default_model && f.default_model.value) || (data && data.settings.default_model) || '').trim();
  };
  // Running: the scenario now. Finished: the four scores. Otherwise the last finished test of the picked model.
  const paintBench = () => {
    const box = el && $('#bench-box', el);
    const btn = el && $('[data-act="bench-run"]', el);
    if (!box) return;
    const st = bench.status;
    if (btn) btn.disabled = !!(st && st.running);
    if (st && st.running) {
      box.innerHTML = `<span class="spinner"></span> ${esc(benchProgress(st))} <span class="faint">(testing ${esc(st.model || 'the model')})</span> <a href="#/bench">Watch on Model scores</a>`;
      return;
    }
    if (bench.error) { box.innerHTML = `<span class="badge bad">Not finished</span> ${esc(bench.error)}`; return; }
    if (bench.row) {
      const r = bench.row;
      box.innerHTML = r.finished_at
        ? `<span class="badge accent">${esc(r.model)}</span> ${esc(benchScores(r))}. <a href="#/bench/${esc(r.id)}">See every scenario</a>${r.error ? ` <div class="warn-text">${esc(r.error)}</div>` : ''}`
        : `<span class="badge warn">Did not finish</span> The test of ${esc(r.model)} stopped early. <a href="#/bench/${esc(r.id)}">Details</a>`;
      return;
    }
    const model = pickedModel();
    const last = benchRuns.find((r) => r.model === model && r.finished_at);
    box.innerHTML = last ? `<span class="faint">Last test of ${esc(model)}, ${esc(shortDate(last.started_at))}:</span> ${esc(benchScores(last))}. <a href="#/bench/${esc(last.id)}">Details</a> · <a href="#/bench">All model scores</a>`
      : `<span class="faint">${model ? `${esc(model)} has not been tested yet.` : 'Pick a model to test.'}</span> <a href="#/bench">All model scores</a>`;
  };
  const follow = async (runId) => {
    bench = { runId, status: { running: true, scenario: '', done: 0, total: 0, model: pickedModel() }, row: null, error: '' };
    paintBench();
    try {
      const row = await benchFollow(runId, () => S.view === view, (st) => { bench.status = st; paintBench(); });
      if (!row) return;
      bench = { runId, status: null, row, error: '' };
      benchRuns = [row, ...benchRuns.filter((r) => r.id !== row.id)];
      toast(row.finished_at ? `Test of ${row.model} finished: ${benchScores(row)}.` : `The test of ${row.model} stopped early.`, !row.finished_at);
    } catch (e) { bench = { runId, status: null, row: null, error: e.message }; }
    paintBench();
  };
  Object.assign(view, {
    async mount(holder) {
      el = holder;
      el.innerHTML = '<div id="set-main"></div><div id="set-conns" class="conn-holder"></div>';
      const [d, st, runs] = await Promise.all([GET('/api/settings'), GET('/api/bench/status').catch(() => null), GET('/api/bench/results?limit=50').catch(() => null)]);
      data = d;
      benchRuns = (runs && runs.runs) || [];
      draw(await loadModels(true));
      // A model picked in the select shows its own last score (unless a test is running).
      el.addEventListener('change', (e) => {
        if (e.target.name !== 'default_model' || !e.target.closest('form[data-form="settings"]') || (bench.status && bench.status.running)) return;
        bench = { runId: 0, status: null, row: null, error: '' };
        paintBench();
      });
      if (st && st.running && st.run_id) follow(st.run_id);
      await conns.mount($('#set-conns', el));
    },
    live(ev) { conns.live(ev); },
    async submit(name, form) {
      if (conns.ownsForm(name)) return conns.submit(name, form);
      if (name !== 'settings') return undefined;
      const f = new FormData(form);
      const body = {};
      for (const [k, v] of f.entries()) if (!k.startsWith('policy_')) body[k] = v;
      dropUntouchedSecrets(form, body);
      ['num_ctx', 'max_concurrent_runs', 'max_steps_per_run', 'max_task_attempts', 'backup_keep'].forEach((k) => {
        const n = parseInt(body[k], 10);
        if (Number.isFinite(n)) body[k] = n; else delete body[k];   // an empty box keeps the stored value
      });
      body.temperature = parseFloat(body.temperature);
      if (!Number.isFinite(body.temperature)) delete body.temperature;
      if (typeof body.telegram_chat_id === 'string') body.telegram_chat_id = body.telegram_chat_id.trim();
      if (typeof body.embedding_model === 'string') body.embedding_model = body.embedding_model.trim();
      if (typeof body.small_model === 'string') body.small_model = body.small_model.trim();
      body.scheduler_enabled = !!form.scheduler_enabled.checked;
      body.policies = {};
      Object.keys(data.policy_actions).forEach((k) => { if (form[`policy_${k}`]) body.policies[k] = form[`policy_${k}`].value; });
      try {
        data = { ...data, ...(await api('PUT', '/api/settings', body)) };
        syncSecrets(form, data.settings);   // a key just saved now shows the mask
        S.models = null;
        toast('Settings saved.');
        refreshState();
        // The context window may have changed: context use is measured against it.
        GET('/api/settings').then((s) => { data.context_use = s.context_use; paintContext(); }).catch(() => {});
      } catch (e) { fail(e); }
      return undefined;
    },
    async act(name, t) {
      if (conns.owns(name)) return conns.act(name, t);
      if (name === 'bench-run') {
        const model = pickedModel();
        if (!model) { toast('Pick an agent model to test first.', true); return undefined; }
        t.disabled = true;
        try { follow(await benchStart(model)); } catch (e) { fail(e); t.disabled = false; }
        return undefined;
      }
      if (name === 'test-llm') {
        const form = $('form[data-form="settings"]', el);
        await this.submit('settings', form);
        const out = $('#llm-result', el);
        out.innerHTML = '<span class="spinner"></span> Asking the model…';
        try {
          const r = await api('POST', '/api/llm/test', {});
          out.innerHTML = `<span class="badge ${r.ok ? 'good' : 'bad'}">${r.ok ? 'OK' : 'Failed'}</span> ${esc(r.message)}`;
          if (r.ok) { const models = await loadModels(true); data = await GET('/api/settings'); draw(models); $('#llm-result', el).innerHTML = `<span class="badge good">OK</span> ${esc(r.message)}`; }
        } catch (e) { out.textContent = e.message; }
      }
      if (name === 'test-notify') {
        const form = $('form[data-form="settings"]', el);
        await this.submit('settings', form);
        const out = $('#notify-result', el);
        out.innerHTML = '<span class="spinner"></span> Sending…';
        try {
          const r = await api('POST', '/api/notify/test', {});
          out.innerHTML = `<span class="badge ${r.ok ? 'good' : 'warn'}">${r.ok ? 'Sent' : 'Not yet'}</span> ${esc(r.detail || r.message || '')}`;
          // With a token but no chat id the server reports the chat ids that messaged the bot: offer the first one.
          if (!r.ok && Array.isArray(r.chat_ids) && r.chat_ids.length && form.telegram_chat_id && !form.telegram_chat_id.value.trim()) {
            form.telegram_chat_id.value = String(r.chat_ids[0]);
          }
        } catch (e) { out.textContent = e.message; }
      }
      return undefined;
    },
  });
  return view;
}

// ---------------------------------------------------------------- phase 3: model scores (#/bench)
// Every Holdco Bench run, newest first, and the latest score of each model side by side; #/bench/<id> shows one
// run's scenarios with what failed, and "Run again" tests the same model on the same scenarios.
function Bench(arg) {
  let el, runs = [], status = null, models = [], row = null, onlyFailed = false, polling = false;
  const view = {};
  const id = /^\d+$/.test(arg || '') ? +arg : 0;
  const alive = () => S.view === view && !!el && el.isConnected !== false;
  const dur = (r) => {
    const s = (Date.parse(r.finished_at || '') - Date.parse(r.started_at || '')) / 1000;
    if (!Number.isFinite(s) || s < 0) return '';
    return s < 90 ? `${Math.round(s)} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`;
  };
  const dateOf = (iso) => { const t = new Date(iso || ''); return Number.isNaN(t.getTime()) ? '' : t.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }); };
  const td = (label, html, cls = '') => `<td data-label="${esc(label)}"${cls ? ` class="${cls}"` : ''}><div class="cell">${html}</div></td>`;
  const bar = (v) => (isNum(v) ? `<div class="meter"><div class="bar"><span style="width:${Math.max(0, Math.min(100, Math.round(Number(v) * 100)))}%"></span></div></div>` : '');
  const stateBadge = (r) => (benchRunning(r, status) ? `<span class="badge accent" title="${esc(benchProgress(status))}"><span class="spinner"></span> Running ${fmtCount(Math.min((Number(status.done) || 0) + 1, Number(status.total) || 1))} of ${fmtCount(status.total)}</span>`
    : benchUnfinished(r, status) ? '<span class="badge warn" title="Holdco stopped before this test finished">Did not finish</span>' : '');

  // ---- the list: progress, the latest score per model, every run
  const live = () => {
    const box = $('#bench-live', el);
    if (!box) return;
    // The error of the bench thread belongs to its run: shown on that run's page, or on the list while it is the newest.
    const err = status && status.error && !status.running && (id ? status.run_id === id : !!runs[0] && runs[0].id === status.run_id);
    box.innerHTML = status && status.running ? `<div class="banner"><span class="spinner"></span><div class="grow"><b>Testing ${esc(status.model || 'a model')}.</b> ${esc(benchProgress(status))}.
      Agents pick up no new tasks until it finishes; chat still works.</div></div>` : err ? `<div class="banner warn"><div class="grow"><b>The test stopped with an error.</b> ${esc(status.error)}</div></div>` : '';
    const btn = $('[data-act="bench-new"]', el);
    if (btn) btn.disabled = !!(status && status.running);
    const again = $('[data-act="bench-again"]', el);
    if (again) again.disabled = !!(status && status.running);
  };
  const compare = () => {
    const seen = new Set();
    const latest = runs.filter((r) => r.finished_at && !seen.has(r.model) && seen.add(r.model));
    if (!latest.length) return '<p class="faint" style="margin:0">No finished tests yet. Pick a model above and press Test this model.</p>';
    return `<div class="bench-compare">${latest.map((r) => `<a class="bench-card" href="#/bench/${esc(r.id)}">
      <div class="bench-card-head"><b>${esc(r.model)}</b><span class="small faint">${esc(dateOf(r.started_at))}</span></div>
      <div class="bench-pass"><span class="v">${esc(fmtShare(r.pass_rate))}</span> <span class="small muted">passed (${fmtCount(r.passed)} of ${fmtCount(r.scenarios)})</span></div>${bar(r.pass_rate)}
      <dl class="bench-facts"><div><dt>Tool calls valid</dt><dd>${esc(fmtShare(r.tool_validity))}</dd></div>
        <div><dt>Speed</dt><dd>${isNum(r.tokens_per_second) ? `${Math.round(Number(r.tokens_per_second)).toLocaleString()} tokens/s` : 'n/a'}</dd></div>
        <div><dt>Steps per scenario</dt><dd>${isNum(r.steps_avg) ? esc(Number(r.steps_avg).toFixed(1)) : 'n/a'}</dd></div></dl></a>`).join('')}</div>`;
  };
  const table = () => (runs.length ? `<div class="btable-wrap"><table class="btable bench-table"><thead><tr><th>Model</th><th>Date</th><th>Pass rate</th><th>Tool calls valid</th><th>Tokens/s</th><th>Steps</th></tr></thead><tbody>
    ${runs.map((r) => {
    const done = !!r.finished_at;
    return `<tr class="bench-row" data-act="bench-open" data-id="${esc(r.id)}">
      ${td('Model', `<a href="#/bench/${esc(r.id)}"><b>${esc(r.model)}</b></a>${stateBadge(r) ? ` ${stateBadge(r)}` : ''}`)}
      ${td('Date', esc(dateOf(r.started_at)))}
      ${td('Pass rate', done ? `<span class="num">${esc(fmtShare(r.pass_rate))}</span> <span class="faint small">${fmtCount(r.passed)} of ${fmtCount(r.scenarios)}</span>${bar(r.pass_rate)}` : '<span class="faint">n/a</span>')}
      ${td('Tool calls valid', done ? `<span class="num">${esc(fmtShare(r.tool_validity))}</span>` : '<span class="faint">n/a</span>')}
      ${td('Tokens/s', done && isNum(r.tokens_per_second) ? `<span class="num">${Math.round(Number(r.tokens_per_second)).toLocaleString()}</span>` : '<span class="faint">n/a</span>')}
      ${td('Steps', done && isNum(r.steps_avg) ? `<span class="num">${esc(Number(r.steps_avg).toFixed(1))}</span>` : '<span class="faint">n/a</span>')}</tr>`;
  }).join('')}</tbody></table></div>` : '<p class="faint" style="padding:0 18px 16px;margin:0">Nothing tested yet.</p>');
  const drawList = () => {
    $('#bench-compare', el).innerHTML = compare();
    $('#bench-runs', el).innerHTML = table();
    live();
  };

  // ---- one run: the four scores, notes, and every scenario with its failed assertions
  const scenarioRow = (s) => {
    const failed = Array.isArray(s.failed) ? s.failed : [];
    const what = [...failed.map((f) => `<li><code>${esc(f.assertion)}</code>${f.detail ? `<div class="small muted">${esc(f.detail)}</div>` : ''}</li>`),
      ...(s.error ? [`<li class="warn-text">${esc(s.error)}</li>`] : [])];
    return `<tr class="${s.passed ? '' : 'failed'}">
      ${td('Scenario', `<b>${esc(s.name)}</b>${s.purpose ? `<div class="small muted">${esc(s.purpose)}</div>` : ''}`)}
      ${td('Result', s.passed ? '<span class="badge good">PASS</span>' : '<span class="badge bad">FAIL</span>')}
      ${td('Steps', `<span class="num">${fmtCount(s.steps)}</span>`)}
      ${td('Seconds', `<span class="num">${isNum(s.seconds) ? esc(Number(s.seconds).toFixed(1)) : 'n/a'}</span>`)}
      ${td('What failed', what.length ? `<ul class="bench-fails">${what.join('')}</ul>` : '<span class="faint">nothing</span>')}</tr>`;
  };
  const drawRun = () => {
    const r = row;
    const results = Array.isArray(r.results) ? r.results : [];
    const shown = onlyFailed ? results.filter((s) => !s.passed) : results;
    const notes = benchNotes(r);
    const running = benchRunning(r, status);
    $('#bench-run-head', el).innerHTML = `<div><h1>${esc(r.model)}</h1><p class="sub">Tested ${esc(dateOf(r.started_at))}${r.finished_at ? `, ${plural(Number(r.scenarios) || 0, 'scenario')}${dur(r) ? ` in ${esc(dur(r))}` : ''}` : ''}. ${benchUnfinished(r, status) ? stateBadge(r) : ''}</p></div>
      <div class="actions"><a class="btn" href="#/bench">All model scores</a><button class="btn primary" data-act="bench-again"${status && status.running ? ' disabled' : ''}>Run again</button></div>`;
    $('#bench-run-body', el).innerHTML = running ? '<section class="panel"><div class="panel-body faint">The scores appear here when the test finishes.</div></section>'
      : `${r.finished_at ? `<div class="kpis bench-kpis">
          <div class="kpi"><div class="v">${esc(fmtShare(r.pass_rate))}</div><div class="k">Passed: ${fmtCount(r.passed)} of ${fmtCount(r.scenarios)}</div></div>
          <div class="kpi"><div class="v">${esc(fmtShare(r.tool_validity))}</div><div class="k">Tool calls valid</div></div>
          <div class="kpi"><div class="v">${isNum(r.tokens_per_second) ? Math.round(Number(r.tokens_per_second)).toLocaleString() : 'n/a'}</div><div class="k">Tokens per second</div></div>
          <div class="kpi"><div class="v">${isNum(r.steps_avg) ? esc(Number(r.steps_avg).toFixed(1)) : 'n/a'}</div><div class="k">Steps per scenario</div></div></div>` : ''}
        ${notes ? `<p class="small muted bench-notes">${esc(notes)}</p>` : ''}
        <section class="panel"><div class="panel-head"><h2>Scenarios</h2><label class="check small" style="align-items:center"><input type="checkbox" data-bench-failed${onlyFailed ? ' checked' : ''} style="margin:0"> Only failures</label></div>
          ${shown.length ? `<div class="btable-wrap"><table class="btable bench-table"><thead><tr><th>Scenario</th><th>Result</th><th>Steps</th><th>Seconds</th><th>What failed</th></tr></thead>
            <tbody>${shown.map(scenarioRow).join('')}</tbody></table></div>`
    : `<p class="faint" style="padding:0 18px 16px;margin:0">${results.length ? 'Every scenario passed.' : 'No scenario results were recorded.'}</p>`}</section>`;
    live();
  };

  const load = async () => {
    const [st, list, one] = await Promise.all([GET('/api/bench/status').catch(() => null), id ? null : GET('/api/bench/results?limit=50'), id ? GET(`/api/bench/results/${id}`) : null]);
    if (!alive()) return;
    status = st;
    if (id) { row = one; drawRun(); } else { runs = list.runs || []; drawList(); }
    if (status && status.running && !polling) poll();
  };
  // While a test runs: the progress every 1.5 s, then the finished scores.
  const poll = async () => {
    polling = true;
    try {
      while (alive()) {
        await sleep(1500);
        if (!alive()) break;
        const st = await GET('/api/bench/status').catch(() => null);
        if (!st) continue;
        const was = status;
        status = st;
        if (!st.running) { await load(); break; }
        if (was && was.run_id !== st.run_id) { await load(); continue; }
        live();
        if (!id) $$('.bench-row', el).forEach((tr) => { const r = runs.find((x) => x.id === +tr.dataset.id); const b = r && $('td .cell', tr); if (b) b.innerHTML = `<a href="#/bench/${esc(r.id)}"><b>${esc(r.model)}</b></a>${stateBadge(r) ? ` ${stateBadge(r)}` : ''}`; });
        else if (row) drawRun();
      }
    } finally { polling = false; }
  };
  const start = async (model, only, btn) => {
    if (!model) { toast('Pick a model to test first.', true); return; }
    if (btn) btn.disabled = true;
    try {
      const runId = await benchStart(model, only);
      if (id && runId !== id) { location.hash = `#/bench/${runId}`; return; }
      await load();
    } catch (e) { fail(e); if (btn) btn.disabled = false; }
  };
  Object.assign(view, {
    async mount(holder) {
      el = holder;
      if (id) {
        el.innerHTML = '<div class="page-head" id="bench-run-head"></div><div id="bench-live"></div><div id="bench-run-body"><span class="spinner"></span></div>';
        el.addEventListener('change', (e) => { if (e.target.matches('[data-bench-failed]')) { onlyFailed = e.target.checked; drawRun(); } });
        await load();
        return;
      }
      const [m, settings] = await Promise.all([loadModels(), GET('/api/settings').catch(() => null)]);
      models = m;
      const agentModel = settings ? String(settings.settings.default_model || '') : '';
      const pick = models.length ? `<select id="bench-model" aria-label="Model to test">${models.map((x) => `<option value="${esc(x)}" ${x === agentModel ? 'selected' : ''}>${esc(x)}</option>`).join('')}</select>`
        : `<input type="text" id="bench-model" aria-label="Model to test" placeholder="gpt-oss:20b" value="${esc(agentModel)}">`;
      el.innerHTML = `<div class="page-head"><div><h1>Model scores</h1><p class="sub">Holdco Bench gives a model scripted jobs (founding a company, research, emails, approvals, small sorting jobs) against pretend customers and services, then scores how it did. Test each model you have and pick by score.</p></div>
          <div class="actions bench-new">${pick}<button class="btn primary" data-act="bench-new">Test this model</button></div></div>
        <div id="bench-live"></div>
        <section class="panel" style="margin-bottom:18px"><div class="panel-head wrap"><h2>Side by side</h2><span class="small faint">The latest test of each model</span></div><div class="panel-body" id="bench-compare"><span class="spinner"></span></div></section>
        <section class="panel"><div class="panel-head wrap" style="padding-bottom:6px"><h2>Every test</h2><span class="small faint">Pass rate: scenarios where every check passed. Tool calls valid: model replies that called a tool correctly.</span></div><div id="bench-runs"></div></section>`;
      await load();
    },
    act(name, t) {
      if (name === 'bench-open') { location.hash = `#/bench/${t.dataset.id}`; return; }
      if (name === 'bench-new') { start(String($('#bench-model', el).value || '').trim(), null, t); return; }
      if (name === 'bench-again' && row) {
        const names = (Array.isArray(row.results) ? row.results : []).map((s) => s.name).filter(Boolean);
        start(row.model, names, t);
      }
    },
  });
  return view;
}
const ARCHETYPES = [['lead_generation', 'Lead generation'], ['newsletter', 'Newsletter'], ['saas', 'SaaS'], ['services', 'Services'],
  ['digital_products', 'Digital products'], ['ecommerce', 'E-commerce'], ['local_services', 'Local services']];
const archetypeLabel = (k) => (ARCHETYPES.find(([v]) => v === k) || [k, k])[1];
const SEARCH_SCOPES = [['all', 'Everything'], ['files', 'Files'], ['context', 'Your context'], ['memory', 'Memory'], ['tasks', 'Tasks'],
  ['emails', 'Emails'], ['chat', 'Chat'], ['decisions', 'Decisions'], ['sessions', 'Sessions'], ['facts', 'Facts']];
const SEARCH_GROUPS = { file: 'Files', context: 'Your context', memory: 'Memory', task: 'Tasks', email: 'Emails', chat: 'Chat with you',
  approval: 'Decisions', run: 'Past sessions', fact: 'Verified facts' };
const FOUNDING_STEPS = [['research', 'Research'], ['charter', 'Charter'], ['critic', 'Critic'], ['plan', 'Plan']];
// Search snippets mark the matching words with [brackets]: escape everything first, then turn the brackets into <mark>.
const marked = (s) => esc(s).replace(/\[([^[\]]+)\]/g, '<mark>$1</mark>');
const fmtTokens = (n) => { n = Number(n) || 0; return n >= 1e5 ? `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)}M` : n.toLocaleString(); };
const sizeText = (n) => (n < 1000 ? plural(n, 'byte') : fmtBytes(n));
// "Website: local preview only..." becomes a bold label and the rest, so the card reads like the agents' block.
const factLine = (line) => {
  const s = String(line || '');
  const i = s.indexOf(': ');
  return i > 0 && i < 40 ? `<b>${esc(s.slice(0, i))}</b> ${esc(s.slice(i + 2))}` : esc(s);
};

function foundingSteps(stage) {
  const at = stage === 'done' ? FOUNDING_STEPS.length : Math.max(0, FOUNDING_STEPS.findIndex(([k]) => k === stage));
  return `<ol class="stepper" aria-label="Founding stages">${FOUNDING_STEPS.map(([, label], n) => `<li class="${n < at ? 'done' : n === at ? 'now' : ''}"${n === at ? ' aria-current="step"' : ''}><span class="n">${n < at ? '✓' : n + 1}</span>${label}</li>`).join('')}</ol>`;
}

// Claims and planning events in the feeds (Phase 0.5): a small badge says what Holdco stopped.
const CLAIM_RULES = {
  claims: 'unsupported claim', log_name: 'event-log file name', records: 'records/ is Holdco\'s', context: 'context/ is yours',
  future_date: 'future date', action_task: 'action not done yet', sources: 'numbers without a source', placeholders: 'placeholders on the page',
};
function eventBadge(ev) {
  const d = ev.data || {};
  if (ev.kind === 'claims') {
    if (d.where === 'truth_scan') return '<span class="badge warn">truth check</span> ';
    return `<span class="badge bad" title="${esc(d.problem || '')}">blocked a false claim</span> <span class="badge">${esc(CLAIM_RULES[d.rule] || d.rule || 'claims')}</span> `;
  }
  if (ev.kind === 'question_deduped') return '<span class="badge good">stopped a repeated question</span> ';
  if (ev.kind === 'budget') return '<span class="badge warn">token budget</span> ';
  if (ev.kind === 'founding' && d.stage) {
    const n = FOUNDING_STEPS.findIndex(([k]) => k === d.stage);
    return n >= 0 ? `<span class="badge accent">stage ${n + 1} of 4: ${esc(FOUNDING_STEPS[n][1].toLowerCase())}</span> ` : '';
  }
  return '';
}

// The owner's context library, shared by the company Context tab and the global "Your context" page.
// opts: { base: '/api/context' or '/api/companies/<id>/context', company: bool, initial: path to open, home: hash of the page }
function ContextLibrary(opts) {
  let el, data = null, global = null;
  const base = opts.base;
  const textOk = (name) => !!data && data.formats.some((x) => name.toLowerCase().endsWith(x));
  const pinnedTotal = () => (data ? data.pinned_chars : 0) + (opts.company && global ? global.pinned_chars : 0);
  const drawMeter = () => {
    const cap = data.pinned_cap;
    const total = pinnedTotal();
    const over = total > cap;
    const pct = Math.min(100, Math.round((100 * total) / Math.max(1, cap)));
    const parts = opts.company
      ? `${global && global.pinned.length ? `${plural(global.pinned.length, 'file')} from <a href="#/context">your context</a> (${global.pinned_chars.toLocaleString()}) plus ` : ''}${plural(data.pinned.length, 'file')} here (${data.pinned_chars.toLocaleString()})`
      : `${plural(data.pinned.length, 'file')} (${data.pinned_chars.toLocaleString()})`;
    $('#ctx-meter', el).innerHTML = `<div class="meter${over ? ' over' : ''}">
      <div class="meter-head"><b>Always included</b><span class="small muted num">${total.toLocaleString()} of ${cap.toLocaleString()} characters</span></div>
      <div class="bar" role="meter" aria-valuemin="0" aria-valuemax="${cap}" aria-valuenow="${total}" aria-label="Always-included context size"><span style="width:${pct}%"></span></div>
      <p class="small ${over ? 'warn-text' : 'faint'}" style="margin:0">${parts}. ${over
        ? 'Over the limit: agents get the pinned files in order until it is full and are told which were cut, so they read those with read_file. Unpin a file or shorten it.'
        : opts.company ? 'Pinned files from your context and from this company share one limit in every prompt.' : 'Every company gets these in every prompt, together with its own pinned files, within one limit.'}</p></div>`;
  };
  const drawList = () => {
    const files = data.files;
    const texts = files.filter((f) => f.text);
    const indexed = texts.filter((f) => f.indexed).length;
    $('#ctx-status', el).innerHTML = !data.fts
      ? '<span class="badge warn">Plain-text search</span> Full-text search is not available in this Python, so agents search these files with a slower plain scan.'
      : texts.length ? `<span class="badge ${indexed === texts.length ? 'good' : ''}">${indexed} of ${plural(texts.length, 'text file')} searchable</span>` : '';
    $('#ctx-list', el).innerHTML = files.length ? files.map((f) => {
      const changed = f.modified ? new Date(f.modified * 1000).toISOString() : '';
      return `<li data-path="${esc(f.path)}"><div class="grow">
        <div><button class="linkish" data-act="ctx-open" data-path="${esc(f.path)}"><code>${esc(f.path)}</code></button>
          ${f.text && data.fts ? (f.indexed ? '<span class="badge good">Searchable</span>' : '<span class="badge">Not indexed yet</span>') : ''}
          ${f.pinned ? '<span class="badge accent">Always included</span>' : ''}</div>
        ${f.summary ? `<div class="small muted">${esc(f.summary)}</div>` : ''}
        <div class="small faint">${sizeText(f.size)}${changed ? `, changed ${when(changed)}` : ''}</div>
        ${f.note ? `<div class="small warn-text">${esc(f.note)}</div>` : ''}</div>
        ${f.text ? `<label class="check small ctx-pin"><input type="checkbox" data-pin="${esc(f.path)}" ${f.pinned ? 'checked' : ''}> Always include</label>` : ''}</li>`;
    }).join('') : `<li class="faint">No files yet. ${opts.company ? 'Add customer interviews, notes, price lists or competitor notes for this company.' : 'Add who you are, your skills, budget, preferences, brand rules and things to avoid.'}</li>`;
    $('#ctx-folder', el).innerHTML = `Files live in <code class="small" style="overflow-wrap:anywhere">${esc(data.folder)}</code>; you can also copy them there.`;
  };
  const draw = () => { drawMeter(); drawList(); };
  const load = async () => {
    const [d, g] = await Promise.all([GET(base), opts.company ? GET('/api/context').catch(() => null) : null]);
    data = d;
    global = g;
    if (el) draw();
  };
  const loadSoon = debounce(() => load().catch(() => {}), 600);
  const hashFor = (path) => `${opts.home}/${encodeURIComponent(path)}`;
  const readText = (file) => new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result || ''));
    r.onerror = () => reject(r.error || new Error(`Could not read ${file.name}`));
    r.readAsText(file);
  });
  const addFiles = async (list) => {
    const files = [...(list || [])];
    if (!files.length || !data) return;
    let added = 0;
    for (const f of files) {
      if (!textOk(f.name)) { toast(`${f.name}: only text files go in the library (${data.formats.join(', ')}). Convert it to text first.`, true); continue; }
      if (f.size > data.max_bytes) { toast(`${f.name} is too big: ${Math.round(data.max_bytes / 1000)} KB at most.`, true); continue; }
      try {
        await api('PUT', `${base}/file`, { path: f.name, content: await readText(f) });
        added++;
      } catch (e) { toast(`${f.name}: ${e.message}`, true); }
    }
    if (added) toast(`${plural(added, 'file')} added. Agents can search ${added === 1 ? 'it' : 'them'} now.`);
    await load();
  };
  const setPinned = async (path, on, box) => {
    const want = on ? [...data.pinned.filter((p) => p !== path), path] : data.pinned.filter((p) => p !== path);
    try {
      const r = await api('PUT', `${base}/pinned`, { paths: want });
      Object.assign(data, { pinned: r.pinned, pinned_chars: r.pinned_chars, pinned_cap: r.pinned_cap });
      data.files.forEach((f) => { f.pinned = r.pinned.includes(f.path); });
      draw();
      toast(on ? `${path} goes into every prompt now.` : `${path} is no longer in every prompt; agents still find it with search.`);
    } catch (e) { fail(e); if (box) box.checked = !on; }
  };
  const open = async (path) => {
    let f;
    try { f = await GET(`${base}/file?path=${encodeURIComponent(path)}`); } catch (e) { fail(e); return; }
    const url = hashFor(path);
    history.replaceState(null, '', url);
    modal({
      title: path,
      wide: true,
      body: f.binary
        ? `<p class="muted">This file is not text (${sizeText(f.size)}), so agents cannot read or search it. Convert it to ${esc(data.formats.join(', '))} and add that instead.</p>`
        : `<form data-form="ctx-save" id="ctx-save" data-path="${esc(path)}"><textarea name="content" class="code ctx-edit" aria-label="Contents of ${esc(path)}">${esc(f.content)}</textarea></form>
          <p class="small faint" style="margin:8px 0 0">${sizeText(f.size)}. ${f.pinned ? 'Always included in every prompt.' : 'Agents find it with search.'}</p>`,
      foot: `<button class="btn danger" data-act="ctx-delete" data-path="${esc(path)}" style="margin-right:auto">Delete</button><button class="btn" data-act="close-modal">Close</button>${f.binary ? '' : '<button class="btn primary" type="submit" form="ctx-save">Save</button>'}`,
      onClose: () => { if (location.hash === url) history.replaceState(null, '', opts.home); },
    });
  };
  return {
    async mount(holder) {
      el = holder;
      el.innerHTML = `<section class="panel ctx-lib" id="ctx-lib">
        <div class="panel-body">
          <p style="margin-bottom:4px"><b>Your agents read these. Mark a file 'Always include' to put it in every prompt; everything else they find with search.</b></p>
          <p class="muted small">${opts.company ? 'Files for this company only: customer interviews, notes, price lists, competitor notes. Agents can read them but never change them.'
            : 'These apply to every company: who you are, your skills, budget, preferences, brand rules and things to avoid. Agents can read them but never change them.'}</p>
          <div id="ctx-meter"></div>
          <div class="actions" style="margin-top:14px"><button class="btn primary" data-act="ctx-new">New file</button>
            <label class="btn" style="cursor:pointer">Add files<input type="file" multiple hidden data-ctx-upload></label>
            <span id="ctx-status" class="small muted"></span></div>
          <div class="dropzone" id="ctx-drop">Drop text files here (.md, .txt, .csv, .json, .html, .xml, .yaml), up to 500 KB each. A file with the same name is replaced.</div>
        </div>
        <ul class="rows" id="ctx-list"><li class="faint"><span class="spinner"></span> Loading…</li></ul>
        <p class="small faint" id="ctx-folder" style="padding:0 18px 14px;margin:0"></p></section>`;
      const panel = $('#ctx-lib', el);
      const zone = $('#ctx-drop', el);
      const over = (on) => zone.classList.toggle('over', on);
      panel.addEventListener('dragover', (e) => { e.preventDefault(); over(true); });
      panel.addEventListener('dragleave', (e) => { if (!panel.contains(e.relatedTarget)) over(false); });
      panel.addEventListener('drop', (e) => { e.preventDefault(); over(false); addFiles(e.dataTransfer && e.dataTransfer.files).catch(fail); });
      el.addEventListener('change', (e) => {
        const box = e.target.closest('[data-pin]');
        if (box) { setPinned(box.dataset.pin, box.checked, box); return; }
        if (e.target.matches('[data-ctx-upload]')) { addFiles(e.target.files).catch(fail).finally(() => { e.target.value = ''; }); }
      });
      await load();
      const upload = $('[data-ctx-upload]', el);
      if (upload) upload.accept = data.formats.join(',');
      if (opts.initial) open(opts.initial);
    },
    live(ev) {
      if (ev.type === 'context') { loadSoon(); return; }
      if (opts.company && ev.type === 'event' && ev.kind === 'file' && String(ev.data?.path || '').startsWith('context/')) loadSoon();
    },
    async act(name, t) {
      if (name === 'ctx-open') { await open(t.dataset.path); return true; }
      if (name === 'ctx-new') {
        modal({
          title: 'New context file',
          wide: true,
          body: `<form class="form-stack" data-form="ctx-new" id="ctx-new">
            <label class="field">File name<span class="hint">Folders are fine, for example brand/voice.md. Text formats only: ${esc(data.formats.join(', '))}.</span><input type="text" name="path" required maxlength="200" placeholder="${opts.company ? 'interviews/first-calls.md' : 'about-me.md'}"></label>
            <label class="field">Contents<textarea name="content" class="code" rows="14" placeholder="${opts.company ? 'Notes from calls with customers, prices you have seen, what competitors do.' : 'Who you are, what you are good at, your budget and hours, brand rules, what to avoid.'}"></textarea></label>
            <label class="check small"><input type="checkbox" name="pin"> Always include it in every prompt</label></form>`,
          foot: '<button class="btn" data-act="close-modal">Cancel</button><button class="btn primary" type="submit" form="ctx-new">Save file</button>',
        });
        return true;
      }
      if (name === 'ctx-delete') {
        const path = t.dataset.path;
        modal({
          title: `Delete ${path}?`,
          body: `<p>It moves to the trash folder in your data directory, and agents stop seeing it at once.${data.pinned.includes(path) ? ' It is also taken out of every prompt.' : ''}</p>`,
          foot: `<button class="btn" data-act="close-modal">Cancel</button><button class="btn danger" data-act="ctx-delete-yes" data-path="${esc(path)}">Delete</button>`,
          onClose: () => { if (location.hash === hashFor(path)) history.replaceState(null, '', opts.home); },
        });
        return true;
      }
      if (name === 'ctx-delete-yes') {
        t.disabled = true;
        try { await api('DELETE', `${base}/file`, { path: t.dataset.path }); closeModal(); toast('Deleted. It is in the trash folder if you need it back.'); await load(); } catch (e) { fail(e); t.disabled = false; }
        return true;
      }
      return false;
    },
    async submit(name, form) {
      if (name === 'ctx-save') {
        try {
          await api('PUT', `${base}/file`, { path: form.dataset.path, content: form.content.value });
          closeModal();
          toast('Saved. Agents see it in their next session.');
          await load();
        } catch (e) { fail(e); }
        return true;
      }
      if (name === 'ctx-new') {
        let path = form.path.value.trim().replace(/\\/g, '/').replace(/^\/+/, '');
        if (path && !/\.[^/.]+$/.test(path)) path += '.md';
        if (!textOk(path)) { toast(`Use a text format: ${data.formats.join(', ')}.`, true); return true; }
        try {
          const r = await api('PUT', `${base}/file`, { path, content: form.content.value });
          if (form.pin.checked) await api('PUT', `${base}/pinned`, { paths: [...data.pinned.filter((p) => p !== r.path), r.path] });
          closeModal();
          toast(`Saved ${r.path}.`);
          await load();
        } catch (e) { fail(e); }
        return true;
      }
      return false;
    },
  };
}

// The global "Your context" page (#/context, #/context/<path> opens a file).
function YourContext(initial) {
  const lib = ContextLibrary({ base: '/api/context', company: false, initial, home: '#/context' });
  return {
    async mount(holder) {
      holder.innerHTML = `<div class="page-head"><div><h1>Your context</h1><p class="sub">What every company's agents should know about you. Each company also has its own Context tab.</p></div></div><div id="ctx-holder"></div>`;
      await lib.mount($('#ctx-holder', holder));
    },
    live: (ev) => lib.live(ev),
    act: (name, t) => lib.act(name, t),
    submit: (name, form) => lib.submit(name, form),
  };
}

// ---- search: the same search the agents use, from the company header
function searchHitLink(cid, h) {
  const go = (href, label) => `<a href="${esc(href)}" data-act="search-go">${label}</a>`;
  const loc = (p) => `<code>${esc(p)}${h.line ? `:${esc(h.line)}` : ''}</code>`;
  switch (h.source) {
    case 'file': return go(`#/c/${cid}/files/${encodeURIComponent(h.path)}`, loc(h.path));
    case 'context': {
      const m = /^(global|context):(.*)$/.exec(h.path || '');
      if (!m) return loc(h.path);
      return go(m[1] === 'global' ? `#/context/${encodeURIComponent(m[2])}` : `#/c/${cid}/context/${encodeURIComponent(m[2])}`, loc(h.path));
    }
    case 'task': return go(`#/c/${cid}/tasks`, esc(h.ref));
    case 'email': return go(`#/c/${cid}/email`, esc(h.ref));
    case 'memory': return go(`#/c/${cid}/memory`, esc(h.ref));
    case 'chat': return go(`#/c/${cid}/chat`, esc(h.ref));
    case 'approval': return go('#/inbox', esc(h.ref));
    case 'run': return `<button class="linkish" data-act="search-run" data-id="${esc(h.id)}">${esc(h.ref)}</button>`;
    default: return esc(h.ref || h.path);
  }
}

async function companySearch(cid, q, scope) {
  if (!q.trim()) return;
  const r = await GET(`/api/companies/${cid}/search?q=${encodeURIComponent(q)}&scope=${encodeURIComponent(scope || 'all')}&limit=20`);
  const order = [];
  r.results.forEach((h) => { if (!order.includes(h.source)) order.push(h.source); });
  const where = scope && scope !== 'all' ? ` in ${(SEARCH_SCOPES.find(([k]) => k === scope) || [scope, scope])[1].toLowerCase()}` : '';
  modal({
    title: `Search: ${q}`,
    wide: true,
    body: `<p class="muted small" style="margin:0 0 6px">${r.results.length ? `${plural(r.results.length, 'result')}${where}, best first. This is the same search your agents use.` : `Nothing found${where}. Try fewer or other words, or another place to search.`}
      ${r.fts ? '' : ' <span class="badge warn">Plain-text search</span>'}</p>
      ${order.map((src) => `<section class="search-group"><h3>${esc(SEARCH_GROUPS[src] || src)}</h3><ul class="rows search-hits">${r.results.filter((h) => h.source === src).map((h) => `
        <li><div class="grow"><div>${searchHitLink(cid, h)}${h.heading ? ` <span class="muted small">${esc(h.heading)}</span>` : ''}</div>
          <div class="small snippet">${marked(h.snippet)}</div></div></li>`).join('')}</ul></section>`).join('')}`,
  });
}

// ---------------------------------------------------------------- company
const TABS = [['overview', 'Overview'], ['chat', 'Chat'], ['tasks', 'Tasks'], ['brain', 'Brain'], ['team', 'Team'], ['mind', 'Thinking'], ['files', 'Files'], ['context', 'Context'], ['memory', 'Memory'], ['email', 'Email'], ['charter', 'Charter'], ['settings', 'Settings']];

function CompanyView(cid, tab, arg) {
  let el, d, tabView;
  // The header is redrawn in parts (title, buttons, tabs) so the search box keeps what the owner is typing.
  const title = () => {
    const c = d.company;
    const status = { active: ['good', 'Running'], founding: ['accent', 'Founding'], paused: ['warn', 'Paused'], archived: ['', 'Archived'] }[c.status] || ['', c.status];
    const url = c.settings.site_url;
    return `<h1>${esc(c.name)}</h1><p class="one">${esc(c.mission || c.idea.split('\n')[0])}</p>
        <div class="meta"><span class="badge ${status[0]}">${status[1]}</span><span class="badge">Stage: ${esc(c.stage)}</span>
        ${url ? `<a class="small" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(url.replace(/^https?:\/\//, ''))}</a>` : ''}</div>`;
  };
  const buttons = () => {
    const c = d.company;
    return `${d.has_site ? `<a class="btn" href="/preview/${esc(c.slug)}/" target="_blank" rel="noopener">Preview website</a>` : ''}
        ${c.status === 'active' ? `<button class="btn" data-act="plan">Plan now</button><button class="btn" data-act="status" data-s="paused">Pause</button>` : ''}
        ${c.status === 'paused' || c.status === 'archived' ? `<button class="btn primary" data-act="status" data-s="active">Resume</button>` : ''}`;
  };
  const tabs = () => TABS.map(([k, l]) => `<a href="#/c/${cid}/${k}" class="${k === tab ? 'on' : ''}">${l}${k === 'overview' && d.stats.approvals ? ` <span class="count">${d.stats.approvals}</span>` : ''}</a>`).join('');
  const head = () => `<div class="co-head"><div id="co-title">${title()}</div>
      <div class="co-side"><form class="co-search" data-form="co-search" role="search">
          <input type="search" name="q" placeholder="Search files, context, tasks, email…" aria-label="Search everything this company knows" maxlength="200">
          <select name="scope" aria-label="Where to search">${SEARCH_SCOPES.map(([k, l]) => `<option value="${k}">${esc(l)}</option>`).join('')}</select>
          <button class="btn" type="submit">Search</button></form>
        <div class="actions" id="co-actions">${buttons()}</div></div></div>
      <nav class="tabs" aria-label="Company" id="co-tabs">${tabs()}</nav>`;
  const reload = async () => {
    d = await GET(`/api/companies/${cid}`);
    d.agentsById = Object.fromEntries(d.agents.map((a) => [a.id, a]));
  };
  const redrawHead = async () => {
    await reload();
    if (!$('#co-title', el)) return;
    $('#co-title', el).innerHTML = title();
    $('#co-actions', el).innerHTML = buttons();
    $('#co-tabs', el).innerHTML = tabs();
  };
  const refreshHeadSoon = debounce(() => redrawHead().then(() => tabView?.onCompany?.()).catch(() => {}), 700);

  return {
    async mount(holder) {
      el = holder;
      await reload();
      el.innerHTML = `<div id="co-head">${head()}</div><div id="tab"></div>`;
      const factory = { overview: TabOverview, chat: TabChat, tasks: TabTasks, brain: TabBrain, team: TabTeam, mind: TabMind, files: TabFiles, context: TabContext, memory: TabMemory, email: TabEmail, charter: TabCharter, settings: TabSettings }[tab] || TabOverview;
      tabView = factory(cid, () => d, arg, async () => { await redrawHead(); tabView?.onCompany?.(); });
      await tabView.mount($('#tab', el));
    },
    live(ev) {
      if (ev.company_id && ev.company_id !== cid) return;
      if (['event', 'agents', 'company', 'approvals', 'tasks'].includes(ev.type)) refreshHeadSoon();
      tabView?.live?.(ev);
    },
    async act(name, t, e) {
      if (name === 'status') {
        try { await api('PATCH', `/api/companies/${cid}`, { status: t.dataset.s }); await redrawHead(); refreshState(); tabView?.onCompany?.(); } catch (err) { fail(err); }
        return;
      }
      if (name === 'plan') {
        try { const r = await api('POST', `/api/companies/${cid}/cycle`); toast(r.message); } catch (err) { fail(err); }
        return;
      }
      if (name === 'search-go') {   // a search hit: close the results and go there (the same place only closes them)
        const href = t.getAttribute('href');
        closeModal();
        if (location.hash !== href) location.hash = href;
        return;
      }
      if (name === 'search-run') { showTranscript(+t.dataset.id).catch(fail); return; }
      if (tabView?.act) return tabView.act(name, t, e);
    },
    submit(name, form, e) {
      if (name === 'co-search') {
        const btn = $('button[type=submit]', form);
        btn.disabled = true;
        companySearch(cid, form.q.value, form.scope.value).catch(fail).finally(() => { btn.disabled = false; });
        return undefined;
      }
      return tabView?.submit?.(name, form, e);
    },
  };
}

// ---- overview
// Metric tiles (Phase 1): numbers a connector read (source = its key) come first and say where they came from;
// numbers an agent typed with record_metric (source manual) say so.
function metricTiles(metrics) {
  const polled = (m) => !!m.source && m.source !== 'manual';
  return [...metrics].sort((a, b) => (polled(b) - polled(a)) || String(a.name).localeCompare(String(b.name))).map((m) => {
    const src = polled(m) ? connectorLabel(m.source) : '';
    const name = String(m.name || '');
    const short = src && name.startsWith(`${m.source}.`) ? name.slice(m.source.length + 1) : name;
    const title = [m.note, src ? `Read from ${src}${m.created_at ? ` ${new Date(m.created_at).toLocaleString()}` : ''}` : 'Typed in by an agent (record_metric); Holdco did not check it']
      .filter(Boolean).join('. ');
    return `<div class="kpi${src ? ' polled' : ''}" title="${esc(title)}"><div class="v">${esc(fmtValue(m.value))}</div>
      <div class="k">${esc(short.replace(/[._]/g, ' '))}</div><div class="src">${src ? `from ${esc(src)}` : 'typed by an agent'}</div></div>`;
  }).join('');
}

// Phase 2 rhythms the owner can start now (POST /rhythms/<key>/run creates the routine task at once).
const RHYTHMS = [['morning', 'Morning plan'], ['evening', 'Evening brief'], ['weekly', 'Weekly review'], ['monthly', 'Monthly check']];

function TabOverview(cid, data) {
  let el, facts = null, plan = null, briefs = null, brainEmpty = false;
  const side = () => {
    const d = data();
    const c = d.company;
    const ceo = d.agents.find((a) => a.role === 'ceo');
    let banner = '';
    if (c.status === 'founding') {
      const steps = plan && plan.founding_stage ? foundingSteps(plan.founding_stage) : '';
      banner = ceo && ceo.status === 'working'
        ? `<div class="banner"><span class="spinner"></span><div class="grow"><b>${esc(ceo.name)} is founding the company.</b> Researching the market, writing a charter, a skeptical review and the first tasks, each as a short session. Follow along in the feed.${steps}</div></div>`
        : `<div class="banner bad"><div class="grow"><b>Founding stopped before it finished.</b> Usually the model server wasn't reachable. Check Settings, then try again.${steps}</div><button class="btn" data-act="refound">Retry founding</button></div>`;
    } else if (c.status === 'paused') {
      banner = `<div class="banner warn"><div class="grow"><b>Paused.</b> Agents finish their current step and then wait. Chat still works.</div><button class="btn primary" data-act="status" data-s="active">Resume</button></div>`;
    }
    if (brainEmpty && c.status === 'active') banner += `<div class="banner"><div class="grow"><b>Brain not written yet;</b> the CEO fills it in the next planning cycle. Until then agents read the charter.</div><a class="btn" href="#/c/${cid}/brain">Open the Brain</a></div>`;
    if (d.stats.approvals) banner += `<div class="banner warn"><div class="grow"><b>${plural(d.stats.approvals, 'request')} from ${esc(c.name)} ${d.stats.approvals === 1 ? 'is' : 'are'} waiting for you.</b></div><a class="btn" href="#/inbox">Review</a></div>`;
    $('#ov-banner', el).innerHTML = banner;
    $('#ov-team', el).innerHTML = d.agents.map((a) => `<li>${avatar(a)}<div class="info"><div><b>${esc(a.name)}</b> <span class="role">${esc(a.title)}</span></div>
      <div class="doing">${!a.enabled ? 'Off' : a.status === 'working' ? esc(a.status_note || 'Working') : a.status === 'error' ? `<span style="color:var(--bad)">${esc(a.status_note || 'Error')}</span>` : 'Idle'}</div></div></li>`).join('');
    $('#ov-stats', el).innerHTML = `
      <div class="kpi"><div class="v">${d.stats.tasks_open}</div><div class="k">Open tasks</div></div>
      <div class="kpi"><div class="v">${d.stats.tasks_done}</div><div class="k">Tasks done</div></div>
      <div class="kpi"><div class="v">${d.stats.done_today}</div><div class="k">Done today</div></div>
      <div class="kpi"><div class="v">${((Number(c.tokens_used) || 0) / 1e6).toFixed(c.tokens_used > 1e7 ? 0 : 2)}M</div><div class="k">Tokens used</div></div>
      ${metricTiles(d.metrics || [])}`;
    $('#ov-folder', el).innerHTML = `<p class="small muted" style="margin:0 0 6px">Everything the team makes is a normal file on your PC:</p><code class="small" style="overflow-wrap:anywhere">${esc(d.folder)}</code>`;
  };
  // "What your agents know": the same facts block every agent gets in its prompt (GET /facts).
  const drawFacts = () => {
    const box = $('#ov-know', el);
    if (!box || !facts) return;
    const f = facts;
    const waiting = f.waiting.length
      ? `<li><b>Waiting on you</b> ${plural(f.waiting.length, 'item')}:<ul>${f.waiting.map((w) => `<li><a href="#/inbox">#${esc(w.id)}</a> ${esc(w.label)}: ${esc(w.summary)} <span class="faint small">${esc(w.age)}${w.asked_by ? `, from ${esc(w.asked_by)}` : ''}</span></li>`).join('')}</ul></li>`
      : '<li><b>Waiting on you</b> nothing.</li>';
    const accounts = f.accounts.length ? f.accounts.map((a) => esc(a.label)).join(', ') : 'none besides the local website';
    const down = f.down.length
      ? `<li class="warn-line"><b>Down right now</b> ${f.down.map((x) => `${esc(x.label)} (${esc(x.detail)})`).join('; ')}</li>`
      : '<li class="faint"><b>Down right now</b> nothing Holdco knows of.</li>';
    box.innerHTML = `<ul class="know">
        <li>${factLine(f.website.line)}</li><li>${factLine(f.email.line)}</li><li>${factLine(f.social.line)}</li>
        <li>${factLine(f.money.line)}</li>${waiting}<li><b>Accounts</b> ${accounts}</li>${down}<li>${factLine(f.signals.line)}</li></ul>
      <details class="raw"><summary class="small muted">Exactly what the agents read</summary><pre class="small">${esc(f.text)}</pre></details>`;
  };
  // Planning effort (GET /planning): the next look, what waits on the owner, the daily token budget.
  const drawPlan = () => {
    const box = $('#ov-plan', el);
    if (!box || !plan) return;
    const c = data().company;
    const b = plan.budget || {};
    const gpu = data().budgets || null;   // Phase 2: GPU hours used today against the company's budget
    const lines = [];
    let alert = '';
    if (b.over) alert += `<div class="banner warn"><div class="grow"><b>Paused for today: daily token budget used.</b> ${fmtTokens(b.used)} of ${fmtTokens(b.budget)} tokens. Background work waits until midnight; chat and tasks you create still run.</div><a class="btn" href="#/c/${cid}/settings">Change the budget</a></div>`;
    if (gpu && gpu.over_gpu_budget) alert += `<div class="banner warn"><div class="grow"><b>Paused for today: GPU budget used.</b> ${fmtHours(gpu.gpu_hours_today)} of ${fmtHours(gpu.gpu_hours_per_day, 2)} hours of model time. Agents pick up no new work until midnight UTC; chat and tasks you create still run.</div><a class="btn" href="#/c/${cid}/settings/budgets">Change the budget</a></div>`;
    $('#ov-plan-banner', el).innerHTML = alert;
    if (c.status === 'founding') {
      lines.push(`<li><b>Founding</b> ${plan.founding_stage === 'done' ? 'finished' : `stage ${Math.max(1, FOUNDING_STEPS.findIndex(([k]) => k === plan.founding_stage) + 1)} of 4`}${foundingSteps(plan.founding_stage)}</li>`);
    } else if (c.status === 'active') {
      const mins = Math.ceil(plan.next_due_minutes || 0);
      const w = plan.waiting;
      let next = b.over ? 'paused for today: daily token budget used'
        : w ? `waiting on you: ${w.what}${w.oldest_age ? `; the oldest has waited ${w.oldest_age}` : ''}. Planning is paused until something changes`
          : mins <= 0 ? 'due now' : `next planning in ${plural(mins, 'minute')}`;
      if (!b.over && !w && plan.cycle_backoff) next += ` (the interval doubled ${plural(plan.cycle_backoff, 'time')} because nothing changed)`;
      if (!b.over && !w && plan.changed) next += '. Something changed since the last plan';
      lines.push(`<li><b>Planning</b> ${esc(next)}.${w && !b.over ? ' <a class="small" href="#/inbox">Open Inbox</a>' : ''}</li>`);
    } else {
      lines.push(`<li><b>Planning</b> stopped while the company is ${esc(c.status)}.</li>`);
    }
    const pct = b.budget ? Math.min(100, Math.round((100 * b.used) / b.budget)) : 0;
    lines.push(`<li><b>Tokens today</b> ${fmtTokens(b.used)}${b.budget ? ` of ${fmtTokens(b.budget)}` : ' (no daily limit)'}${b.budget ? `<div class="meter${b.over ? ' over' : ''}"><div class="bar"><span style="width:${pct}%"></span></div></div>` : ''}</li>`);
    if (gpu && gpu.gpu_hours_per_day) {
      const gpct = Math.min(100, Math.round((100 * gpu.gpu_hours_today) / gpu.gpu_hours_per_day));
      lines.push(`<li><b>GPU budget used</b> ${fmtHours(gpu.gpu_hours_today)} of ${fmtHours(gpu.gpu_hours_per_day, 2)} h <a class="small" href="#/c/${cid}/settings/budgets">Change</a>
        <div class="meter${gpu.over_gpu_budget ? ' over' : ''}"><div class="bar" role="meter" aria-valuemin="0" aria-valuemax="${esc(gpu.gpu_hours_per_day)}" aria-valuenow="${esc(gpu.gpu_hours_today)}" aria-label="GPU hours used today"><span style="width:${gpct}%"></span></div></div></li>`);
    }
    lines.push(`<li><b>Playbook</b> ${plan.archetype ? esc(archetypeLabel(plan.archetype)) : 'none matched yet'} <a class="small" href="#/c/${cid}/settings">Change</a></li>`);
    box.innerHTML = `<ul class="know">${lines.join('')}</ul>`;
    side();
  };
  const loadKnow = async () => {
    const [f, pl] = await Promise.all([GET(`/api/companies/${cid}/facts`).catch(() => null), GET(`/api/companies/${cid}/planning`).catch(() => null)]);
    if (!el) return;
    if (f) { facts = f; drawFacts(); }
    if (pl) { plan = pl.planning; drawPlan(); }
  };
  const loadKnowSoon = debounce(() => loadKnow().catch(() => {}), 1500);
  // Phase 2: the latest daily brief, links to the weekly memo and the monthly check, and "Run now" for each rhythm.
  const drawBrief = () => {
    const box = $('#ov-brief', el);
    if (!box || !briefs) return;
    const b = briefs.daily;
    const links = ['weekly', 'monthly'].filter((k) => briefs[k])
      .map((k) => `<button class="linkish small" data-act="brief" data-kind="${k}">${BRIEF_LABEL[k]}</button>`).join(' <span class="faint">·</span> ');
    $('#ov-brief-meta', el).innerHTML = `${b ? `<span class="small faint">${when(b.created_at)}</span>` : ''}${links ? ` <span class="brief-links">${links}</span>` : ''}`;
    const c = data().company;
    box.innerHTML = (b ? briefBody(b) : `<p class="faint" style="margin:0">No daily brief yet. ${c.status === 'founding' ? 'The first one comes after founding.' : 'One is written every evening from Holdco\'s own numbers; press Evening brief below to get one now.'}</p>`)
      + (c.status === 'founding' ? '' : `<div class="rhythms"><span class="small muted">Run now:</span>${RHYTHMS.map(([k, l]) => `<button class="btn sm" data-act="rhythm" data-key="${k}">${l}</button>`).join('')}</div>`);
  };
  const loadBriefs = async () => {
    const [r, br] = await Promise.all([GET(`/api/briefs/latest?company_id=${cid}`).catch(() => null), GET(`/api/companies/${cid}/brain`).catch(() => null)]);
    if (!el) return;
    briefs = r || briefs || {};
    if (br) brainEmpty = !!br.empty;
    drawBrief();
    side();
  };
  const loadBriefsSoon = debounce(() => loadBriefs().catch(() => {}), 1000);
  return {
    async mount(holder) {
      el = holder;
      el.innerHTML = `<div id="ov-banner"></div><div id="ov-plan-banner"></div><div class="cols">
        <div class="stack"><section class="panel"><div class="panel-head"><h2>Daily brief</h2><span id="ov-brief-meta"></span></div><div class="panel-body" id="ov-brief"><span class="spinner"></span></div></section>
          <section class="panel"><div class="panel-head"><h2>Live</h2></div><div id="ov-feed"></div></section></div>
        <div class="stack"><section class="panel"><div class="panel-head"><h2>What your agents know</h2><span class="small faint">From Holdco's records</span></div><div class="panel-body" id="ov-know"><span class="spinner"></span></div></section>
          <section class="panel"><div class="panel-head"><h2>Planning</h2></div><div class="panel-body" id="ov-plan"><span class="spinner"></span></div></section>
          <section class="panel"><div class="panel-head"><h2>Team</h2><a class="small" href="#/c/${cid}/team">Manage</a></div><ul class="team-list" id="ov-team" style="padding-top:6px"></ul></section>
          <section class="panel"><div class="panel-head"><h2>Numbers</h2></div><div class="kpis" id="ov-stats" style="padding-top:14px"></div></section>
          <section class="panel"><div class="panel-body" id="ov-folder"></div></section></div></div>`;
      mountFeed($('#ov-feed', el), cid);
      side();
      // Connector labels for the metric tiles ("from Stripe"); the key stands in until they arrive.
      if ((data().metrics || []).some((m) => m.source && m.source !== 'manual') && !S.connectors) loadConnectors().then(() => { if (el) side(); }).catch(() => {});
      await Promise.all([loadKnow(), loadBriefs()]);
    },
    live(ev) {
      if (['approvals', 'company', 'tasks'].includes(ev.type) || (ev.type === 'event' && !['think', 'result'].includes(ev.kind))) loadKnowSoon();
      if (ev.type === 'briefs' || (ev.type === 'event' && (ev.kind === 'brief' || ev.kind === 'brain'))) loadBriefsSoon();
    },
    onCompany: () => { side(); drawPlan(); drawBrief(); },
    async act(name, t) {
      if (name === 'refound') {
        try { await api('POST', `/api/companies/${cid}/refound`); toast('Founding restarted.'); } catch (e) { fail(e); }
      }
      if (name === 'brief' && briefs && briefs[t.dataset.kind]) showBrief(briefs[t.dataset.kind]);
      if (name === 'rhythm') {
        t.disabled = true;
        try {
          const r = await api('POST', `/api/companies/${cid}/rhythms/${encodeURIComponent(t.dataset.key)}/run`, {});
          const who = (data().agentsById[r.task.agent_id] || {}).name || 'the team';
          toast(r.created ? `${r.task.title} queued for ${who} as task #${r.task.id}.` : `${r.task.title} is already queued as task #${r.task.id}.`);
        } catch (e) { fail(e); }
        t.disabled = false;
      }
    },
  };
}

// ---- chat
function TabChat(cid, data) {
  let el, busy = [];
  const draw = (msgs) => {
    const d = data();
    const th = $('.thread', el);
    const atBottom = th.scrollHeight - th.scrollTop - th.clientHeight < 80;
    th.innerHTML = msgs.length ? msgs.map((m) => {
      const me = m.sender === 'owner';
      const a = d.agentsById[m.agent_id] || { name: m.agent_name || 'Team', role: m.agent_role };
      return `<div class="bubble-row ${me ? 'me' : ''}">${me ? '<span class="av owner">Y</span>' : avatar(a)}
        <div class="bubble"><div class="who">${me ? `You to ${esc(a.name)}` : esc(a.name)}, ${when(m.created_at)}</div>${md(m.content)}</div></div>`;
    }).join('') : `<div class="faint" style="padding:10px">Talk to your CEO like you would a cofounder: ask for status, change priorities, hand over context. Agents act on it.</div>`;
    const target = +$('select[name=agent_id]', el).value;
    const who = d.agentsById[target];
    $('.typing', el).textContent = busy.includes(target) && who ? `${who.name} is working…` : '';
    if (atBottom || !loaded) th.scrollTop = th.scrollHeight;
    loaded = true;
  };
  let loaded = false;
  const load = async () => { const r = await GET(`/api/companies/${cid}/messages`); busy = r.busy; draw(r.messages); };
  const loadSoon = debounce(() => load().catch(() => {}), 300);
  return {
    async mount(holder) {
      el = holder;
      const d = data();
      const ceo = d.agents.find((a) => a.role === 'ceo');
      el.innerHTML = `<section class="panel chat"><div class="thread"></div><div class="typing"></div>
        <form class="composer" data-form="chat">
          <select name="agent_id" aria-label="Send to">${d.agents.map((a) => `<option value="${a.id}" ${a.id === ceo?.id ? 'selected' : ''}>${esc(a.name)} (${esc(a.role)})</option>`).join('')}</select>
          <textarea name="content" rows="1" placeholder="Message your team…" aria-label="Message"></textarea>
          <button class="btn primary" type="submit">Send</button></form></section>`;
      const ta = $('textarea', el);
      ta.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); $('form', el).requestSubmit(); } });
      ta.addEventListener('input', () => { ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight, 200) + 'px'; });
      $('select', el).addEventListener('change', () => loadSoon());
      await load();
      ta.focus();
    },
    live(ev) { if (['messages', 'agents'].includes(ev.type)) loadSoon(); },
    async submit(name, form) {
      const content = form.content.value.trim();
      if (!content) return;
      form.content.value = '';
      form.content.style.height = 'auto';
      try {
        const r = await api('POST', `/api/companies/${cid}/messages`, { content, agent_id: +form.agent_id.value });
        if (r.queued) toast(`${r.agent} is in the middle of a task and will reply right after.`);
        await load();
      } catch (e) { fail(e); form.content.value = content; }
    },
  };
}

// ---- tasks
const LANES = [['todo', 'To do', ['todo']], ['in_progress', 'In progress', ['in_progress']], ['waiting', 'Waiting or blocked', ['waiting', 'blocked']], ['done', 'Done', ['done', 'cancelled']]];

// Phase 2 contract badges on a card: the goal it serves (its title on hover), "waiting on #12" (blocked_by),
// "due Oct 30" (red once overdue) and, for open tasks an agent created without a goal, "No goal".
function taskTags(t) {
  const open = OPEN_STATUSES.includes(t.status);
  const tags = [];
  if (t.goal_id) tags.push(`<span class="badge accent" title="${esc(`Serves goal #${t.goal_id}${t.goal_title ? `: ${t.goal_title}` : ''}`)}">Goal #${esc(t.goal_id)}</span>`);
  else if (open && !t.routine && !EXEMPT_CREATORS.includes(t.created_by || '')) {
    tags.push('<span class="badge nogoal" title="An agent created this task without naming the goal it serves. The weekly review cancels it unless a goal is attached.">No goal</span>');
  }
  const waiting = open && Array.isArray(t.blocked_by) ? t.blocked_by : [];
  if (waiting.length) tags.push(`<span class="badge warn" title="Starts once ${waiting.length === 1 ? 'that task is' : 'those tasks are'} done or cancelled">waiting on ${waiting.map((d) => `#${esc(d)}`).join(', ')}</span>`);
  const due = Date.parse(t.due_at || '');
  if (open && Number.isFinite(due)) {
    const late = due < Date.now();
    tags.push(`<span class="badge ${late ? 'bad' : ''}" title="${esc(`Due ${new Date(due).toLocaleString()}${late ? ' (overdue)' : ''}`)}">due ${esc(shortDate(t.due_at))}</span>`);
  }
  const gate = gateBadge(t);   // Phase 3: green when every gate passed, red with the first failure on hover
  if (gate) tags.push(gate);
  return tags.length ? `<div class="tags">${tags.join('')}</div>` : '';
}

function TabTasks(cid, data) {
  let el, tasks = [], goals = [];
  const draw = () => {
    $('.board', el).innerHTML = LANES.map(([k, label, sts]) => {
      const items = tasks.filter((t) => sts.includes(t.status));
      return `<div class="lane"><h3><span>${label}</span><span class="num">${items.length}</span></h3>
        ${items.map((t) => `<button class="card st-${esc(t.status)}" data-act="task" data-id="${t.id}">
          <div class="t">${esc(t.title)}</div>${taskTags(t)}<div class="m">${avatar(data().agentsById[t.agent_id] || { name: t.agent_name || '?', role: t.role }, 'sm')}
          <span>#${t.id}</span>${t.priority === 1 && !['done', 'cancelled'].includes(t.status) ? '<span class="p1">Urgent</span>' : ''}
          ${t.status === 'blocked' ? '<span style="color:var(--bad)">Blocked</span>' : ''}${t.status === 'waiting' ? '<span style="color:var(--warn)">Waiting on you</span>' : ''}
          <span style="margin-left:auto">${when(t.updated_at)}</span></div></button>`).join('') || '<div class="faint small" style="padding:4px">Nothing here.</div>'}</div>`;
    }).join('');
  };
  const load = async () => { tasks = (await GET(`/api/companies/${cid}/tasks`)).tasks; draw(); };
  const loadSoon = debounce(() => load().catch(() => {}), 500);
  // The goal select offers the brain's active goals (GET /brain); a task keeps a goal that has closed since.
  const loadGoals = async () => { goals = ((await GET(`/api/companies/${cid}/brain`)).goals || []); };
  const loadGoalsSoon = debounce(() => loadGoals().catch(() => {}), 800);
  const agentOptions = (sel) => data().agents.map((a) => `<option value="${a.id}" ${a.id === sel ? 'selected' : ''}>${esc(a.name)} (${esc(a.role)})</option>`).join('');
  // The contract part of both task forms. task is the task being edited (null for a new one).
  const contractFields = (task) => {
    const gid = task && task.goal_id ? task.goal_id : '';
    const active = goals.filter((g) => g.status === 'active');
    const goalOpts = [['', 'No goal'], ...active.map((g) => [g.id, `#${g.id} ${g.title}`])];
    if (gid && !active.some((g) => g.id === gid)) goalOpts.push([gid, `#${gid} ${task.goal_title || ''} (closed)`]);
    const deps = task && Array.isArray(task.depends_on) ? task.depends_on : [];
    const pick = tasks.filter((x) => (!task || x.id !== task.id) && (OPEN_STATUSES.includes(x.status) || deps.includes(x.id)));
    const due = Date.parse((task && task.due_at) || '');
    const daysLeft = Number.isFinite(due) ? Math.ceil((due - Date.now()) / 86400000) : 0;
    const dueHint = !Number.isFinite(due) ? '1 to 90. Empty means no deadline.'
      : daysLeft > 0 ? `Now due ${shortDate(task.due_at)}. Change it to move the deadline, empty it to remove it.`
        : `It was due ${shortDate(task.due_at)}. Enter days to set a new deadline.`;
    return `<div class="contract"><h3>Goal and contract</h3>
      <div class="form-grid">
        <label class="field">Goal it serves<span class="hint">Tasks an agent creates without a goal are cancelled at the weekly review.</span><select name="goal_id">${goalOpts.map(([v, l]) => `<option value="${esc(v)}" ${String(v) === String(gid) ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select></label>
        <label class="field">Due in days<span class="hint">${esc(dueHint)}</span><input type="number" name="due_days" min="1" max="90" value="${daysLeft > 0 ? daysLeft : ''}"></label></div>
      <label class="field">Expected effect<span class="hint">What this task should move, and by how much.</span><input type="text" name="expected" maxlength="500" value="${esc(task ? task.expected : '')}" placeholder="30 more waitlist signups"></label>
      <label class="field">Inputs<span class="hint">Files and facts the agent must read first.</span><textarea name="inputs" rows="2" maxlength="1500" placeholder="research/market.md and the price in the Brain tab">${esc(task ? task.inputs : '')}</textarea></label>
      <div class="deps"><div class="deps-label">Waits for<span class="hint">It starts only once the tasks you tick are done or cancelled.</span></div>
        ${pick.length ? `<div class="dep-list">${pick.map((x) => `<label class="check small"><input type="checkbox" name="depends_on" value="${x.id}" ${deps.includes(x.id) ? 'checked' : ''}><span>#${x.id} ${esc(x.title)}${OPEN_STATUSES.includes(x.status) ? '' : ` <span class="faint">(${esc(x.status)})</span>`}</span></label>`).join('')}</div>`
        : '<p class="small faint" style="margin:0">No open tasks to wait for.</p>'}</div>
      <div class="form-grid">
        <label class="field">Step budget<span class="hint">Tool calls per attempt, 3 to 60. Empty uses the company's setting.</span><input type="number" name="budget_steps" min="3" max="60" value="${task && task.budget_steps ? esc(task.budget_steps) : ''}"></label>
        <label class="field">Token budget<span class="hint">Tokens across all attempts. Empty means no limit.${task && task.tokens_used ? ` Used so far: ${esc(fmtTokens(task.tokens_used))}.` : ''}</span><input type="number" name="budget_tokens" min="0" step="1000" value="${task && task.budget_tokens ? esc(task.budget_tokens) : ''}"></label></div>
      <label class="field">Checks<span class="hint">One per line, each starting with ${CHECK_TOOLS.map((c) => `<code>${c}</code>`).join(', ')}. The agent is told they run on its work.</span>
        <textarea name="checks" rows="2" class="code" placeholder="browser_check site/index.html">${esc(task ? task.checks : '')}</textarea></label></div>`;
  };
  // The contract as the API takes it. A new task sends what is filled in; an edit sends only what changed, so a
  // deadline or a dependency left alone is not set again.
  const contractBody = (form, task) => {
    const fd = new FormData(form);
    const out = {};
    const text = (k) => String(fd.get(k) || '').trim();
    const goal = text('goal_id') ? +text('goal_id') : null;
    if (task ? goal !== (task.goal_id || null) : goal) out.goal_id = goal;
    ['expected', 'inputs', 'checks'].forEach((k) => { if (task ? text(k) !== String(task[k] || '').trim() : text(k)) out[k] = text(k); });
    const deps = fd.getAll('depends_on').map(Number).sort((a, b) => a - b);
    const had = (task && Array.isArray(task.depends_on) ? task.depends_on : []).slice().sort((a, b) => a - b);
    if (task ? deps.join() !== had.join() : deps.length) out.depends_on = deps;
    const due = form.elements.due_days;
    if (!task) { if (due.value) out.due_days = +due.value; } else if (due.value !== due.defaultValue) {
      if (due.value) out.due_days = +due.value; else out.due_at = '';   // emptied: no deadline
    }
    ['budget_steps', 'budget_tokens'].forEach((k) => {
      const inp = form.elements[k];
      if (task ? inp.value !== inp.defaultValue : inp.value) out[k] = inp.value ? +inp.value : '';   // '' on an edit clears it
    });
    return out;
  };
  // A 400 (a goal that is not active, a dependency loop, a check line with an unknown tool) stays in the modal.
  const showError = (form, e) => {
    const box = $('.form-error', form);
    if (!box) { fail(e); return; }
    // The API names the fields as the tools do; the owner sees the form's labels.
    const words = [[/^depends_on would\b/, 'Waiting on these tasks would'], [/\bdepends_on\b/g, 'Waits for'], [/\bbudget_steps\b/g, 'Step budget'], [/\bbudget_tokens\b/g, 'Token budget'],
      [/\bdue_days\b/g, 'Due in days'], [/\bdue_at\b/g, 'The deadline'], [/\bgoal_id\b/g, 'the goal'], [/^checks line\b/, 'Checks: the line']];
    box.textContent = capFirst(words.reduce((m, [re, w]) => m.replace(re, w), String(e.message || e)));
    box.classList.remove('hidden');
    box.scrollIntoView({ block: 'nearest' });
  };
  return {
    async mount(holder) {
      el = holder;
      el.innerHTML = `<div class="actions" style="justify-content:space-between;margin-bottom:14px"><p class="muted" style="margin:0">The CEO plans work here and agents pick it up automatically. Add your own tasks any time.</p>
        <button class="btn primary" data-act="new-task">Add task</button></div><div class="board"></div>`;
      await Promise.all([load(), loadGoals().catch(() => {})]);
    },
    live(ev) {
      if (['tasks', 'agents'].includes(ev.type) || (ev.type === 'event' && ['task', 'done', 'run'].includes(ev.kind))) loadSoon();
      if (ev.type === 'event' && ev.kind === 'brain') loadGoalsSoon();
    },
    act(name, t) {
      if (name === 'new-task') {
        const ceo = data().agents.find((a) => a.role === 'ceo');
        modal({
          title: 'Add a task',
          wide: true,
          body: `<form class="form-stack" data-form="new-task" id="nt"><label class="field">Title<input type="text" name="title" required maxlength="200"></label>
            <label class="field">Details<span class="hint">Objective, inputs to read, and what's out of scope.</span><textarea name="description" rows="4"></textarea></label>
            <div class="form-grid"><label class="field">Deliverable<input type="text" name="deliverable" placeholder="site/pricing.html"></label></div>
            <label class="field">Acceptance criteria<span class="hint">3-5 checkable points, one per line. Agents can't finish until the deliverable exists.</span><textarea name="acceptance" rows="3" placeholder="- Three plans with prices from the charter&#10;- audit_page reports no blockers"></textarea></label>
            <div class="form-grid"><label class="field">Assign to<select name="agent_id">${agentOptions(ceo?.id)}</select></label>
            <label class="field">Priority<select name="priority"><option value="1">Urgent</option><option value="2" selected>Normal</option><option value="3">Later</option></select></label></div>
            ${contractFields(null)}<div class="form-error hidden" role="alert"></div></form>`,
          foot: `<button class="btn" data-act="close-modal">Cancel</button><button class="btn primary" type="submit" form="nt">Add task</button>`,
        });
      }
      if (name === 'task') {
        const task = tasks.find((x) => x.id === +t.dataset.id);
        if (!task) return;
        modal({
          title: `Task #${task.id}`,
          wide: true,
          body: `<h2 style="font-size:19px;margin-bottom:6px">${esc(task.title)}</h2>${taskTags(task)}
            <p class="faint small">Created by ${esc(task.created_by)} ${when(task.created_at)}. Attempts: ${task.attempts}.</p>
            ${task.description ? md(task.description) : '<p class="faint">No details.</p>'}
            ${task.deliverable ? `<p><b>Deliverable:</b> ${/^[\w./-]+$/.test(task.deliverable) && /[/.]/.test(task.deliverable) ? `<a href="#/c/${cid}/files/${encodeURIComponent(task.deliverable)}">${esc(task.deliverable)}</a>` : esc(task.deliverable)}</p>` : ''}
            ${task.acceptance ? `<h3 style="margin:12px 0 4px">Acceptance criteria</h3>${md(task.acceptance)}` : ''}
            ${task.routine ? '<p class="faint small">Recurring duty</p>' : ''}
            ${gateList(task)}
            ${task.result ? `<h3 style="margin:16px 0 6px">Notes and results</h3><div class="box">${md(task.result)}</div>` : ''}
            <form class="form-stack" data-form="edit-task" id="et" data-id="${task.id}" style="margin-top:18px">
              <div class="form-grid"><label class="field">Status<select name="status">${['todo', 'in_progress', 'waiting', 'blocked', 'done', 'cancelled'].map((s) => `<option value="${s}" ${s === task.status ? 'selected' : ''} ${s === 'in_progress' ? 'disabled' : ''}>${s.replace('_', ' ')}</option>`).join('')}</select></label>
              <label class="field">Assigned to<select name="agent_id">${agentOptions(task.agent_id)}</select></label>
              <label class="field">Priority<select name="priority">${[[1, 'Urgent'], [2, 'Normal'], [3, 'Later']].map(([v, l]) => `<option value="${v}" ${v === task.priority ? 'selected' : ''}>${l}</option>`).join('')}</select></label></div>
              <label class="field">Add a note for the agent<textarea name="note" rows="2"></textarea></label>
              ${contractFields(task)}<div class="form-error hidden" role="alert"></div></form>`,
          foot: `<button class="btn" data-act="close-modal">Close</button><button class="btn primary" type="submit" form="et">Save</button>`,
        });
      }
    },
    async submit(name, form) {
      const f = Object.fromEntries(new FormData(form));
      const btn = $(`button[type=submit][form="${form.id}"]`);
      if (btn) btn.disabled = true;
      try {
        if (name === 'new-task') {
          await api('POST', `/api/companies/${cid}/tasks`, {
            title: f.title, description: f.description, deliverable: f.deliverable, acceptance: f.acceptance,
            agent_id: +f.agent_id, priority: +f.priority, ...contractBody(form, null),
          });
        }
        if (name === 'edit-task') {
          const task = tasks.find((x) => x.id === +form.dataset.id);
          const body = { agent_id: +f.agent_id, priority: +f.priority, note: f.note, ...contractBody(form, task) };
          if (f.status !== task.status) body.status = f.status;
          await api('PATCH', `/api/tasks/${form.dataset.id}`, body);
        }
        closeModal();
        toast('Saved.');
        await load();
      } catch (e) { showError(form, e); }
      if (btn) btn.disabled = false;
    },
  };
}

// ---- brain (Phase 2): the company's structured source of truth. Agents read a digest of it at the start of every
// session; the owner edits it here. Edits are PATCH /api/brain/<section>/<id> (the strategy row's id is the
// company's), deletes DELETE the same address, and the server emits a "brain" event so agents see the change.
// In form order: the one-liner across the top, then pairs (price and voice are short, so they sit together).
const STRATEGY_FIELDS = [['one_liner', 'One-liner', 'What you sell and to whom, in one sentence.'], ['icp', 'Customer (ICP)', 'Who buys, as precisely as you can.'],
  ['problem', 'Problem', 'What hurts for them today.'], ['offer', 'Offer', 'What they get.'], ['positioning', 'Positioning', 'Why you, not the alternatives.'],
  ['price', 'Price', 'What they pay.'], ['voice', 'Voice', 'How the team writes.']];
const CONTACT_STAGES = ['lead', 'contacted', 'replied', 'customer', 'lost'];
const GOAL_STATUSES = [['active', 'Active'], ['done', 'Done'], ['dropped', 'Dropped']];
const VERDICT_BADGE = { pending: ['accent', 'Running'], won: ['good', 'Won'], lost: ['bad', 'Lost'], inconclusive: ['', 'Inconclusive'] };
// The fields the owner edits inline, per section: [column, label, kind]. Kinds: text, long, number, date, check,
// role (a role on the team), goal (one of the company's goals), or a list of choices. Mirrors brain.COLUMNS.
const BRAIN_FIELDS = {
  goals: [['title', 'Goal', 'text'], ['metric', 'Metric', 'text'], ['target', 'Target', 'number'], ['deadline', 'Deadline', 'date'], ['owner_role', 'Owner', 'role']],
  contacts: [['name', 'Name', 'text'], ['email', 'Email', 'text'], ['company', 'Company', 'text'], ['role', 'Their role', 'text'], ['stage', 'Stage', CONTACT_STAGES],
    ['source', 'Source', 'text'], ['consent', 'Consent', 'text'], ['last_touch', 'Last touch', 'date'], ['notes', 'Notes', 'long']],
  decisions: [['what', 'What was decided', 'text'], ['expected', 'Expected result', 'text'], ['review_at', 'Review on', 'date'], ['why', 'Why', 'long'],
    ['outcome', 'Outcome', 'long'], ['reversible', 'Can be undone', 'check']],
  experiments: [['hypothesis', 'Hypothesis', 'text'], ['goal_id', 'Goal', 'goal'], ['metric', 'Metric', 'text'], ['start', 'Started', 'date'], ['stop', 'Stops', 'date'],
    ['verdict', 'Verdict', ['pending', 'won', 'lost', 'inconclusive']], ['result', 'Result', 'long']],
  lessons: [['text', 'Lesson', 'long']],
};
const BRAIN_SHOWN = 10;   // rows per list before "Show all"

function TabBrain(cid, data) {
  let el, b = null, editing = null, stale = false, stage = '';
  const shown = {};   // section -> true once the owner pressed "Show all"
  const isEditing = (section, id) => !!editing && editing.section === section && editing.id === id;
  const roles = () => [...new Set(['ceo', ...data().agents.map((a) => a.role)])];
  const ceoName = () => (data().agents.find((a) => a.role === 'ceo') || {}).name || 'the CEO';
  const decider = (by) => ({ ceo: ceoName(), owner: 'you', telegram: 'you, from your phone', deadline: 'the deadline' }[by] || by || 'someone');
  const day = (v) => String(v || '').slice(0, 10);
  const num = (v) => (v === null || v === undefined || v === '' ? '?' : esc(fmtValue(v)));
  const opt = (v, l, cur) => `<option value="${esc(v)}" ${String(v) === String(cur) ? 'selected' : ''}>${esc(l)}</option>`;
  const acts = (section, id) => `<div class="row-acts"><button class="btn sm ghost" data-act="b-edit" data-section="${section}" data-id="${id}">Edit</button>
    <button class="btn sm ghost" data-act="b-del" data-section="${section}" data-id="${id}">Delete</button></div>`;
  const more = (section, total) => (total > BRAIN_SHOWN && !shown[section]
    ? `<li class="more-row"><button class="btn sm ghost" data-act="b-more" data-section="${section}">Show all ${total}</button></li>` : '');
  const cut = (section, rows) => (shown[section] ? rows : rows.slice(0, BRAIN_SHOWN));

  // One inline field. data-orig holds the value it started with, so a save sends only what changed.
  const field = ([col, label, kind], row) => {
    const raw = row[col];
    const val = raw === null || raw === undefined ? '' : kind === 'date' ? day(raw) : String(raw);
    if (kind === 'check') {
      const on = raw === undefined || raw === null ? true : !!Number(raw);
      return `<label class="check small"><input type="checkbox" name="${col}" data-orig="${on ? 1 : 0}" ${on ? 'checked' : ''}><span>${esc(label)}</span></label>`;
    }
    const attrs = `name="${col}" data-orig="${esc(val)}"`;
    if (Array.isArray(kind)) return `<label class="field">${esc(label)}<select ${attrs}>${kind.map((o) => opt(o, capFirst(o), val)).join('')}</select></label>`;
    if (kind === 'role') return `<label class="field">${esc(label)}<select ${attrs}>${[...new Set([...roles(), ...(val ? [val] : [])])].map((r) => opt(r, r, val)).join('')}</select></label>`;
    if (kind === 'goal') return `<label class="field">${esc(label)}<select ${attrs}>${opt('', 'No goal', val)}${b.goals.map((g) => opt(g.id, `#${g.id} ${g.title}${g.status === 'active' ? '' : ` (${g.status})`}`, val)).join('')}</select></label>`;
    if (kind === 'long') return `<label class="field">${esc(label)}<textarea ${attrs} rows="2">${esc(val)}</textarea></label>`;
    const type = kind === 'number' ? 'number' : kind === 'date' ? 'date' : 'text';
    return `<label class="field">${esc(label)}<input type="${type}" ${attrs}${kind === 'number' ? ' step="any"' : ''} value="${esc(val)}"></label>`;
  };
  const editForm = (section, row) => {
    const fields = BRAIN_FIELDS[section];
    const short = fields.filter((f) => !['long', 'check'].includes(f[2]));
    return `<form class="form-stack brain-edit" data-form="b-edit" data-section="${section}" data-id="${row.id}">
      ${short.length ? `<div class="form-grid">${short.map((f) => field(f, row)).join('')}</div>` : ''}
      ${fields.filter((f) => ['long', 'check'].includes(f[2])).map((f) => field(f, row)).join('')}
      <div class="actions"><button class="btn sm primary" type="submit">Save</button><button class="btn sm" type="button" data-act="b-cancel">Cancel</button></div></form>`;
  };

  // ---- strategy: a form on the page; Save sends the fields that changed.
  const strategyDirty = () => $$('#b-strategy [name]', el).some((i) => i.value !== i.defaultValue);
  const drawStrategy = () => {
    const s = b.strategy || {};
    $('#b-strategy-when', el).innerHTML = s.updated_at ? `Changed ${when(s.updated_at)}` : '';
    if (strategyDirty()) return;   // the owner is typing: keep it
    $('#b-strategy', el).innerHTML = `<div class="form-grid strategy-grid">${STRATEGY_FIELDS.map(([k, l, hint]) => (['one_liner', 'price', 'voice'].includes(k)
      ? `<label class="field${k === 'one_liner' ? ' span' : ''}">${esc(l)}${hint ? `<span class="hint">${esc(hint)}</span>` : ''}<input type="text" name="${k}" maxlength="500" value="${esc(s[k] || '')}"></label>`
      : `<label class="field">${esc(l)}${hint ? `<span class="hint">${esc(hint)}</span>` : ''}<textarea name="${k}" rows="2" maxlength="500">${esc(s[k] || '')}</textarea></label>`)).join('')}</div>
      <div class="actions"><button class="btn primary" type="submit">Save strategy</button><span class="small faint">Price, offer, positioning and customer changes an agent proposes come to your Inbox.</span></div>`;
  };

  // ---- goals: a table; the status changes inline, Edit opens the row's form under it.
  const td = (label, html, cls = '') => `<td data-label="${esc(label)}"${cls ? ` class="${cls}"` : ''}><div class="cell">${html}</div></td>`;
  const goalRow = (g) => {
    if (isEditing('goals', g.id)) return `<tr class="editing"><td colspan="7">${editForm('goals', g)}</td></tr>`;
    const pct = g.target ? Math.max(0, Math.min(100, Math.round((100 * (Number(g.current) || 0)) / g.target))) : 0;
    const d = g.days_left;
    const left = g.status === 'active' && d !== null && d !== undefined
      ? `<div class="small ${d < 0 ? 'warn-text' : 'faint'}">${d < 0 ? `${plural(-d, 'day')} overdue` : `${plural(d, 'day')} left`}</div>` : '';
    return `<tr class="${g.status === 'active' ? '' : 'closed'}">
      ${td('Goal', `<b>#${g.id}</b> ${esc(g.title)}`)}${td('Metric', g.metric ? `<code>${esc(g.metric)}</code>` : '<span class="faint">none</span>')}
      ${td('Current / target', `<span class="num">${num(g.current)} of ${num(g.target)}</span><div class="meter"><div class="bar"><span style="width:${pct}%"></span></div></div>`)}
      ${td('Deadline', `${esc(day(g.deadline))}${left}`)}${td('Owner', esc(g.owner_role || ''))}
      ${td('Status', `<select data-b-status="${g.id}" aria-label="Status of goal #${g.id}">${GOAL_STATUSES.map(([v, l]) => opt(v, l, g.status)).join('')}</select>`)}
      <td class="acts-cell">${acts('goals', g.id)}</td></tr>`;
  };
  const drawGoals = () => {
    const active = b.goals.filter((g) => g.status === 'active').length;
    $('#b-goals-count', el).textContent = `${active} of 3 active`;
    $('#b-goals', el).innerHTML = b.goals.length ? b.goals.map(goalRow).join('')
      : '<tr><td colspan="7" class="faint">No goals yet. The CEO sets up to three while planning, or add one below.</td></tr>';
  };

  // ---- contacts: a table with a stage filter; the stage changes inline.
  const contactRow = (c) => {
    if (isEditing('contacts', c.id)) return `<tr class="editing"><td colspan="6">${editForm('contacts', c)}</td></tr>`;
    return `<tr>${td('Name', `<b>${esc(c.name || '(no name)')}</b>${c.unsubscribed ? ' <span class="badge warn">unsubscribed</span>' : ''}${c.company ? `<div class="small muted">${esc(c.company)}${c.role ? `, ${esc(c.role)}` : ''}</div>` : ''}`)}
      ${td('Email', c.email ? esc(c.email) : '<span class="faint">none</span>')}
      ${td('Stage', `<select data-b-stage="${c.id}" aria-label="Stage of ${esc(c.name || c.email || 'contact')}">${CONTACT_STAGES.map((s) => opt(s, capFirst(s), c.stage)).join('')}</select>`)}
      ${td('Last touch', c.last_touch ? esc(day(c.last_touch)) : '<span class="faint">never</span>')}${td('Source', esc(c.source || ''))}
      <td class="acts-cell">${acts('contacts', c.id)}</td></tr>`;
  };
  const drawContacts = () => {
    const count = (s) => b.contacts.filter((c) => c.stage === s).length;
    $('#b-stage', el).innerHTML = opt('', `All stages (${b.contacts.length})`, stage) + CONTACT_STAGES.map((s) => opt(s, `${capFirst(s)} (${count(s)})`, stage)).join('');
    const rows = b.contacts.filter((c) => !stage || c.stage === stage);
    const list = cut('contacts', rows);
    $('#b-contacts', el).innerHTML = rows.length ? list.map(contactRow).join('')
      + (rows.length > list.length ? `<tr class="more-row"><td colspan="6"><button class="btn sm ghost" data-act="b-more" data-section="contacts">Show all ${rows.length}</button></td></tr>` : '')
      : `<tr><td colspan="6" class="faint">${b.contacts.length ? 'No contacts at this stage.' : 'No contacts yet. Sales and support agents add the people they talk to.'}</td></tr>`;
  };

  // ---- proposals: changes the team suggested. The CEO reviews them while planning; those that touch the price,
  // offer, positioning or customer went to the owner as a decision ("In your Inbox"). Either can be decided here.
  const drawProposals = () => {
    const open = b.proposals.filter((p) => p.status === 'pending' || p.status === 'owner');
    const closed = b.proposals.filter((p) => p.status !== 'pending' && p.status !== 'owner').slice(0, 5);
    const item = (p) => `<li><div class="grow"><div><b>#${p.id}</b> by ${esc(p.agent_name)} <span class="badge">${esc(p.label)}</span>
        ${p.status === 'owner' ? ' <a class="badge warn" href="#/inbox" title="It changes the price, offer, positioning or customer, so it waits for your decision">In your Inbox</a>'
        : p.status === 'pending' ? ' <span class="badge accent" title="The CEO reviews it in the next planning cycle">For the CEO</span>'
          : ` <span class="badge ${p.status === 'accepted' ? 'good' : ''}">${esc(p.status)}</span>`}</div>
      ${p.summary ? `<div>${esc(p.summary)}</div>` : ''}${p.reason ? `<div class="small muted">Reason: ${esc(p.reason)}</div>` : ''}
      <div class="small faint">${when(p.created_at)}</div></div>
      ${p.status === 'pending' || p.status === 'owner' ? `<div class="row-acts"><button class="btn sm" data-act="b-proposal" data-d="reject" data-id="${p.id}">Reject</button>
        <button class="btn sm good" data-act="b-proposal" data-d="accept" data-id="${p.id}">Accept</button></div>` : ''}</li>`;
    $('#b-proposals', el).innerHTML = (open.length ? open.map(item).join('') : '<li class="faint">Nothing waiting. When an agent suggests a change to the brain, it shows up here.</li>')
      + (closed.length ? `<li class="sub-head small faint">Recently decided</li>${closed.map(item).join('')}` : '');
    $('#b-proposals-count', el).innerHTML = open.length ? `<span class="badge warn">${open.length} waiting</span>` : '';
  };

  // ---- decisions, experiments, lessons: lists with inline edit and delete.
  const drawDecisions = () => {
    const today = new Date().toISOString().slice(0, 10);
    const item = (r) => (isEditing('decisions', r.id) ? `<li class="editing">${editForm('decisions', r)}</li>` : `<li><div class="grow">
      <div><b>#${r.id}</b> ${esc(r.what)}${Number(r.reversible ?? 1) ? '' : ' <span class="badge warn">Cannot be undone</span>'}</div>
      ${r.expected ? `<div class="small"><b>Expected:</b> ${esc(r.expected)}</div>` : ''}
      ${r.outcome ? `<div class="small"><b>Outcome:</b> ${esc(r.outcome)}</div>`
        : r.review_at ? `<div class="small ${day(r.review_at) <= today ? 'warn-text' : 'faint'}">${day(r.review_at) <= today ? `Review was due ${esc(shortDate(r.review_at))}; no outcome logged yet` : `Review on ${esc(shortDate(r.review_at))}`}</div>` : ''}
      ${r.why ? `<details class="fold"><summary class="small">Why</summary><p class="small muted">${esc(r.why)}</p></details>` : ''}
      <div class="small faint">Decided by ${esc(decider(r.decided_by))}, ${when(r.created_at)}</div></div>${acts('decisions', r.id)}</li>`);
    $('#b-decisions', el).innerHTML = b.decisions.length ? cut('decisions', b.decisions).map(item).join('') + more('decisions', b.decisions.length)
      : '<li class="faint">No decisions logged yet. The CEO logs its own; the ones you make in the Inbox land here too.</li>';
  };
  const drawExperiments = () => {
    const item = (r) => {
      if (isEditing('experiments', r.id)) return `<li class="editing">${editForm('experiments', r)}</li>`;
      const v = VERDICT_BADGE[r.verdict] || ['', r.verdict];
      return `<li><div class="grow"><div><b>#${r.id}</b> ${esc(r.hypothesis)} <span class="badge ${v[0]}">${esc(v[1])}</span></div>
        <div class="small muted">${r.goal_id ? `Goal #${esc(r.goal_id)}, ` : ''}${r.metric ? `<code>${esc(r.metric)}</code>, ` : ''}${esc(day(r.start) || '?')} to ${esc(day(r.stop) || '?')}</div>
        ${r.result ? `<div class="small"><b>Result:</b> ${esc(r.result)}</div>` : ''}</div>${acts('experiments', r.id)}</li>`;
    };
    $('#b-experiments', el).innerHTML = b.experiments.length ? cut('experiments', b.experiments).map(item).join('') + more('experiments', b.experiments.length)
      : '<li class="faint">No experiments yet. The CEO starts one for each bet that could move a goal.</li>';
  };
  const drawLessons = () => {
    const item = (r) => (isEditing('lessons', r.id) ? `<li class="editing">${editForm('lessons', r)}</li>`
      : `<li><div class="grow"><div>${esc(r.text)}</div><div class="small faint">${when(r.created_at)}</div></div>${acts('lessons', r.id)}</li>`);
    $('#b-lessons', el).innerHTML = b.lessons.length ? cut('lessons', b.lessons).map(item).join('') + more('lessons', b.lessons.length)
      : '<li class="faint">No lessons yet. Agents save short rules here when something worked or failed.</li>';
  };

  const draw = () => {
    if (!b || !el) return;
    $('#b-empty', el).innerHTML = b.empty ? `<div class="banner"><div class="grow"><b>Brain not written yet; the CEO fills it in the next planning cycle.</b> You can also fill in the strategy yourself below.</div>
      ${data().company.status === 'active' ? '<button class="btn" data-act="plan">Plan now</button>' : ''}</div>` : '';
    drawStrategy(); drawGoals(); drawContacts(); drawProposals(); drawDecisions(); drawExperiments(); drawLessons();
    const over = b.digest_chars > b.digest_cap;
    $('#b-foot', el).innerHTML = `<span class="${over ? 'warn-text' : ''}">Digest: ${Number(b.digest_chars).toLocaleString()} of ${Number(b.digest_cap).toLocaleString()} characters.</span>
      Every agent reads it at the start of each session and looks up the rest.
      <details class="raw"><summary class="small muted">Exactly what the agents read</summary><pre class="small">${esc(b.digest)}</pre></details>`;
    stale = false;
  };
  const load = async () => {
    b = await GET(`/api/companies/${cid}/brain`);
    if (editing) { stale = true; drawStrategy(); return; }   // keep the open form; redraw once it closes
    draw();
  };
  const loadSoon = debounce(() => load().catch(() => {}), 700);
  const closeEdit = () => { editing = null; draw(); };
  const patch = async (section, id, body, okText = 'Saved. Agents see it in their next session.') => {
    await api('PATCH', `/api/brain/${section}/${id}`, body);
    toast(okText);
  };

  return {
    async mount(holder) {
      el = holder;
      // Strategy beside the proposals (what may need you); the goals and contacts tables take the full width.
      el.innerHTML = `<div id="b-empty"></div><div class="brain">
        <div class="cols">
          <section class="panel"><div class="panel-head"><h2>Strategy</h2><span class="small faint" id="b-strategy-when"></span></div>
            <form class="panel-body form-stack" data-form="b-strategy" id="b-strategy"></form></section>
          <section class="panel"><div class="panel-head"><h2>Proposals</h2><span id="b-proposals-count"></span></div><ul class="rows" id="b-proposals" style="padding-top:8px"></ul></section>
        </div>
        <section class="panel"><div class="panel-head"><h2>Goals</h2><span class="small faint" id="b-goals-count"></span></div>
          <div class="btable-wrap"><table class="btable"><thead><tr><th>Goal</th><th>Metric</th><th>Current / target</th><th>Deadline</th><th>Owner</th><th>Status</th><th><span class="sr">Actions</span></th></tr></thead>
            <tbody id="b-goals"></tbody>
            <tfoot><tr class="add-row">
              <td data-label="Goal"><div class="cell"><input form="b-add-goal" type="text" name="title" required maxlength="200" placeholder="10 paying customers" aria-label="New goal"></div></td>
              <td data-label="Metric"><div class="cell"><input form="b-add-goal" type="text" name="metric" required maxlength="60" placeholder="stripe.customers" aria-label="Metric"></div></td>
              <td data-label="Target"><div class="cell"><input form="b-add-goal" type="number" name="target" required step="any" placeholder="10" aria-label="Target"></div></td>
              <td data-label="Deadline"><div class="cell"><input form="b-add-goal" type="date" name="deadline" required aria-label="Deadline"></div></td>
              <td data-label="Owner"><div class="cell"><select form="b-add-goal" name="owner_role" aria-label="Owner">${roles().map((r) => opt(r, r, 'ceo')).join('')}</select></div></td>
              <td></td><td class="acts-cell"><button form="b-add-goal" class="btn sm primary" type="submit">Add goal</button></td></tr></tfoot></table></div>
          <form id="b-add-goal" data-form="b-add-goal"></form></section>
        <div class="brain-pair">
          <section class="panel"><div class="panel-head"><h2>Decisions</h2></div><ul class="rows" id="b-decisions" style="padding-top:8px"></ul></section>
          <div class="stack">
            <section class="panel"><div class="panel-head"><h2>Experiments</h2></div><ul class="rows" id="b-experiments" style="padding-top:8px"></ul></section>
            <section class="panel"><div class="panel-head"><h2>Lessons</h2></div><ul class="rows" id="b-lessons" style="padding-top:8px"></ul>
              <form class="b-add-lesson" data-form="b-add-lesson"><input type="text" name="text" required maxlength="300" placeholder="Add a lesson: one short rule the team should follow" aria-label="New lesson"><button class="btn" type="submit">Add</button></form></section>
          </div></div>
        <section class="panel"><div class="panel-head"><h2>Contacts</h2><select id="b-stage" class="b-filter" aria-label="Show contacts at this stage"></select></div>
          <div class="btable-wrap"><table class="btable"><thead><tr><th>Name</th><th>Email</th><th>Stage</th><th>Last touch</th><th>Source</th><th><span class="sr">Actions</span></th></tr></thead>
            <tbody id="b-contacts"></tbody></table></div></section>
        </div>
        <div class="brain-foot small faint" id="b-foot"></div>`;
      // Inline selects: a goal's status, a contact's stage, the contacts filter.
      el.addEventListener('change', async (e) => {
        const t = e.target;
        if (t.id === 'b-stage') { stage = t.value; drawContacts(); return; }
        const goal = t.dataset.bStatus;
        const contact = t.dataset.bStage;
        if (!goal && !contact) return;
        const [section, id, body] = goal ? ['goals', goal, { status: t.value }] : ['contacts', contact, { stage: t.value }];
        const row = b[section].find((r) => r.id === +id);
        try {
          await patch(section, id, body, goal ? `Goal #${id} is ${t.value} now.` : 'Stage saved.');
          if (row) Object.assign(row, body);
          await load();
        } catch (err) { fail(err); if (row) t.value = goal ? row.status : row.stage; }
      });
      await load();
    },
    live(ev) { if (ev.type === 'event' && ['brain', 'decision'].includes(ev.kind)) loadSoon(); },
    async act(name, t) {
      const section = t.dataset.section;
      const id = +t.dataset.id;
      if (name === 'b-edit') { editing = { section, id }; draw(); $('form.brain-edit input, form.brain-edit textarea', el)?.focus(); return; }
      if (name === 'b-cancel') { closeEdit(); return; }
      if (name === 'b-more') { shown[section] = true; draw(); return; }
      if (name === 'b-del') {
        const what = { goals: 'goal', contacts: 'contact', decisions: 'decision', experiments: 'experiment', lessons: 'lesson' }[section] || 'row';
        if (!confirm(`Delete this ${what}? Agents stop seeing it at once. This cannot be undone.`)) return;
        try {
          await api('DELETE', `/api/brain/${section}/${id}`, {});
          if (isEditing(section, id)) editing = null;
          toast(`${capFirst(what)} deleted.`);
          await load();
        } catch (e) { fail(e); }
        return;
      }
      if (name === 'b-proposal') {
        $$('button', t.closest('li')).forEach((x) => { x.disabled = true; });
        try {
          const r = await api('POST', `/api/brain/changes/${id}`, { decision: t.dataset.d });
          toast(r.status === 'accepted' ? `Accepted${r.applied ? `: ${r.applied}` : ''}.` : 'Rejected.');
          await load();
          refreshState();   // a proposal that was in the Inbox is answered there too
        } catch (e) { fail(e); $$('button', t.closest('li')).forEach((x) => { x.disabled = false; }); }
      }
    },
    async submit(name, form) {
      const btn = $('button[type=submit]', form) || $(`button[type=submit][form="${form.id}"]`, el);
      if (btn) btn.disabled = true;
      try {
        if (name === 'b-strategy') {
          const body = {};
          $$('[name]', form).forEach((i) => { if (i.value !== i.defaultValue) body[i.name] = i.value.trim(); });
          if (!Object.keys(body).length) toast('Nothing changed.');
          else {
            await patch('strategy', cid, body);
            $$('[name]', form).forEach((i) => { i.defaultValue = i.value; });
            await load();
          }
        }
        if (name === 'b-edit') {
          const body = {};
          $$('[name]', form).forEach((i) => {
            const v = i.type === 'checkbox' ? (i.checked ? '1' : '0') : i.value;
            if (v !== i.dataset.orig) body[i.name] = i.type === 'checkbox' ? i.checked : v.trim();
          });
          if (Object.keys(body).length) await patch(form.dataset.section, form.dataset.id, body);
          closeEdit();
          if (Object.keys(body).length || stale) await load();
        }
        if (name === 'b-add-goal') {
          const f = Object.fromEntries(new FormData(form));
          await api('POST', `/api/companies/${cid}/brain/goals`, { title: f.title.trim(), metric: f.metric.trim(), target: f.target, deadline: f.deadline, owner_role: f.owner_role });
          form.reset();
          toast('Goal added. Agents plan with it from their next session.');
          await load();
        }
        if (name === 'b-add-lesson') {
          await api('POST', `/api/companies/${cid}/brain/lessons`, { text: form.text.value.trim() });
          form.reset();
          toast('Lesson saved. Every agent reads the newest lessons.');
          await load();
        }
      } catch (e) { fail(e); }
      if (btn) btn.disabled = false;
    },
  };
}

// ---- team
function TabTeam(cid, data, _arg, reloadCompany) {
  let el;
  const draw = async () => {
    const models = await loadModels();
    const d = data();
    $('#team', el).innerHTML = d.agents.map((a) => `
      <li data-agent="${a.id}" style="flex-wrap:wrap">${avatar(a, 'lg')}
        <div class="info" style="min-width:200px"><div><b>${esc(a.name)}</b> <span class="role">${esc(a.title)}</span>${!presetByKey(a.preset || a.role) ? ' <span class="badge">custom</span>' : ''}</div>
          <div class="doing">${!a.enabled ? 'Off' : a.status === 'working' ? esc(a.status_note || 'Working') : a.status === 'error' ? `<span style="color:var(--bad)">${esc(a.status_note)}</span>` : `Idle${a.last_run_at ? `, last active ${rel(a.last_run_at)}` : ''}`}</div></div>
        <div class="actions">
          <select data-act-change="model" aria-label="Model for ${esc(a.name)}" style="width:auto;max-width:210px"><option value="">Default model</option>${models.map((m) => `<option ${m === a.model ? 'selected' : ''}>${esc(m)}</option>`).join('')}${a.model && !models.includes(a.model) ? `<option selected>${esc(a.model)}</option>` : ''}</select>
          <label class="check small" style="align-items:center"><input type="checkbox" data-act-change="enabled" ${a.enabled ? 'checked' : ''} style="margin:0"> On</label>
          <button class="btn sm" data-act="run">${a.role === 'ceo' ? 'Plan now' : 'Run next task'}</button>
          <button class="btn sm" data-act="watch">Watch thinking</button>
          <button class="btn sm" data-act="sessions">Sessions</button>
          ${presetByKey(a.preset || a.role) ? `<button class="btn sm ghost" data-act="preset-info" data-key="${esc(a.preset || a.role)}">Playbook</button>` : ''}
          <button class="btn sm ghost" data-act="edit-agent">Edit</button>
        </div></li>`).join('');
    const hired = new Set(d.agents.map((a) => a.preset || a.role));
    $('#gallery', el).innerHTML = presetGallery(S.presets, { hire: true, hired });
  };
  return {
    async mount(holder) {
      el = holder;
      await loadPresets();
      el.innerHTML = `<section class="panel"><ul class="team-list" id="team"></ul></section>
        <section class="panel" style="margin-top:18px"><div class="panel-head"><h2>Hire from the library</h2><span class="small faint">Agents only use the GPU when they have work, so a bigger team means more work queued, not more load.</span></div>
          <div class="panel-body" id="gallery"></div></section>
        <section class="panel" style="margin-top:18px"><details><summary class="panel-head" style="padding-bottom:14px;cursor:pointer"><h2>Create a custom agent</h2></summary>
        <form class="panel-body form-stack" data-form="hire"><div class="form-grid">
          <label class="field">Role key<input type="text" name="role" required maxlength="24" placeholder="pricing, translations"></label>
          <label class="field">Name<input type="text" name="name" maxlength="30"></label>
          <label class="field">Title<input type="text" name="title" maxlength="60" placeholder="Head of Pricing"></label></div>
          <label class="field">What they own<span class="hint">Their focus, how they work and how to judge good work. Custom agents get a general tool belt.</span><textarea name="focus" rows="4"></textarea></label>
          <div><button class="btn primary" type="submit">Add agent</button></div></form></details></section>`;
      el.addEventListener('change', async (e) => {
        const t = e.target.closest('[data-act-change]');
        if (!t) return;
        const id = +t.closest('[data-agent]').dataset.agent;
        const body = t.dataset.actChange === 'model' ? { model: t.value } : { enabled: t.checked };
        try { await api('PATCH', `/api/agents/${id}`, body); toast('Saved.'); } catch (err) { fail(err); }
      });
      await draw();
    },
    onCompany: () => draw(),
    async act(name, t) {
      const li = t.closest('[data-agent]');
      const a = li ? data().agentsById[+li.dataset.agent] : null;
      if (name === 'preset-info') { showPreset(t.dataset.key, { hire: !data().agents.some((x) => (x.preset || x.role) === t.dataset.key) }); return; }
      if (name === 'hire-preset') {
        t.disabled = true;
        try {
          const r = await api('POST', `/api/companies/${cid}/agents`, { preset: t.dataset.key });
          closeModal();
          toast(`${r.agent.name} joined as ${r.agent.title}.`);
          await reloadCompany();
          refreshState();
        } catch (e) { fail(e); t.disabled = false; }
        return;
      }
      if (name === 'run') {
        try { const r = await api('POST', `/api/agents/${a.id}/run`, {}); toast(r.message); } catch (e) { fail(e); }
      }
      if (name === 'edit-agent') {
        const p = presetByKey(a.preset || a.role);
        modal({
          title: `Edit ${a.name}`,
          body: `<form class="form-stack" data-form="edit-agent" id="ea" data-id="${a.id}"><div class="form-grid">
            <label class="field">Name<input type="text" name="name" value="${esc(a.name)}"></label>
            <label class="field">Title<input type="text" name="title" value="${esc(a.title)}"></label></div>
            <label class="field">Extra instructions<span class="hint">Added to ${p ? 'the ' + esc(p.title) + ' playbook' : 'this agent\'s role'} in every session and take priority over it.</span><textarea name="focus" rows="5">${esc(a.focus)}</textarea></label>
            <div class="form-grid">
              <label class="field">Reasoning effort<span class="hint">For gpt-oss models. Higher is slower but better at multi-step work.</span><select name="reasoning">
                <option value="">Playbook default (${esc(p ? p.reasoning : 'medium')})</option>${['low', 'medium', 'high'].map((r) => `<option value="${r}" ${a.reasoning === r ? 'selected' : ''}>${r[0].toUpperCase() + r.slice(1)}</option>`).join('')}</select></label>
              <label class="check" style="align-self:end"><input type="checkbox" name="routines_on" ${a.routines_on ? 'checked' : ''}><span>Do recurring duties${p && p.routines.length ? `<br><span class="faint small">${p.routines.map((r) => `${esc(r.title)} (${cadence(r.every_hours)})`).join(', ')}</span>` : '<br><span class="faint small">None in this role</span>'}</span></label>
            </div></form>`,
          foot: `${a.role !== 'ceo' ? '<button class="btn danger" data-act="remove-agent" style="margin-right:auto">Remove agent</button>' : ''}<button class="btn" data-act="close-modal">Cancel</button><button class="btn primary" type="submit" form="ea">Save</button>`,
        });
        S.editAgent = a;
      }
      if (name === 'remove-agent') {
        const ag = S.editAgent;
        if (!confirm(`Remove ${ag.name}? Their open tasks go back to the CEO.`)) return;
        try { await api('DELETE', `/api/agents/${ag.id}`); closeModal(); toast('Removed.'); await reloadCompany(); } catch (e) { fail(e); }
      }
      if (name === 'sessions') {
        const r = await GET(`/api/companies/${cid}/runs?agent_id=${a.id}`);
        modal({
          title: `${a.name}'s work sessions`, wide: true,
          body: r.runs.length ? `<ul class="rows">${r.runs.map((x) => `<li><div class="grow"><div><span class="badge ${x.status === 'done' ? 'good' : x.status === 'error' ? 'bad' : x.status === 'partial' ? 'warn' : ''}">${esc(x.status)}</span> <b>${esc(x.kind)}</b>${x.task_id ? ` on task #${x.task_id}` : ''} <span class="faint small">${when(x.started_at)}, ${x.steps} steps, ${Number(x.tokens).toLocaleString()} tokens${Number(x.max_prompt_tokens) > 0 ? `, peak prompt ${fmtCount(x.max_prompt_tokens)} tokens` : ''}</span></div>
            <div class="small muted">${esc((x.summary || '').replace(/^Status: \w+\n?/, '').slice(0, 300))}</div></div><button class="btn sm" data-act="transcript" data-id="${x.id}">Transcript</button></li>`).join('')}</ul>` : '<p class="faint">No sessions yet.</p>',
        });
      }
      if (name === 'transcript') showTranscript(+t.dataset.id);
      if (name === 'watch') location.hash = `#/c/${cid}/mind/${a.id}`;
    },
    async submit(name, form) {
      const f = Object.fromEntries(new FormData(form));
      try {
        if (name === 'hire') { await api('POST', `/api/companies/${cid}/agents`, f); form.reset(); toast('Agent added.'); await reloadCompany(); }
        if (name === 'edit-agent') {
          await api('PATCH', `/api/agents/${form.dataset.id}`, { name: f.name, title: f.title, focus: f.focus, reasoning: f.reasoning, routines_on: !!form.routines_on.checked });
          closeModal(); toast('Saved.'); await reloadCompany();
        }
      } catch (e) { fail(e); }
    },
  };
}

async function showTranscript(runId) {
  const { run } = await GET(`/api/runs/${runId}`);
  const turn = (m) => {
    if (m.role === 'assistant') {
      return `<div class="turn assistant"><div class="r">${esc(run.agent_name)}</div>${m.thinking ? `<details class="think"><summary>Thinking</summary><pre class="small">${esc(m.thinking)}</pre></details>` : ''}${m.content ? `<div>${md(m.content)}</div>` : ''}
        ${(m.tool_calls || []).map((c) => `<pre class="small">→ ${esc(c.name)}(${esc(JSON.stringify(c.arguments, null, 1)).slice(0, 4000)})</pre>`).join('')}</div>`;
    }
    if (m.role === 'tool') return `<div class="turn tool"><div class="r">Result of ${esc(m.name)}</div><pre class="small">${esc(m.content)}</pre></div>`;
    return `<div class="turn"><div class="r">${m.role === 'user' ? 'Instructions' : esc(m.role)}</div><pre class="small">${esc(m.content)}</pre></div>`;
  };
  modal({ title: `Session #${run.id}: ${run.kind}, ${run.status}`, wide: true, body: `<div class="transcript">${run.transcript.map(turn).join('')}</div>` });
}

// ---- thinking: a live view of what each agent is reasoning and doing
function TabMind(cid, data, arg) {
  let el, agentId = +arg || 0, showTools = true, items = [], oldest = 0, more = false;
  const KINDS = 'think,result,run,done,error,claims,question_deduped';
  const stepHtml = (ev) => {
    const a = S.agents[ev.agent_id];
    const d = ev.data || {};
    const who = a ? esc(a.name) : 'Holdco';
    const head = (label) => `<div class="mh">${a ? avatar(a, '', false) : ''}<b>${who}</b> <span class="faint small">${label} · ${when(ev.created_at)}</span></div>`;
    if (ev.kind === 'think') {
      const calls = (d.calls || []).map((c) => `<div class="call">→ <b>${esc(c.name)}</b> <span class="faint">${esc(JSON.stringify(c.args)).slice(0, 500)}</span></div>`).join('');
      return `<li class="m-think">${head(`step ${d.step || ''}${d.task_id ? `, task #${d.task_id}` : ''}`)}
        ${d.thinking ? `<details class="think" open><summary>Thinking</summary><pre>${esc(d.thinking)}</pre></details>` : ''}
        ${d.said ? `<div class="said">${md(d.said)}</div>` : ''}${calls}
        ${!d.thinking && !d.said && !calls ? '<div class="faint small">(no visible reasoning)</div>' : ''}</li>`;
    }
    if (ev.kind === 'result') {
      if (!showTools) return '';
      return `<li class="m-tool ${d.failed ? 'bad' : ''}"><details><summary>${d.failed ? '⚠ ' : '↳ '}${esc(d.name)} result <span class="faint small">${esc((d.result || '').replace(/\s+/g, ' ').slice(0, 110))}</span></summary><pre>${esc(d.result || '')}</pre></details></li>`;
    }
    return `<li class="m-sep k-${esc(ev.kind)}">${eventBadge(ev)}${who}: ${esc(ev.message)} <span class="faint small">${when(ev.created_at)}</span></li>`;
  };
  const paint = (keepBottom) => {
    const box = $('#mind-box', el);
    const near = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    const html = items.map(stepHtml).join('');
    $('#mind-list', el).innerHTML = (more ? '<li class="m-more"><button class="btn sm ghost" data-act="older">Show older</button></li>' : '') +
      (html || '<li class="faint" style="padding:20px">Nothing yet. When an agent starts working you will see its reasoning, the tools it picks and what they return, live.</li>');
    if (keepBottom || near) box.scrollTop = box.scrollHeight;
  };
  const working = () => {
    const w = data().agents.filter((a) => a.status === 'working' && (!agentId || a.id === agentId));
    $('#mind-now', el).innerHTML = w.length ? w.map((a) => `<span class="badge accent"><span class="spinner"></span> ${esc(a.name)}: ${esc(a.status_note || 'working')}</span>`).join(' ')
      : '<span class="faint small">Nobody is working right now. Press "Run next task" on the Team tab, or wait for autopilot.</span>';
  };
  const chips = () => {
    const d = data();
    $('#mind-chips', el).innerHTML = `<button class="btn sm ${agentId ? '' : 'primary'}" data-act="pick" data-id="0">Everyone</button>` +
      d.agents.map((a) => `<button class="btn sm ${agentId === a.id ? 'primary' : ''}" data-act="pick" data-id="${a.id}">${esc(a.name)}</button>`).join('');
  };
  const load = async (before = 0) => {
    const r = await GET(`/api/events?limit=60&company_id=${cid}&kinds=${KINDS}${agentId ? `&agent_id=${agentId}` : ''}${before ? `&before=${before}` : ''}`);
    const batch = r.events.slice().reverse();
    items = before ? batch.concat(items) : batch;
    if (r.events.length) oldest = r.events[r.events.length - 1].id;
    more = r.events.length === 60;
    paint(!before);
  };
  return {
    async mount(holder) {
      el = holder;
      el.innerHTML = `<section class="panel"><div class="panel-head"><h2>What your agents are thinking</h2>
          <label class="check small" style="align-items:center"><input type="checkbox" id="mind-tools" checked style="margin:0"> Show tool results</label></div>
        <div class="panel-body"><div id="mind-chips" class="chips" style="margin-bottom:10px"></div><div id="mind-now" style="margin-bottom:12px"></div>
        <p class="faint small" style="margin:0 0 10px">Reasoning is the model's own scratch work. Some models don't share it, in which case you will still see what they say and which tools they use.</p>
        <div id="mind-box" class="mind"><ul id="mind-list"></ul></div></div></section>`;
      $('#mind-tools', el).onchange = (e) => { showTools = e.target.checked; paint(false); };
      chips(); working();
      await load();
    },
    onCompany() { chips(); working(); },
    live(ev) {
      if (ev.type !== 'event' || ev.company_id !== cid || !KINDS.split(',').includes(ev.kind)) return;
      if (agentId && ev.agent_id !== agentId) return;
      items.push(ev);
      if (items.length > 400) items.splice(0, items.length - 400);
      paint(false);
    },
    async act(name, t) {
      if (name === 'pick') { agentId = +t.dataset.id; history.replaceState(null, '', `#/c/${cid}/mind${agentId ? '/' + agentId : ''}`); chips(); working(); await load(); }
      if (name === 'older') await load(oldest);
    },
  };
}

// ---- files
function TabFiles(cid, data, initial) {
  let el, files = [], leases = [], current = initial || null, editing = false, content = null;
  const slug = () => data().company.slug;
  // Phase 2: a file an agent is writing right now carries a lease ("editing: Forge", the task on hover). Your own
  // saves go through anyway: the owner is the board.
  const leaseOf = (path) => leases.find((l) => l.path === path) || null;
  const leaseBadge = (l) => {
    const until = Date.parse(l.expires_at || '');
    const title = `${l.agent_name || 'An agent'} is editing it${l.task_id ? ` for task #${l.task_id}` : ''}${Number.isFinite(until) ? ` until ${new Date(until).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}` : ''}`;
    return `<span class="badge warn lease" title="${esc(title)}">editing: ${esc(l.agent_name || 'an agent')}</span>`;
  };
  const drawTree = () => {
    const visible = files.filter((f) => !f.dir).sort((a, b) => a.path.localeCompare(b.path));
    let lastDir = null;
    $('.tree', el).innerHTML = visible.length ? visible.map((f) => {
      const dir = f.path.includes('/') ? f.path.slice(0, f.path.lastIndexOf('/')) : '';
      const head = dir !== lastDir ? `<div class="dir">${esc(dir || 'top level')}</div>` : '';
      lastDir = dir;
      const l = leaseOf(f.path);
      return `${head}<button class="${f.path === current ? 'on' : ''}${l ? ' leased' : ''}" data-act="open" data-path="${esc(f.path)}" title="${esc(f.path)}"><span class="fname">${esc(f.path.split('/').pop())}</span>${l ? leaseBadge(l) : ''}</button>`;
    }).join('') : '<div class="faint" style="padding:14px">No files yet. They appear as agents work.</div>';
  };
  const drawViewer = () => {
    const v = $('.viewer', el);
    if (!current) { v.innerHTML = `<div class="viewer-body faint">Pick a file to read it here. Text files can be edited.</div>`; return; }
    const isImg = /\.(png|jpe?g|gif|webp|svg|ico)$/i.test(current);
    const isSite = current.startsWith('site/');
    const raw = `/files/${encodeURIComponent(slug())}/${current.split('/').map(encodeURIComponent).join('/')}`;
    let body;
    if (isImg) body = `<img src="${raw}" alt="${esc(current)}">`;
    else if (!content) body = '<span class="spinner"></span>';
    else if (content.binary) body = `<p class="faint">Binary file (${content.size.toLocaleString()} bytes). <a href="${raw}" download>Download</a></p>`;
    else if (editing) body = `<textarea id="file-edit">${esc(content.content)}</textarea>`;
    else if (/\.md$/i.test(current)) body = md(content.content);
    else body = `<pre class="small">${esc(content.content)}</pre>`;
    const lease = leaseOf(current);
    v.innerHTML = `<div class="viewer-head"><span><code class="small" style="overflow-wrap:anywhere">${esc(current)}</code> <span id="file-lease">${lease ? leaseBadge(lease) : ''}</span></span><div class="actions">
      ${isSite && /\.html?$/i.test(current) ? `<a class="btn sm" target="_blank" rel="noopener" href="/preview/${encodeURIComponent(slug())}/${current.slice(5).split('/').map(encodeURIComponent).join('/')}">Open preview</a>` : ''}
      ${!isImg && content && !content.binary ? (editing ? `<button class="btn sm" data-act="cancel-edit">Cancel</button><button class="btn sm primary" data-act="save-file">Save</button>` : `<button class="btn sm" data-act="edit-file">Edit</button>`) : ''}
      <a class="btn sm ghost" href="${raw}" download>Download</a></div></div><div class="viewer-body">${body}</div>`;
  };
  const open = async (path) => {
    current = path; editing = false; content = null;
    history.replaceState(null, '', `#/c/${cid}/files/${encodeURIComponent(path)}`);
    drawTree(); drawViewer();
    if (!/\.(png|jpe?g|gif|webp|svg|ico)$/i.test(path)) {
      try { content = await GET(`/api/companies/${cid}/file?path=${encodeURIComponent(path)}`); } catch (e) { content = { binary: false, content: `Couldn't open: ${e.message}` }; }
      drawViewer();
    }
  };
  const loadTree = async () => {
    const r = await GET(`/api/companies/${cid}/files`);
    files = r.files;
    leases = Array.isArray(r.leases) ? r.leases : [];
    drawTree();
    const badge = el && $('#file-lease', el);   // only the badge: the open file keeps its scroll and edits
    if (badge) { const l = current && leaseOf(current); badge.innerHTML = l ? leaseBadge(l) : ''; }
  };
  const loadSoon = debounce(() => loadTree().catch(() => {}), 800);
  return {
    async mount(holder) {
      el = holder;
      el.innerHTML = `<section class="panel files"><div class="tree"></div><div class="viewer"></div></section>`;
      await loadTree();
      if (current) open(current); else drawViewer();
    },
    live(ev) {
      if (ev.type === 'event' && ev.kind === 'file') {
        loadSoon();
        if (ev.data?.path === current && !editing) open(current);
      }
      if (ev.type === 'agents') loadSoon();   // a finished session releases its leases
    },
    async act(name, t) {
      if (name === 'open') open(t.dataset.path);
      if (name === 'edit-file') { editing = true; drawViewer(); $('#file-edit', el)?.focus(); }
      if (name === 'cancel-edit') { editing = false; drawViewer(); }
      if (name === 'save-file') {
        const text = $('#file-edit', el).value;
        try { await api('PUT', `/api/companies/${cid}/file`, { path: current, content: text }); content.content = text; editing = false; drawViewer(); toast('Saved.'); } catch (e) { fail(e); }
      }
    },
  };
}

// ---- context: the company's own library (the owner's files agents read but never change)
function TabContext(cid, _data, initial) {
  return ContextLibrary({ base: `/api/companies/${cid}/context`, company: true, initial: initial || null, home: `#/c/${cid}/context` });
}

// ---- memory
function TabMemory(cid) {
  let el;
  const load = async () => {
    const r = await GET(`/api/companies/${cid}/memory`);
    $('#mem', el).innerHTML = r.memory.length ? r.memory.map((m) => `<li data-mem="${m.id}"><div class="grow">
      <div class="small faint">${m.pinned ? '<span class="badge accent">Pinned</span> ' : ''}${esc(m.kind)} from ${esc(m.agent_name || 'you')}, ${when(m.created_at)}</div>
      <div>${inline(m.content)}</div></div>
      <button class="btn sm ghost" data-act="pin" data-p="${m.pinned ? 0 : 1}">${m.pinned ? 'Unpin' : 'Pin'}</button>
      <button class="btn sm ghost" data-act="forget">Delete</button></li>`).join('') : '<li class="faint">Nothing remembered yet.</li>';
  };
  const loadSoon = debounce(() => load().catch(() => {}), 800);
  return {
    async mount(holder) {
      el = holder;
      el.innerHTML = `<section class="panel"><form class="panel-body form-stack" data-form="note">
          <label class="field">Tell the team something to remember<span class="hint">Pinned notes are shown to every agent in every session. Use them for facts that must never be forgotten.</span>
          <textarea name="content" rows="2" placeholder="Our refund policy is 30 days, no questions asked."></textarea></label>
          <div class="actions"><label class="check small"><input type="checkbox" name="pinned"> Pin it</label><button class="btn primary" type="submit">Save note</button></div></form></section>
        <section class="panel" style="margin-top:18px"><ul class="rows" id="mem"></ul></section>`;
      await load();
    },
    live(ev) { if (ev.type === 'event' && ['memory', 'decision'].includes(ev.kind)) loadSoon(); },
    async act(name, t) {
      const id = t.closest('[data-mem]')?.dataset.mem;
      try {
        if (name === 'pin') await api('PATCH', `/api/memory/${id}`, { pinned: !!+t.dataset.p });
        if (name === 'forget') await api('DELETE', `/api/memory/${id}`);
        await load();
      } catch (e) { fail(e); }
    },
    async submit(name, form) {
      try {
        await api('POST', `/api/companies/${cid}/memory`, { content: form.content.value, pinned: form.pinned.checked });
        form.reset();
        await load();
      } catch (e) { fail(e); }
    },
  };
}

// ---- email
function TabEmail(cid, data) {
  let el, emails = [];
  const load = async () => {
    emails = (await GET(`/api/companies/${cid}/emails`)).emails;
    $('#mail', el).innerHTML = emails.length ? emails.map((m) => `<li data-act="mail" data-id="${m.id}" style="cursor:pointer">
      <span class="badge ${m.direction === 'in' ? 'accent' : ''}">${m.direction === 'in' ? 'In' : 'Out'}</span>
      <div class="grow"><div><b>${esc(m.subject || '(no subject)')}</b></div><div class="small muted">${m.direction === 'in' ? `From ${esc(m.from_addr)}` : `To ${esc(m.to_addr)}`}, ${when(m.created_at)}</div></div>
      <span class="badge">${esc(m.status)}</span></li>`).join('') : '<li class="faint">No email yet.</li>';
  };
  return {
    async mount(holder) {
      el = holder;
      const d = data();
      el.innerHTML = `${!d.email_ready || !d.inbox_ready ? `<div class="banner warn"><div class="grow"><b>${!d.email_ready && !d.inbox_ready ? 'Email isn\'t connected yet.' : !d.email_ready ? 'Sending isn\'t set up yet.' : 'Receiving isn\'t set up yet.'}</b>
        Give this company its own mailbox (any provider with SMTP and IMAP, e.g. Gmail with an app password, Zoho or Fastmail). Sales and support agents use it for outreach and replies, always through your inbox first.</div>
        <a class="btn" href="#/c/${cid}/settings">Set up email</a></div>` : ''}
        <section class="panel"><div class="panel-head"><h2>Mail</h2>${d.inbox_ready ? '<button class="btn sm" data-act="check-mail">Check for new mail</button>' : ''}</div><ul class="rows" id="mail" style="padding-top:8px"></ul></section>`;
      await load();
    },
    live(ev) { if (ev.type === 'event' && ['email', 'decision'].includes(ev.kind)) load().catch(() => {}); },
    async act(name, t) {
      if (name === 'check-mail') {
        try { const r = await api('POST', `/api/companies/${cid}/emails/check`); toast(`${plural(r.new, 'new email')}.`); await load(); } catch (e) { fail(e); }
      }
      if (name === 'mail') {
        const m = emails.find((x) => x.id === +t.dataset.id);
        modal({ title: m.subject || '(no subject)', body: `<p class="small muted">From ${esc(m.from_addr)} to ${esc(m.to_addr)}, ${when(m.created_at)}</p><pre style="font-family:var(--font);font-size:14px">${esc(m.body)}</pre>` });
      }
    },
  };
}

// ---- charter
function TabCharter(cid, data) {
  let el, editing = false;
  const draw = () => {
    const c = data().company;
    el.innerHTML = `<section class="panel"><div class="panel-head"><h2>Charter</h2><div class="actions">
      ${editing ? `<button class="btn sm" data-act="cancel">Cancel</button><button class="btn sm primary" data-act="save">Save</button>` : `<button class="btn sm" data-act="edit">Edit</button>`}</div></div>
      <div class="panel-body">${editing ? `<textarea id="charter" style="min-height:60vh" class="code">${esc(c.charter)}</textarea>`
        : c.charter ? md(c.charter) : '<p class="faint">The CEO writes the charter while founding the company.</p>'}</div></section>`;
  };
  return {
    async mount(holder) { el = holder; draw(); },
    onCompany: () => { if (!editing) draw(); },
    async act(name) {
      if (name === 'edit') { editing = true; draw(); }
      if (name === 'cancel') { editing = false; draw(); }
      if (name === 'save') {
        try { await api('PATCH', `/api/companies/${cid}`, { charter: $('#charter', el).value }); data().company.charter = $('#charter', el).value; editing = false; draw(); toast('Charter saved. Agents use it from their next session.'); } catch (e) { fail(e); }
      }
    },
  };
}

// ---- company settings
// Settings Holdco keeps for itself in the same JSON; the form never sends them (the server would accept founding_stage).
// Kept in step with server.INTERNAL_COMPANY_SETTINGS (plus pinned_context, which the Context tab owns).
const INTERNAL_COMPANY_SETTINGS = ['founding_stage', 'founding_retry', 'founding_research_note', 'founding_context', 'last_waiting_notice',
  'budget_notice', 'truth_scan_done', 'name_locked', 'template', 'pinned_context'];
const EMAIL_PROVIDERS = [['auto', 'Resend when connected, otherwise SMTP'], ['smtp', 'SMTP'], ['resend', 'Resend']];

function TabSettings(cid, data, arg) {
  let el, global, plan = null, trustData = null, trustBusy = false;
  const conns = ConnectorCards({ scope: 'company', cid, focus: connectorFocus(arg) });
  // Phase 3: the trust ladder per action (GET /trust). Level 0 asks, 1 runs on its own up to the daily cap, 2 runs on
  // its own. Locked actions always ask; run_code follows its approval setting. A change is saved at once (PUT).
  const trustRow = (r) => {
    const td = (label, html) => `<td data-label="${esc(label)}"><div class="cell">${html}</div></td>`;
    const name = `<b>${esc(r.label || r.action)}</b>`;
    if (!r.on_ladder) {
      return `<tr class="closed">${td('Action', name)}${td('Level', r.locked ? '<span class="badge">always asks</span>'
        : `<span class="small muted" title="Code runs ask or not by the approval setting above; with Docker it runs in a sandbox">follows its approval setting</span>`)}<td></td><td></td><td></td></tr>`;
    }
    const cap = Number(r.cap_per_day) || 1;
    const used = Number(r.used_today) || 0;
    const streak = Number(r.streak) || 0;
    return `<tr data-trust="${esc(r.action)}">${td('Action', name)}
      ${td('Level', `<select data-trust-level aria-label="Trust level for ${esc(r.label || r.action)}">${LEVEL_LABEL(cap).map(([v, l]) => `<option value="${v}" ${Number(r.level) === v ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select>`)}
      ${td('Streak', `<span class="num" title="${esc(streak ? streakText(streak) : 'No approvals in a row yet')}">${fmtCount(streak)}</span>`)}
      ${td('Cap per day', `<input type="number" data-trust-cap min="1" max="1000" step="1" value="${esc(cap)}" aria-label="Daily cap for ${esc(r.label || r.action)}">`)}
      ${td('Today', `<span class="num">${Number(r.level) === 1 ? `${fmtCount(used)} of ${fmtCount(cap)}` : fmtCount(used)}</span>`)}</tr>`;
  };
  const drawTrust = (force = false) => {
    const box = el && $('#trust-box', el);
    if (!box || !trustData) return;
    // A refresh from elsewhere keeps a cap the owner is typing; a save redraws.
    if (!force && $$('[data-trust-cap]', box).some((x) => x === document.activeElement && x.value !== x.defaultValue)) return;
    box.innerHTML = `<div class="btable-wrap"><table class="btable trust-table"><thead><tr><th>Action</th><th>Level</th><th title="Approvals in a row without edits">Streak</th><th>Cap per day</th><th title="Times it ran on its own today">Today</th></tr></thead>
      <tbody>${trustData.actions.map(trustRow).join('')}</tbody></table></div>`;
  };
  const loadTrust = async () => {
    try { trustData = await GET(`/api/companies/${cid}/trust`); } catch (e) {
      const box = el && $('#trust-box', el);
      if (box) box.innerHTML = `<p class="warn-text small" style="margin:0">The trust levels could not load: ${esc(e.message)}</p>`;
      return;
    }
    drawTrust();
  };
  const loadTrustSoon = debounce(() => { if (!trustBusy) loadTrust(); }, 600);
  const saveTrust = async (tr) => {
    if (!tr || trustBusy) return;   // one save at a time; the redraw after it shows the result
    const action = tr.dataset.trust;
    const level = +$('[data-trust-level]', tr).value;
    const capBox = $('[data-trust-cap]', tr);
    const cap = parseInt(capBox.value, 10);
    if (!Number.isFinite(cap) || cap < 1 || cap > 1000) { toast('The cap per day is a whole number from 1 to 1000.', true); capBox.focus(); return; }
    const was = trustData.actions.find((r) => r.action === action) || {};
    if (Number(was.level) === level && Number(was.cap_per_day) === cap) return;
    trustBusy = true;
    $$('select, input', tr).forEach((x) => { x.disabled = true; });
    try {
      trustData = await api('PUT', `/api/companies/${cid}/trust`, { action, level, cap_per_day: cap });
      const label = was.label || action;
      toast(level === 0 ? `${label}: agents ask you first.` : level === 1 ? `${label}: agents do it on their own, up to ${cap} a day.` : `${label}: agents do it on their own.`);
    } catch (e) { fail(e); }
    trustBusy = false;
    drawTrust(true);
  };
  const draw = () => {
    const c = data().company;
    const s = c.settings;
    const pol = s.policies || {};
    const val = (k) => esc(s[k] ?? '');
    const used = plan && plan.budget ? plan.budget.used : null;
    const bud = data().budgets || null;   // Phase 2: what each budget has used (GET /api/companies/<id>)
    const detected = !s.archetype && plan && plan.archetype ? ` (now: ${archetypeLabel(plan.archetype)})` : '';
    const provider = EMAIL_PROVIDERS.some(([k]) => k === s.email_provider) ? s.email_provider : 'auto';
    $('#co-set-form', el).innerHTML = `<form class="panel" data-form="co-settings">
      <div class="section"><h2>Direction</h2>
        <label class="field">Standing orders<span class="hint">Every agent reads these in every session. Brand rules, things to avoid, priorities.</span>
          <textarea name="directives" rows="5" placeholder="Never offer discounts. Write in British English. Focus on cafés in Manchester first.">${val('directives')}</textarea></label>
        <div class="form-grid" style="margin-top:14px">
          <label class="field">CEO re-plans every (minutes)<input type="number" name="cycle_minutes" min="5" value="${val('cycle_minutes')}"></label>
          <label class="field">Company name<input type="text" name="name" value="${esc(c.name)}"></label>
        </div>
        <div class="form-grid" style="margin-top:14px">
          <label class="field">Business type<span class="hint">Picks the playbook agents plan with: how money flows, first experiments, common traps.</span>
            <select name="archetype"><option value="">Detect automatically${esc(detected)}</option>${ARCHETYPES.map(([k, l]) => `<option value="${k}" ${s.archetype === k ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select></label>
          <label class="field">Daily token budget<span class="hint">Background work stops for the day once this is used; chat and your own tasks still run. 0 = no limit.${used !== null ? ` Used today: ${esc(Number(used).toLocaleString())}.` : ''}</span>
            <input type="number" name="daily_token_budget" min="0" step="100000" value="${val('daily_token_budget')}"></label>
        </div></div>
      <div class="section" id="budgets"><h2>Budgets</h2><p class="muted">Limits for this company. Once one is used up, agents stop that kind of work until it resets; chat and tasks you create still run.</p>
        <div class="form-grid">
          <label class="field">GPU hours per day<span class="hint">Model time this company may use each day. Agents pick up no new work once it is used, until midnight UTC.${bud ? ` Used today: ${esc(fmtHours(bud.gpu_hours_today))} h.` : ''}</span>
            <input type="number" name="gpu_hours_per_day" min="0.25" step="0.25" value="${val('gpu_hours_per_day')}"></label>
          <label class="field">Steps per task<span class="hint">Tool calls an agent may make per attempt at a task. 0 means the app default (${esc(global.settings.max_steps_per_run)}, from Settings).</span>
            <input type="number" name="steps_per_task" min="0" max="60" value="${val('steps_per_task')}"></label>
          <label class="field">Posts per week<span class="hint">Social posts approved or waiting in the last 7 days.${bud ? ` This week: ${esc(bud.posts_this_week)}.` : ''}</span>
            <input type="number" name="posts_per_week" min="0" value="${val('posts_per_week')}"></label>
          <label class="field">Emails per day<span class="hint">New domains should start around 20.${bud ? ` Sent today: ${esc(bud.emails_today)}.` : ''}</span>
            <input type="number" name="daily_email_limit" min="0" value="${val('daily_email_limit')}"></label>
          <label class="field">Money budget, USD (display only)<span class="hint">Holdco never spends money: every purchase asks you and you buy it yourself.${bud ? ` $${esc(fmtValue(bud.money_approved_month))} approved this month.` : ''}</span>
            <input type="number" name="money_usd" min="0" value="${val('money_usd')}"></label>
          <label class="field">Quiet hours<span class="hint">Low-priority research with no deadline runs in this window (this PC's clock), or when nothing else is queued. For example 23:00-07:00; empty means no window.</span>
            <input type="text" name="quiet_hours" value="${val('quiet_hours')}" placeholder="23:00-07:00" autocomplete="off" pattern="\\s*\\d{1,2}:[0-5]\\d\\s*-\\s*\\d{1,2}:[0-5]\\d\\s*" title="Two times, for example 23:00-07:00"></label>
        </div></div>
      <div class="section"><h2>Approvals for this company</h2><p class="muted">Override your global defaults.</p>
        <div class="form-grid">${Object.entries(global.policy_actions).map(([k, label]) => `<label class="field">${esc(label)}${global.locked.includes(k) ? '<div class="box small">Always asks you</div>' :
          `<select name="policy_${k}"><option value="">Default (${esc({ approve: 'ask me', auto: 'allowed', off: 'never' }[global.settings.policies[k]])})</option>
          ${[['approve', 'Ask me first'], ['auto', 'Let agents do it'], ['off', 'Never allow']].map(([v, l]) => `<option value="${v}" ${pol[k] === v ? 'selected' : ''}>${l}</option>`).join('')}</select>`}</label>`).join('')}</div></div>
      <div class="section" id="trust"><h2>Trust</h2><p class="muted">Each approval you give without changes builds a streak. At ${esc(trustData ? trustData.promote_at : 15)} in a row Holdco asks whether agents may do it on their own; a rejection moves it one level down. Legal wording, a first email to a new person and anything after agents read outside content still ask. Changes here apply at once.</p>
        <div id="trust-box"><span class="spinner"></span></div></div>
      <div class="section"><h2>Email</h2>
        <div class="form-grid" style="margin:10px 0 16px;max-width:520px">
          <label class="field">Send email with<span class="hint">Resend is set up on its card under Connectors at the top of this tab. Replies always arrive through IMAP below.</span>
            <select name="email_provider">${EMAIL_PROVIDERS.map(([k, l]) => `<option value="${k}" ${provider === k ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select></label>
        </div>
        <p class="muted">Sending (SMTP). Port 587 uses STARTTLS, 465 uses SSL. For Gmail, create an app password.</p>
        <div class="form-grid">
          <label class="field">SMTP server<input type="text" name="smtp_host" value="${val('smtp_host')}" placeholder="smtp.gmail.com"></label>
          <label class="field">Port<input type="number" name="smtp_port" value="${val('smtp_port')}"></label>
          <label class="field">Username<input type="text" name="smtp_user" value="${val('smtp_user')}" autocomplete="off"></label>
          <label class="field">Password<input type="password" name="smtp_password" value="${val('smtp_password')}" autocomplete="new-password" data-secret></label>
          <label class="field">From address<input type="email" name="smtp_from" value="${val('smtp_from')}"></label>
          <label class="field">From name<input type="text" name="smtp_from_name" value="${val('smtp_from_name')}" placeholder="${esc(c.name)}"></label>
        </div>
        <label class="field" style="margin-top:14px">Postal address<span class="hint">Commercial email must include a postal address; agents can't email prospects until this is set.</span>
          <input type="text" name="postal_address" value="${val('postal_address')}" placeholder="1 Main Street, Springfield, IL 62701" autocomplete="street-address"></label>
        <p class="muted" style="margin-top:16px">Receiving (IMAP). New mail becomes a task for the support agent.</p>
        <div class="form-grid">
          <label class="field">IMAP server<input type="text" name="imap_host" value="${val('imap_host')}" placeholder="imap.gmail.com"></label>
          <label class="field">Port<input type="number" name="imap_port" value="${val('imap_port')}"></label>
          <label class="field">Username<input type="text" name="imap_user" value="${val('imap_user')}" autocomplete="off"></label>
          <label class="field">Password<input type="password" name="imap_password" value="${val('imap_password')}" autocomplete="new-password" data-secret></label>
        </div>
        <div class="actions" style="margin-top:14px"><input type="email" id="test-to" placeholder="Send a test email to…" style="max-width:280px"><button class="btn" type="button" data-act="test-email">Send test</button></div></div>
      <div class="section"><h2>Website</h2><p class="muted">The site in <code>site/</code> always previews locally. To publish it, add a Netlify personal access token; Holdco creates the site on first deploy.</p>
        <div class="form-grid">
          <label class="field">Netlify token<input type="password" name="netlify_token" value="${val('netlify_token')}" autocomplete="off" data-secret></label>
          <label class="field">Netlify site ID<span class="hint">Leave blank to create one.</span><input type="text" name="netlify_site_id" value="${val('netlify_site_id')}"></label>
          <label class="field">Public address<input type="url" name="site_url" value="${val('site_url')}"></label>
        </div></div>
      <div class="section"><h2>Posting webhook</h2><p class="muted">Approved social posts are sent here as JSON, so you can wire them to X, LinkedIn, Discord or anything else with Zapier, Make or n8n. Without it, you get a ready-to-post link.</p>
        <label class="field">Webhook address<input type="url" name="webhook_url" value="${val('webhook_url')}"></label></div>
      <div class="section actions" style="justify-content:flex-end"><button class="btn primary" type="submit">Save</button></div>
    </form>
    <section class="panel" style="margin-top:18px"><div class="section"><h2>Archive or delete</h2><p class="muted">Archiving stops all work and keeps everything. Deleting removes the company from Holdco and moves its folder to the trash folder in your data directory.</p>
      <div class="actions">${c.status === 'archived' ? `<button class="btn" data-act="status" data-s="active">Unarchive</button>` : `<button class="btn" data-act="status" data-s="archived">Archive</button>`}
      <button class="btn danger" data-act="delete">Delete company</button></div></div></section>`;
  };
  return {
    async mount(holder) {
      el = holder;
      // Connectors first, at the top of the tab; they load while the settings form is fetched.
      el.innerHTML = '<div id="co-conns" class="conn-holder"></div><div id="co-set-form"></div>';
      const connsReady = conns.mount($('#co-conns', el));
      const [g, pl] = await Promise.all([GET('/api/settings'), GET(`/api/companies/${cid}/planning`).catch(() => null)]);
      global = g;
      plan = pl && pl.planning;
      draw();
      if (arg === 'budgets') $('#budgets', el)?.scrollIntoView({ block: 'start' });   // from the Overview's GPU line
      // The trust table sits inside the settings form without names, so Save never sends it: each change is a PUT.
      el.addEventListener('change', (e) => {
        const tr = e.target.closest('[data-trust]');
        if (tr && (e.target.matches('[data-trust-level]') || e.target.matches('[data-trust-cap]'))) saveTrust(tr);
      });
      el.addEventListener('input', (e) => {   // "On its own, up to N a day" follows the cap being typed
        if (!e.target.matches('[data-trust-cap]')) return;
        const o = $('[data-trust-level] option[value="1"]', e.target.closest('[data-trust]'));
        const n = parseInt(e.target.value, 10);
        if (o && Number.isFinite(n) && n > 0) o.textContent = LEVEL_LABEL(n)[1][1];
      });
      el.addEventListener('keydown', (e) => {   // Enter in a cap saves that row, not the whole form
        if (e.key === 'Enter' && e.target.matches('[data-trust-cap]')) { e.preventDefault(); saveTrust(e.target.closest('[data-trust]')); }
      });
      await Promise.all([connsReady, loadTrust()]);
    },
    live(ev) {
      conns.live(ev);
      if (ev.type === 'trust' || ev.type === 'approvals') loadTrustSoon();   // a promotion, a streak, a demotion
    },
    async submit(name, form) {
      if (conns.ownsForm(name)) return conns.submit(name, form);
      if (name !== 'co-settings') return undefined;
      const f = Object.fromEntries(new FormData(form));
      const settings = { policies: {} };
      for (const [k, v] of Object.entries(f)) {
        if (k.startsWith('policy_')) settings.policies[k.slice(7)] = v;
        else if (k !== 'name' && !INTERNAL_COMPANY_SETTINGS.includes(k)) settings[k] = v;
      }
      dropUntouchedSecrets(form, settings);
      const budget = parseInt(settings.daily_token_budget, 10);
      if (Number.isFinite(budget)) settings.daily_token_budget = Math.max(0, budget); else delete settings.daily_token_budget;   // empty keeps the stored value
      // Phase 2 budgets: an empty box keeps the stored value, like the token budget.
      ['gpu_hours_per_day', 'steps_per_task', 'posts_per_week', 'daily_email_limit', 'money_usd'].forEach((k) => {
        const n = k === 'gpu_hours_per_day' ? parseFloat(settings[k]) : parseInt(settings[k], 10);
        if (Number.isFinite(n)) settings[k] = n; else delete settings[k];
      });
      if (typeof settings.postal_address === 'string') settings.postal_address = settings.postal_address.trim();
      if (typeof settings.quiet_hours === 'string') settings.quiet_hours = settings.quiet_hours.replace(/\s+/g, '');
      try {
        const r = await api('PATCH', `/api/companies/${cid}`, { name: f.name, settings });
        syncSecrets(form, r.company && r.company.settings);   // a password just saved now shows the mask
        toast('Saved.');
        refreshState();
      } catch (e) { fail(e); }
      return undefined;
    },
    async act(name, t) {
      if (conns.owns(name)) return conns.act(name, t);
      if (name === 'test-email') {
        const to = $('#test-to', el).value.trim();
        if (!to) { toast('Enter an address to send the test to.', true); return; }
        await this.submit('co-settings', $('form[data-form="co-settings"]', el));
        try { const r = await api('POST', `/api/companies/${cid}/emails/test`, { to }); toast(r.message, !r.ok); } catch (e) { fail(e); }
      }
      if (name === 'delete') {
        const c = data().company;
        modal({
          title: `Delete ${c.name}?`,
          body: `<p>This removes the company, its agents, tasks and history from Holdco. Its folder moves to the trash folder in your data directory.</p>
            <form data-form="confirm-delete" id="cd" class="form-stack"><label class="field">Type <b>${esc(c.name)}</b> to confirm<input type="text" name="confirm" autocomplete="off"></label></form>`,
          foot: `<button class="btn" data-act="close-modal">Cancel</button><button class="btn danger" type="submit" form="cd">Delete for good</button>`,
        });
      }
    },
  };
}

// ---------------------------------------------------------------- global events
document.addEventListener('click', async (e) => {
  const t = e.target.closest('[data-act]');
  if (!t) return;
  const name = t.dataset.act;
  if (name === 'overlay') { if (e.target === t) closeModal(); return; }
  if (name === 'close-modal') { e.preventDefault(); closeModal(); return; }
  if (name === 'menu') { $('#side').classList.toggle('open'); return; }
  if (name === 'toggle-autopilot') {
    const on = !S.st.scheduler_enabled;
    try { await api('PUT', '/api/settings', { scheduler_enabled: on }); toast(on ? 'Autopilot on. Agents keep working on their own.' : 'Autopilot off. Running sessions finish, then agents wait.'); refreshState(); } catch (err) { fail(err); }
    return;
  }
  if (name === 'decide') { e.preventDefault(); decideRequest(t.closest('[data-req]'), t.dataset.d, t.dataset.answer || ''); return; }
  if (name === 'kill-on') { confirmKill(); return; }
  if (name === 'kill-confirm') { setKill(true, t); return; }
  if (name === 'kill-off') { setKill(false, t); return; }
  if (name === 'pick' && S.view?.actPick) { S.view.actPick(t); return; }
  if (S.view?.act) { e.preventDefault(); S.view.act(name, t, e); }
});

document.addEventListener('submit', async (e) => {
  const form = e.target.closest('form[data-form]');
  if (!form) return;
  e.preventDefault();
  const name = form.dataset.form;
  if (name === 'confirm-delete') {
    const cid = route()[1];
    try { await api('DELETE', `/api/companies/${cid}`, { confirm: form.confirm.value }); closeModal(); await refreshState(); location.hash = '#/'; toast('Company deleted.'); } catch (err) { fail(err); }
    return;
  }
  if (S.view?.submit) S.view.submit(name, form, e);
});

// Live updates from the server.
function connect() {
  const es = new EventSource('/api/stream');
  es.onopen = () => { if (S.offline) { S.offline = false; refreshState(); } };
  es.onerror = () => { S.offline = true; renderSide(); };
  es.onmessage = (m) => {
    let ev;
    try { ev = JSON.parse(m.data); } catch (_) { return; }
    if (ev.type === 'event') S.feeds.forEach((f) => f.add(ev));
    if (ev.type !== 'messages') stateSoon();
    S.view?.live?.(ev);
  };
}

(async function boot() {
  await refreshState();
  await render();
  connect();
})();
