// MQL Browser: the graph view, #/graph/<id>.
//
// Objects are nodes; edges come from /api/expand. Expanding a node adds
// its neighbours by one of its relations (/api/relations): its direct
// links, or objects reached through the objects linking to it, such as
// an author's co-authors (Author of, then Author), weighted by the
// number of objects in between. Uses el(), api(), render() and
// viewHref() from app.js, and vis-network, loaded on first use.
'use strict';

const VIS_SRC = '/vendor/vis-network.min.js';
const GRAPH_EXPAND_LIMIT = 40;
// Categorical colours, told apart in both light and dark themes.
const TYPE_COLORS = ['#4e79a7', '#f28e2b', '#59a14f', '#e15759', '#76b7b2',
                     '#edc948', '#b07aa1', '#ff9da7', '#9c755f', '#bab0ac'];

let visLoading = null;

function loadVis() {
  if (window.vis) return Promise.resolve();
  if (!visLoading) {
    visLoading = new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = VIS_SRC;
      s.onload = resolve;
      s.onerror = () => reject(new Error('could not load ' + VIS_SRC));
      document.head.append(s);
    });
  }
  return visLoading;
}

function graphHref(id) { return '#/graph/' + encodeURIComponent(id); }

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// The type a node is coloured by: its last (most specific) type.
function mainType(types) {
  return types && types.length ? types[types.length - 1] : null;
}

