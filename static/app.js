// MQL Browser: a single page over /api/topic, /api/search and
// /api/mqlread. Routes live in the URL hash:
//   #/view/<id>     an object
//   #/view/<id>?asof=<timestamp>
//                   an object as it was at a time (UTC)
//   #/history/<id>  changes to an object's links
//   #/search/<text> name search
//   #/query         MQL query editor
//   #/graph/<id>    graph view (graph.js)
'use strict';

const main = document.getElementById('main');

// Build an element: el('a', {href: '#'}, 'text', child, ...).
function el(tag, attrs, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined) continue;
    if (k === 'class') e.className = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return e;
}

// Replace the page's contents; like el(), skips null/false and flattens.
function render(...children) {
  main.replaceChildren(...children.flat(Infinity).filter(
    c => c !== null && c !== undefined && c !== false));
}

// While viewing the past (#/view/...?asof=T), links stay in it.
let currentAsof = null;

function viewHref(id, asof = currentAsof) {
  return '#/view/' + encodeURIComponent(id)
    + (asof ? '?asof=' + encodeURIComponent(asof) : '');
}
function historyHref(id) { return '#/history/' + encodeURIComponent(id); }

// graphd timestamps, like 2026-11-04T18:28:44.0012Z: the digits after
// the seconds number the primitives written in that second.
function formatTime(ts) {
  return ts ? ts.replace('T', ' ').replace(/\.\d+Z?$|Z$/, '') : '';
}
// The present, as a timestamp for a datetime-local input (UTC).
function nowUtc() { return new Date().toISOString().slice(0, 19); }

function objectLink(obj) {
  return el('a', {href: viewHref(obj.id), title: obj.id}, obj.name || obj.id);
}

async function api(path, params) {
  const url = path + '?' + new URLSearchParams(params);
  const resp = await fetch(url);
  let body;
  try {
    body = await resp.json();
  } catch (e) {
    throw new Error(`${resp.status} ${resp.statusText} from ${path}`);
  }
  if (!resp.ok) {
    const msgs = (body.messages || []).map(m => m.message).join('\n');
    const err = new Error(msgs || `${resp.status} ${resp.statusText}`);
    err.body = body;
    err.status = resp.status;
    throw err;
  }
  return body;
}

function showError(err) {
  render(el('div', {class: 'error'}, err.message || String(err)));
}

function loading() {
  render(el('p', {class: 'note'}, 'Loading…'));
}

// ---- object view ----

