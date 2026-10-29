// MQL Browser: a single page over /api/topic, /api/search and
// /api/mqlread. Routes live in the URL hash:
//   #/view/<id>     an object
//   #/search/<text> name search
//   #/query         MQL query editor
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
  for (const c of children.flat()) {
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

function viewHref(id) { return '#/view/' + encodeURIComponent(id); }

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
    throw new Error(msgs || `${resp.status} ${resp.statusText}`);
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

async function showTopic(id) {
  loading();
  const t = await api('/api/topic', {id});
  document.title = (t.name || t.id) + ' – MQL Browser';
  const truncated = Object.entries(t.truncated).filter(([, v]) => v).map(([k]) => k);
  render(
    el('h1', {}, t.name || t.id),
    el('div', {class: 'ids mono'},
      el('span', {}, t.id),
      el('span', {}, t.guid),
      t.timestamp && el('span', {}, t.timestamp)),
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

function showQuery() {
  document.title = 'Query – MQL Browser';
  const text = el('textarea', {id: 'query-text', spellcheck: 'false'});
  text.value = JSON.stringify(pendingQuery || EXAMPLE_QUERY, null, 2);
  pendingQuery = null;
  const out = el('div', {});
  const status = el('span', {class: 'note'});

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
      const r = await api('/api/mqlread', {query: JSON.stringify(query)});
      status.textContent = `${Math.round(performance.now() - started)} ms`;
      out.replaceChildren(el('pre', {class: 'json'}, renderJson(r.result)));
    } catch (e) {
      status.textContent = '';
      out.replaceChildren(el('div', {class: 'error'}, e.message));
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
  const hash = location.hash.replace(/^#/, '') || '/';
  const [, page, ...rest] = hash.split('/');
  const arg = decodeURIComponent(rest.join('/'));
  try {
    if (page === 'view' && arg) await showTopic(arg);
    else if (page === 'search' && arg) await showSearch(arg);
    else if (page === 'query') showQuery();
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
    ? viewHref(q) : '#/search/' + encodeURIComponent(q);
});
window.addEventListener('hashchange', route);
route();