async function showGraph(startId) {
  document.title = 'Graph – MQL Browser';
  render(el('p', {class: 'note'}, 'Loading graph…'));
  await loadVis();

  const nodes = new vis.DataSet();
  const edges = new vis.DataSet();
  const typeColor = new Map();
  const typeNames = new Map();
  const expanded = new Map();  // node id -> ids of the nodes it added
  const relationsOf = new Map();  // node id -> its relations
  let minWeight = 1;
  let selected = null;

  // The page.
  const canvas = el('div', {class: 'graph-canvas'});
  const status = el('span', {class: 'note'});
  const relSelect = el('select', {class: 'graph-select', title: 'How to expand the selected node'});
  const weightInput = el('input', {type: 'range', min: '1', max: '1', value: '1'});
  const weightLabel = el('span', {class: 'mono'}, '1');
  const addInput = el('input', {type: 'search', class: 'graph-add',
                                placeholder: 'Add a node: name or id'});
  const panel = el('aside', {class: 'graph-panel'});
  const legend = el('div', {class: 'graph-legend'});
  const button = (label, onclick, title) => el('button', {onclick, title, class: 'secondary'}, label);

  render(
    el('div', {class: 'graph-toolbar'},
      el('form', {class: 'graph-form', onsubmit: e => { e.preventDefault(); addByText(); }},
        addInput, el('button', {type: 'submit'}, 'Add')),
      el('span', {class: 'graph-group'},
        relSelect,
        button('Expand', () => selected && expand(selected, relSelect.value),
               'Add the selected node\'s neighbours (or double-click a node)'),
        button('Delete', () => selected && removeNode(selected), 'Remove the selected node'),
        button('Cluster', () => selected && toggleCluster(selected),
               'Collapse the selected node\'s neighbours into one node, or open a cluster'),
        button('Clear', clear, 'Remove every node')),
      el('label', {class: 'graph-group', title: 'Hide edges lighter than this'},
        'Min edge weight ', weightInput, weightLabel),
      status),
    el('div', {class: 'graph-body'}, canvas, panel));

  const edgeView = new vis.DataView(edges, {filter: e => e.weight >= minWeight});
  const fg = cssVar('--fg'), bg = cssVar('--bg'), muted = cssVar('--muted');
  const network = new vis.Network(canvas, {nodes, edges: edgeView}, {
    nodes: {
      shape: 'dot',
      scaling: {min: 8, max: 30, label: {enabled: false}},
      font: {color: fg, size: 13, strokeWidth: 3, strokeColor: bg},
      borderWidth: 1,
    },
    edges: {
      color: {color: muted, highlight: cssVar('--accent'), opacity: 0.6},
      scaling: {min: 1, max: 8, label: {enabled: false}},
      font: {color: muted, size: 11, strokeWidth: 3, strokeColor: bg, align: 'middle'},
      smooth: false,
      selectionWidth: 1.5,
    },
    physics: {
      solver: 'forceAtlas2Based',
      forceAtlas2Based: {gravitationalConstant: -60, springLength: 120},
      stabilization: {iterations: 150},
    },
    interaction: {hover: true, tooltipDelay: 150, multiselect: false},
  });

  function colorFor(type) {
    if (!type) return muted;
    if (!typeColor.has(type)) typeColor.set(type, TYPE_COLORS[typeColor.size % TYPE_COLORS.length]);
    return typeColor.get(type);
  }

  function addNode(n) {
    const type = mainType(n.types);
    const label = (n.name || n.id).length > 40 ? (n.name || n.id).slice(0, 38) + '…' : (n.name || n.id);
    const existing = nodes.get(n.id);
    const node = {
      id: n.id, label, title: `${n.name || ''}\n${n.id}`, types: n.types,
      color: {background: colorFor(type), border: colorFor(type),
              highlight: {background: colorFor(type), border: fg}},
    };
    if (existing) nodes.update(node);
    else nodes.add(node);
    if (type && !typeNames.has(type)) typeNames.set(type, type);
  }

  function addEdge(e) {
    const id = [e.from, e.to].sort().join('|') + '|' + e.labels.join(',') + (e.directed ? '>' : '');
    const old = edges.get(id);
    const weight = Math.max(e.weight, old ? old.weight : 0);
    edges.update({
      // The width shows the weight; the tooltip says it.
      id, from: e.from, to: e.to, weight, value: weight,
      title: `${e.labels.join(', ')}${weight > 1 ? `: ${weight}` : ''}`,
      arrows: e.directed ? {to: {enabled: true, scaleFactor: 0.5}} : undefined,
    });
  }

  function refreshNodeSizes() {
    const degree = new Map();
    edgeView.get().forEach(e => {
      degree.set(e.from, (degree.get(e.from) || 0) + 1);
      degree.set(e.to, (degree.get(e.to) || 0) + 1);
    });
    nodes.update(nodes.get().map(n => ({
      id: n.id, value: (degree.get(n.id) || 0) + 1,
      borderWidth: expanded.has(n.id) ? 3 : 1,
    })));
    const max = Math.max(1, ...edges.get().map(e => e.weight));
    weightInput.max = String(max);
    status.textContent = `${nodes.length} nodes, ${edgeView.length} edges`;
    renderLegend();
  }

  async function relations(id) {
    if (!relationsOf.has(id)) relationsOf.set(id, (await api('/api/relations', {id})).relations);
    return relationsOf.get(id);
  }

  // The relation used by double-click: a "co-" relation (Author of, then
  // Author: co-authors) if there is one, else direct links. Not through a
  // unique property (Part of, then Part of only leads back to the node).
  function defaultRelation(rels) {
    const co = rels.find(r => r.via && r.via === r.to && !r.unique);
    return co || rels[0];
  }

  function relationValue(r) { return JSON.stringify([r.via, r.to]); }

  async function expand(id, value) {
    const rels = await relations(id);
    const r = value ? rels.find(r => relationValue(r) === value) : defaultRelation(rels);
    status.textContent = `Expanding ${nodes.get(id)?.label || id}…`;
    try {
      const params = {id, limit: GRAPH_EXPAND_LIMIT};
      if (r && r.via) Object.assign(params, {via: r.via, to: r.to});
      const result = await api('/api/expand', params);
      addNode(result.center);
      const added = expanded.get(id) || new Set();
      for (const n of result.nodes) {
        if (!nodes.get(n.id)) added.add(n.id);
        addNode(n);
      }
      result.edges.forEach(addEdge);
      expanded.set(id, added);
      refreshNodeSizes();
      if (result.truncated) {
        status.textContent += ` (showing the ${GRAPH_EXPAND_LIMIT} strongest)`;
      }
    } catch (e) {
      status.textContent = e.message;
    }
  }

  // Contract: remove the nodes this one added that nothing else links to.
  function contract(id) {
    const added = expanded.get(id) || new Set();
    for (const n of added) {
      const others = edges.get().filter(e => (e.from === n || e.to === n) &&
                                             e.from !== id && e.to !== id);
      if (!others.length && !expanded.has(n)) removeNode(n, false);
    }
    expanded.delete(id);
    refreshNodeSizes();
  }

  function removeNode(id, refresh = true) {
    if (network.isCluster(id)) network.openCluster(id);
    edges.remove(edges.get().filter(e => e.from === id || e.to === id).map(e => e.id));
    nodes.remove(id);
    expanded.delete(id);
    if (selected === id) select(null);
    if (refresh) refreshNodeSizes();
  }

  function toggleCluster(id) {
    if (network.isCluster(id)) {
      network.openCluster(id);
    } else {
      network.clusterByConnection(id, {
        clusterNodeProperties: {
          label: `${nodes.get(id)?.label || id} +${network.getConnectedNodes(id).length}`,
          shape: 'dot', borderWidth: 3, color: nodes.get(id)?.color,
        },
      });
    }
    select(null);
  }

  function clear() {
    nodes.clear();
    edges.clear();
    expanded.clear();
    select(null);
    refreshNodeSizes();
  }

  async function addByText() {
    const text = addInput.value.trim();
    if (!text) return;
    let id = text;
    if (!text.startsWith('/') && !text.startsWith('#')) {
      const {result} = await api('/api/search', {q: text});
      if (!result.length) {
        status.textContent = `No match for "${text}"`;
        return;
      }
      id = result[0].id;
    }
    addInput.value = '';
    await expand(id);
    select(id);
    network.focus(id, {scale: 1, animation: true});
  }

  async function select(id) {
    selected = id;
    relSelect.replaceChildren();
    if (!id || network.isCluster(id)) {
      panel.replaceChildren(
        el('p', {class: 'note'},
          id ? 'A cluster: click Cluster to open it.'
             : 'Click a node to select it; double-click to expand or contract it.'),
        legend);
      return;
    }
    const n = nodes.get(id);
    panel.replaceChildren(
      el('h3', {}, n.title.split('\n')[0] || id),
      el('div', {class: 'mono note graph-id'}, id),
      el('p', {}, el('a', {href: viewHref(id)}, 'Open page')),
      el('p', {class: 'note'}, 'Loading relations…'),
      legend);
    try {
      const rels = await relations(id);
      if (selected !== id) return;
      const def = defaultRelation(rels);
      for (const r of rels) {
        const label = r.count ? `${r.label} (${r.count})` : r.label;
        relSelect.append(el('option', {value: relationValue(r),
                                       selected: r === def ? 'selected' : null}, label));
      }
      panel.children[3].replaceWith(el('p', {class: 'note'},
        expanded.has(id) ? 'Expanded. Double-click to contract.'
                         : 'Double-click to expand by the selected relation.'));
    } catch (e) {
      panel.children[3].textContent = e.message;
    }
  }

  function renderLegend() {
    const shown = new Set(nodes.get().map(n => mainType(n.types)).filter(Boolean));
    legend.replaceChildren(...[
      shown.size ? el('h4', {}, 'Types') : null,
      ...[...shown].sort().map(t => el('div', {class: 'graph-legend-row'},
        el('span', {class: 'graph-swatch', style: `background:${colorFor(t)}`}),
        el('a', {href: viewHref(t), class: 'mono'}, t))),
    ].filter(Boolean));
  }

  network.on('click', p => select(p.nodes.length ? p.nodes[0] : null));
  network.on('doubleClick', p => {
    if (!p.nodes.length) return;
    const id = p.nodes[0];
    if (network.isCluster(id)) network.openCluster(id);
    else if (expanded.has(id)) contract(id);
    else expand(id, id === selected ? relSelect.value : undefined);
  });
  weightInput.addEventListener('input', () => {
    minWeight = Number(weightInput.value);
    weightLabel.textContent = weightInput.value;
    edgeView.refresh();
    refreshNodeSizes();
  });

  select(null);
  if (startId) {
    await expand(startId);
    await select(startId);
  } else {
    status.textContent = 'Add a node to start.';
  }
}