function renderValue(v) {
  if (v.id !== undefined) return objectLink(v);
  const shown = typeof v.value === 'string' ? v.value : JSON.stringify(v.value);
  const type = v.type && v.type !== '/type/text' ? v.type.replace(/^\/type\//, '') : null;
  return el('span', {}, shown, type && el('span', {class: 'value-type'}, type));
}

function renderSections(sections, incoming) {
  return sections.map(s => el('div', {class: 'section'},
    el('div', {class: 'section-head'},
      el('h3', {}, s.type.id ? objectLink(s.type) : s.type.name),
      s.type.id && el('span', {class: 'mono'}, s.type.id)),
    el('dl', {class: 'props'}, s.properties.map(p => el('div', {class: 'prop'},
      el('dt', {},
        incoming ? el('span', {}, p.name, ' of') : p.name,
        el('a', {class: 'mono', href: viewHref(p.id)}, p.id)),
      el('dd', {}, el('div', {class: 'values'},
        p.values.map(renderValue))))))));
}

function renderTypeInfo(info, typeId) {
  const props = info.properties.length
    ? el('div', {class: 'section'}, el('div', {class: 'table-wrap'}, el('table', {},
        el('tr', {}, el('th', {}, 'Property'), el('th', {}, 'Id'),
          el('th', {}, 'Expected type'), el('th', {}, 'Unique')),
        info.properties.map(p => el('tr', {},
          el('td', {}, p.name),
          el('td', {}, el('a', {class: 'mono', href: viewHref(p.id)}, p.id)),
          el('td', {}, p.expected_type ? objectLink(p.expected_type) : ''),
          el('td', {}, p.unique ? 'yes' : ''))))))
    : el('p', {class: 'note'}, 'No properties.');
  const shown = info.instances.length;
  return [
    el('h2', {}, 'Properties of this type'),
    props,
    el('h2', {}, `Instances (about ${info.instance_count})`),
    el('div', {class: 'values'}, info.instances.map(objectLink)),
    shown < info.instance_count && el('p', {class: 'more'},
      `Showing ${shown}. `,
      el('a', {href: '#/query', onclick: () => {
        pendingQuery = [{type: typeId, id: null, name: null, limit: 100}];
      }}, 'Query for more')),
  ];
}

function asofForm(id, asof) {
  const input = el('input', {type: 'datetime-local', step: '1', class: 'asof-input',
                             value: (asof || nowUtc()).slice(0, 19)});
  return el('form', {class: 'asof-form', onsubmit: e => {
    e.preventDefault();
    if (input.value) location.hash = viewHref(id, input.value);
  }}, el('label', {}, 'View as of ', input, ' UTC '),
     el('button', {class: 'secondary'}, 'Go'));
}

async function showTopic(id, asof) {
  loading();
  let t;
  try {
    t = await api('/api/topic', asof ? {id, asof} : {id});
  } catch (e) {
    if (!(asof && e.status === 404)) throw e;
    render(
      el('div', {class: 'asof-banner'}, `${id} did not exist at ${formatTime(asof)} UTC. `,
        el('a', {href: viewHref(id, null)}, 'View it now'), ' · ',
        el('a', {href: historyHref(id)}, 'History')),
      asofForm(id, asof));
    return;
  }
  document.title = (t.name || t.id) + (asof ? ` (${formatTime(asof)})` : '')
    + ' – MQL Browser';
  const truncated = Object.entries(t.truncated).filter(([, v]) => v).map(([k]) => k);
  render(
    asof && el('div', {class: 'asof-banner'},
      `As it was at ${formatTime(asof)} UTC. `,
      el('a', {href: viewHref(t.id, null)}, 'View it now'), ' · ',
      el('a', {href: historyHref(t.id)}, 'History')),
    el('h1', {}, t.name || t.id),
    el('div', {class: 'ids mono'},
      el('span', {}, t.id),
      el('span', {}, t.guid),
      t.timestamp && el('span', {title: 'created'}, t.timestamp),
      el('a', {href: graphHref(t.id)}, 'Graph'),
      el('a', {href: historyHref(t.id)}, 'History')),
    asofForm(t.id, asof),
    el('div', {class: 'chips'}, t.types.map(ty =>
      el('a', {class: 'chip', href: viewHref(ty.id), title: ty.id}, ty.name || ty.id))),
    t.keys.length > 1 && el('p', {class: 'note'}, 'Keys: ',
      t.keys.map(k => `${k.namespace}/${k.value}`).join(', ')),
    truncated.length > 0 && el('p', {class: 'more'},
      `Some ${truncated.join(', ')} links are not shown (more than 500).`),
    t.type_info && renderTypeInfo(t.type_info, t.id),
    el('h2', {}, 'Properties'),
    t.properties.length ? renderSections(t.properties, false)
                        : el('p', {class: 'note'}, 'No properties.'),
    el('h2', {}, 'Linked from'),
    t.incoming.length ? renderSections(t.incoming, true)
                      : el('p', {class: 'note'}, 'Nothing links here.'),
  );
}

// ---- history ----

const OPERATIONS = {
  create: 'created',
  insert: 'added',
  update: 'changed',
  delete: 'removed',
};

function renderEnd(object, value) {
  if (object && value !== null) return [objectLink(object), el('span', {class: 'value-type'}, value)];
  if (object) return objectLink(object);
  return el('span', {class: 's'}, JSON.stringify(value));
}

function renderChange(c, id) {
  const property = c.property && (c.direction === 'in'
    ? [el('a', {href: viewHref(c.property.id), title: c.property.id}, c.property.name || c.property.id), ' of']
    : el('a', {href: viewHref(c.property.id), title: c.property.id}, c.property.name || c.property.id));
  const value = c.operation === 'create' ? null
    : c.previous ? [el('del', {}, renderEnd(c.previous.object, c.previous.value)), ' → ',
                    renderEnd(c.object, c.value)]
    : renderEnd(c.object, c.value);
  // An insertion or update that a later change replaced or removed.
  const superseded = !c.valid && c.operation !== 'delete';
  return el('tr', {class: superseded ? 'superseded' : null,
                   title: superseded ? 'No longer current' : null},
    el('td', {class: 'mono'}, formatTime(c.timestamp).slice(11)),
    el('td', {}, el('span', {class: 'op op-' + c.operation}, OPERATIONS[c.operation])),
    el('td', {}, property),
    el('td', {}, value),
    el('td', {}, c.creator ? objectLink({id: c.creator, name: c.creator.replace(/^\/user\//, '')}) : ''),
    el('td', {}, el('a', {href: viewHref(id, c.timestamp), title: 'The object just after this change'},
      'as of')));
}

async function showHistory(id) {
  loading();
  const h = await api('/api/history', {id});
  document.title = `History of ${h.name || h.id} – MQL Browser`;
  // Newest first, under a heading for each day.
  const days = [];
  for (const c of h.changes) {
    const day = formatTime(c.timestamp).slice(0, 10);
    if (!days.length || days[days.length - 1].day !== day) days.push({day, changes: []});
    days[days.length - 1].changes.push(c);
  }
  render(
    el('h1', {}, 'History: ', el('a', {href: viewHref(h.id)}, h.name || h.id)),
    el('div', {class: 'ids mono'}, el('span', {}, h.id),
      el('a', {href: graphHref(h.id)}, 'Graph')),
    el('p', {class: 'note'},
      `${h.changes.length - 1} changes to links from and to this object since it was created. `
      + 'Times are UTC; faded rows were later replaced or removed. '
      + '“As of” shows the object just after a change.'),
    asofForm(h.id, null),
    h.truncated && el('p', {class: 'more'},
      'Only the most recent changes in each direction are shown.'),
    days.map(d => [
      el('h2', {}, d.day),
      el('div', {class: 'section'}, el('div', {class: 'table-wrap'},
        el('table', {class: 'history'}, d.changes.map(c => renderChange(c, h.id))))),
    ]));
}

// ---- search ----

async function showSearch(text) {
  loading();
  document.title = text + ' – MQL Browser';
  const {result} = await api('/api/search', {q: text});
  render(
    el('h1', {}, `Search: ${text}`),
    result.length ? el('ul', {class: 'results'}, result.map(r => el('li', {},
      objectLink(r),
      el('span', {class: 'mono'}, r.id),
      el('div', {class: 'chips'}, r.type.map(ty =>
        el('span', {class: 'chip'}, ty.name || ty.id))))))
      : el('p', {class: 'note'}, 'No matches.'));
}

// ---- query editor ----

let pendingQuery = null;
let explainQueries = false;
const EXAMPLE_QUERY = [{
  type: '/type/type',
  id: null,
  name: null,
  sort: 'name',
  limit: 10,
}];

// JSON with strings that look like ids linked to their objects.
function renderJson(v, indent = '') {
  const inner = indent + '  ';
  if (Array.isArray(v)) {
    if (!v.length) return ['[]'];
    return ['[\n', v.flatMap((x, i) => [inner, ...renderJson(x, inner),
      i < v.length - 1 ? ',\n' : '\n']), indent, ']'];
  }
  if (v && typeof v === 'object') {
    const keys = Object.keys(v);
    if (!keys.length) return ['{}'];
    return ['{\n', keys.flatMap((k, i) => [inner, el('span', {class: 'k'}, JSON.stringify(k)),
      ': ', ...renderJson(v[k], inner), i < keys.length - 1 ? ',\n' : '\n']), indent, '}'];
  }
  if (typeof v === 'string') {
    const s = JSON.stringify(v);
    return [/^(\/[^\s]*|#[0-9a-f]{32})$/.test(v)
      ? el('a', {href: viewHref(v)}, s) : el('span', {class: 's'}, s)];
  }
  return [el('span', {class: 'n'}, JSON.stringify(v))];
}

// ---- explain: the graphd requests behind a read ----

// graphd's cost fields (graphd/doc/gr-protocol.txt), in display order.
const COSTS = {
  tr: 'real time in graphd, ms',
  tu: 'user CPU time in graphd, ms',
  ts: 'system CPU time in graphd, ms (mostly disk reads)',
  te: 'end-to-end time in graphd, ms, including waiting',
  dr: 'primitives read',
  in: 'index lookups',
  ir: 'index elements read',
  va: 'result values allocated',
  pf: 'page faults',
  pr: 'page reclaims',
  dw: 'primitives written',
  iw: 'index elements written',
};
// Request kinds (see server.py's Trace): one, many, what they're for.
const KINDS = {
  'query': ['query', 'queries', 'the read itself, compiled to graphd'],
  'schema': ['schema load', 'schema loads', 'types and properties the query names'],
  'id lookup': ['id lookup', 'id lookups', 'ids and namespace keys resolved to guids'],
  'dateline': ['dateline check', 'dateline checks', 'has graphd changed since the last read?'],
  'other': ['other request', 'other requests', ''],
};

const CACHE_NOTES = {
  kept: 'Schema and ids came from the server\'s cache.',
  bypassed: 'Reads of the past don\'t use the server\'s cache of schema and ids.',
  new: 'The server\'s cache of schema and ids was empty.',
  flushed: 'graphd had changed, so the server dropped its cache of schema and ids.',
};

function ms(n) { return n < 10 ? n.toFixed(1) + ' ms' : Math.round(n) + ' ms'; }
function countOf(n, one, many) { return `${n} ${n === 1 ? one : many}`; }

// A graphd request, one constraint per line, with guids linked.
// "(" after "=" or inside such a list ("result=((guid contents))",
// "guid=(a b)") is a value list and stays on its line; any other "("
// opens a constraint.
function formatGraphd(text) {
  const tokens = text.match(/"(?:[^"\\]|\\.)*"|[()]|[^\s()"]+|\s+/g) || [];
  const out = [];
  const stack = [];
  let depth = 0;
  let prev = '';
  for (const t of tokens) {
    if (/^\s+$/.test(t)) {
      if (!/\n$/.test(String(out[out.length - 1] || ''))) out.push(' ');
      continue;
    }
    if (t === '(') {
      const inline = prev.endsWith('=') || (prev === '(' && stack[stack.length - 1] === false);
      stack.push(!inline);
      if (!inline) {
        if (out.length && out[out.length - 1] === ' ') out.pop();
        out.push('\n' + '  '.repeat(depth));
        depth++;
      }
      out.push('(');
    } else if (t === ')') {
      if (out[out.length - 1] === ' ') out.pop();
      if (stack.pop()) depth--;
      out.push(')');
    } else if (/^[0-9a-f]{32}$/.test(t)) {
      out.push(el('a', {href: viewHref('#' + t), class: 'guid'}, t));
    } else if (t.startsWith('"')) {
      out.push(el('span', {class: 's'}, t));
    } else {
      const m = t.match(/^([a-z<>\-]+=)(.*)$/);
      out.push(m ? [el('span', {class: 'k'}, m[1]), m[2]] : t);
    }
    prev = t;
  }
  return out;
}

function costChips(cost, keys) {
  if (!cost) return null;
  return el('span', {class: 'costs'}, keys.filter(k => k in cost).map(k =>
    el('span', {class: 'cost', title: COSTS[k]}, `${k}=${cost[k]}`)));
}

function renderRequest(r, i, open) {
  return el('details', {class: 'gq', open: open || null},
    el('summary', {},
      el('span', {class: 'gq-n'}, '#' + (i + 1)),
      el('span', {class: 'gq-kind'}, r.kind),
      el('span', {}, ms(r.ms)),
      costChips(r.cost, ['tr', 'dr', 'ir']),
      r.error ? el('span', {class: 'gq-error'}, 'error') : null),
    el('pre', {class: 'json gq-text'}, formatGraphd(r.query)),
    r.error ? el('div', {class: 'error'}, r.error) : null,
    r.cost ? el('div', {class: 'gq-costs'}, costChips(r.cost, Object.keys(COSTS))) : null);
}

function renderExplain(x) {
  const reqs = x.requests.map((r, i) => ({...r, i}));
  const totals = {};
  for (const r of reqs) {
    for (const [k, v] of Object.entries(r.cost || {})) totals[k] = (totals[k] || 0) + v;
  }
  const byKind = {};
  for (const r of reqs) (byKind[r.kind] = byKind[r.kind] || []).push(r);
  const kinds = Object.keys(KINDS).filter(k => byKind[k]);
  const count = k => countOf(byKind[k].length, KINDS[k][0], KINDS[k][1]);
  return el('div', {class: 'explain'},
    el('h2', {}, 'Explain'),
    el('p', {},
      `${countOf(reqs.length, 'graphd request', 'graphd requests')}, `
      + `${ms(x.graphd_ms)} waiting on graphd of ${ms(x.ms)} in all: `
      + kinds.map(count).join(', ') + '. ' + (CACHE_NOTES[x.cache] || '')),
    el('div', {class: 'gq-costs'}, costChips(totals, Object.keys(COSTS))),
    kinds.map(k => {
      // The query itself is shown open; the rest folded by kind.
      const rows = byKind[k].map(r => renderRequest(r, r.i, k === 'query'));
      if (k === 'query') return rows;
      const kms = byKind[k].reduce((a, r) => a + r.ms, 0);
      return el('details', {class: 'gq-group'},
        el('summary', {}, count(k), ' ',
          el('span', {class: 'note'}, `${ms(kms)} · ${KINDS[k][2]}`)),
        rows);
    }));
}

function showQuery() {
  document.title = 'Query – MQL Browser';
  const text = el('textarea', {id: 'query-text', spellcheck: 'false'});
  text.value = JSON.stringify(pendingQuery || EXAMPLE_QUERY, null, 2);
  pendingQuery = null;
  const out = el('div', {});
  const status = el('span', {class: 'note'});
  const asof = el('input', {type: 'text', class: 'asof-input mono', size: '22',
                            placeholder: 'now', title: 'Run the query as of a time (UTC), like 2026-11-04T18:28:44'});
  const explain = el('input', {type: 'checkbox', id: 'explain'});
  explain.checked = explainQueries;
  explain.addEventListener('change', () => { explainQueries = explain.checked; });

  async function run() {
    let query;
    try {
      query = JSON.parse(text.value);
    } catch (e) {
      out.replaceChildren(el('div', {class: 'error'}, 'Not valid JSON: ' + e.message));
      return;
    }
    status.textContent = 'Running…';
    const started = performance.now();
    try {
      const params = {query: JSON.stringify(query)};
      if (explain.checked) params.explain = '1';
      if (asof.value.trim()) params.asof = asof.value.trim();
      const r = await api('/api/mqlread', params);
      status.textContent = `${Math.round(performance.now() - started)} ms`;
      out.replaceChildren(el('pre', {class: 'json'}, renderJson(r.result)),
        r.explain ? renderExplain(r.explain) : null);
    } catch (e) {
      status.textContent = '';
      out.replaceChildren(el('div', {class: 'error'}, e.message),
        e.body && e.body.explain ? renderExplain(e.body.explain) : null);
    }
  }

  text.addEventListener('keydown', e => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      run();
    }
  });
  render(
    el('h1', {}, 'MQL query'),
    text,
    el('div', {class: 'toolbar'},
      el('button', {onclick: run}, 'Run'),
      el('label', {class: 'note', for: 'explain'}, explain, ' Explain'),
      el('label', {class: 'note'}, 'As of ', asof),
      el('span', {class: 'note'}, '⌘/Ctrl + Enter'),
      status),
    out);
  text.focus();
}

// ---- home and routing ----

// Domains that are part of MQL itself, not data.
const SYSTEM_DOMAINS = new Set(['/type', '/freebase']);
const HOME_TYPES = 60;

async function showHome() {
  document.title = 'MQL Browser';
  render(el('h1', {}, 'MQL Browser'), el('p', {class: 'note'}, 'Loading…'));
  const {result} = await api('/api/mqlread', {query: JSON.stringify([{
    type: '/type/domain',
    id: null,
    name: null,
    types: [{id: null, name: null, optional: true, limit: HOME_TYPES + 1}],
  }])});
  const domains = result.filter(d => !SYSTEM_DOMAINS.has(d.id));
  render(
    el('h1', {}, 'MQL Browser'),
    el('p', {}, 'Search by name above, enter an id, or start from a type:'),
    domains.length ? domains.map(d => {
      const types = d.types.slice(0, HOME_TYPES)
        .sort((a, b) => (a.name || a.id).localeCompare(b.name || b.id));
      return el('div', {class: 'section'},
        el('div', {class: 'section-head'},
          el('h3', {}, objectLink(d)),
          el('span', {class: 'mono'}, d.id)),
        el('div', {class: 'prop'},
          el('dd', {style: 'grid-column: 1 / -1'}, el('div', {class: 'values'},
            types.map(objectLink),
            d.types.length > HOME_TYPES &&
              el('a', {href: viewHref(d.id)}, 'more…')))));
    }) : el('p', {class: 'note'}, 'This graph has no data domains yet.'));
}

async function route() {
  const full = location.hash.replace(/^#/, '') || '/';
  const q = full.indexOf('?');
  const hash = q < 0 ? full : full.slice(0, q);
  const params = new URLSearchParams(q < 0 ? '' : full.slice(q + 1));
  const [, page, ...rest] = hash.split('/');
  const arg = decodeURIComponent(rest.join('/'));
  currentAsof = page === 'view' ? params.get('asof') : null;
  main.classList.toggle('wide', page === 'graph');
  try {
    if (page === 'view' && arg) await showTopic(arg, currentAsof);
    else if (page === 'history' && arg) await showHistory(arg);
    else if (page === 'search' && arg) await showSearch(arg);
    else if (page === 'query') showQuery();
    else if (page === 'graph') await showGraph(arg || null);
    else showHome();
  } catch (e) {
    showError(e);
  }
  window.scrollTo(0, 0);
}

document.getElementById('search-form').addEventListener('submit', e => {
  e.preventDefault();
  const q = document.getElementById('search').value.trim();
  if (!q) return;
  location.hash = (q.startsWith('/') || q.startsWith('#'))
    ? viewHref(q, null) : '#/search/' + encodeURIComponent(q);
});
window.addEventListener('hashchange', route);
route();
