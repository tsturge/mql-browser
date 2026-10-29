"""A WSGI web service around MQL, for the MQL browser.

  GET /api/mqlread?query=JSON[&cursor=C]   run an MQL read
  GET /api/topic?id=ID                     everything about one object
  GET /api/search?q=TEXT                   objects whose names match
  GET /api/relations?id=ID                 ways to expand ID in a graph
  GET /api/expand?id=ID[&via=P&to=Q]       ID's neighbours in a graph

Replies are JSON. mqlread follows Freebase's API: {"code":
"/api/status/ok", "result": ..., "cursor": ...}, or on error {"code":
"/api/status/error", "messages": [...]} with HTTP status 400.

It runs under gunicorn behind nginx, which serves the browser (static/);
see run.sh. GRAPHD_ADDR (host:port) names the graphd:

  GRAPHD_ADDR=localhost:8200 gunicorn server:application
"""

import json
import os
import threading
import urllib.parse

from absl import flags

from pymql import MQLService
from pymql.mql import error
from pymql.mql import graph

# Most links of one kind (outgoing, incoming, values) shown for an object.
LINK_LIMIT = 500
# Most instances shown for a type.
INSTANCE_LIMIT = 100

# Expanding a node in the graph view follows at most this many links
# into it (papers by an author, say), and returns this many neighbours.
EXPAND_VIA_LIMIT = 1000
EXPAND_NODE_LIMIT = 200

# Links shown elsewhere on the page, or not interesting.
HIDDEN_LINKS = {
    '/type/object/type',
    '/type/object/name',
    '/type/object/permission',
    '/type/namespace/keys',
}


class BadRequest(Exception):
  pass


class NotFound(Exception):
  pass


class Graph(object):
  """MQL reads against one graphd, one at a time."""

  def __init__(self, addr):
    host, port = addr.rsplit(':', 1)
    self.connector = graph.TcpGraphConnector(addrs=[(host, int(port))])
    self.lock = threading.Lock()

  def read(self, query, cursor=None):
    env = {'escape': False}
    if cursor is not None:
      # As in Freebase's API, an empty cursor starts paging; pymql wants
      # True for that.
      env['cursor'] = True if cursor in ('', 'true') else cursor
    with self.lock:
      # A new service per read: MQLService caches namespace contents and
      # would not see changes made by other clients.
      r = MQLService(connector=self.connector).read(query, **env)
    return r.result, r.cursor

  def topic(self, id):
    """The object, its types, and its links grouped by property."""
    obj, _ = self.read({
        'id': id,
        'guid': None,
        'name': None,
        'timestamp': None,
        'type': [{'id': None, 'name': None, 'optional': True}],
        'key': [{'namespace': None, 'value': None, 'optional': True}],
    })
    if obj is None:
      raise NotFound('no object with id %s' % id)

    def reflect(prop, clause):
      clause = dict(clause, link=None, limit=LINK_LIMIT, optional=True)
      result, _ = self.read({'id': id, prop: [clause]})
      links = [l for l in result[prop] if l['link'] not in HIDDEN_LINKS]
      return links, len(result[prop]) == LINK_LIMIT

    outgoing, more_out = reflect('/type/reflect/any_master',
                                 {'id': None, 'name': None})
    incoming, more_in = reflect('/type/reflect/any_reverse',
                                {'id': None, 'name': None})
    values, more_values = reflect('/type/reflect/any_value',
                                  {'value': None, 'type': None})

    props = self.properties({l['link'] for l in outgoing + incoming + values})
    type_order = [t['id'] for t in obj['type']]

    def group(links, make_value):
      by_prop = {}
      for l in links:
        by_prop.setdefault(l['link'], []).append(make_value(l))
      sections = {}
      for pid, vals in by_prop.items():
        p = props.get(pid) or {'id': pid, 'name': pid, 'schema': None}
        schema = p['schema'] or {'id': None, 'name': 'Other'}
        section = sections.setdefault(schema['id'], {
            'type': schema,
            'properties': [],
        })
        section['properties'].append(dict(p, values=vals))
      for s in sections.values():
        s['properties'].sort(key=lambda p: (p.get('name') or '').lower())
      # The object's own types first, in its order; then the rest.
      return sorted(
          sections.values(),
          key=lambda s: (s['type']['id'] not in type_order,
                         type_order.index(s['type']['id'])
                         if s['type']['id'] in type_order else 0,
                         (s['type']['name'] or '').lower()))

    link = lambda l: {'id': l['id'], 'name': l['name']}
    value = lambda l: {'value': l['value'], 'type': l['type']}
    topic = {
        'id': obj['id'],
        'guid': obj['guid'],
        'name': obj['name'],
        'timestamp': obj['timestamp'],
        'types': obj['type'],
        'keys': obj['key'],
        'properties': group(values + outgoing,
                            lambda l: link(l) if 'id' in l else value(l)),
        'incoming': group(incoming, link),
        'truncated': {
            'outgoing': more_out,
            'incoming': more_in,
            'values': more_values,
        },
    }
    if '/type/type' in type_order:
      topic['type_info'] = self.type_info(id)
    return topic

  def properties(self, ids):
    """Schema information for property ids, by id."""
    if not ids:
      return {}
    result, _ = self.read([{
        'id|=': sorted(ids),
        'id': None,
        'type': '/type/property',
        'name': None,
        'unique': None,
        'expected_type': None,
        'schema': {'id': None, 'name': None, 'optional': True},
        'limit': len(ids),
    }])
    return {p['id']: p for p in result}

  def type_info(self, id):
    """A type's properties and some of its instances."""
    props, _ = self.read([{
        'type': '/type/property',
        'schema': {'id': id},
        'id': None,
        'name': None,
        'unique': None,
        'expected_type': {'id': None, 'name': None, 'optional': True},
        'sort': 'name',
        'limit': 1000,
    }])
    instances, _ = self.read([{
        'type': id,
        'id': None,
        'name': None,
        'limit': INSTANCE_LIMIT,
    }])
    count, _ = self.read({'type': id, 'return': 'estimate-count'})
    return {
        'properties': props,
        'instances': instances,
        'instance_count': count,
    }

  def node(self, id):
    """An object as a graph node: id, name and type ids."""
    obj, _ = self.read({'id': id, 'name': None, 'type': []})
    if obj is None:
      raise NotFound('no object with id %s' % id)
    return {'id': obj['id'], 'name': obj['name'], 'types': obj['type']}

  def relations(self, id):
    """The ways to expand id in the graph view.

    Its direct links; and, for each property P linking objects M to id
    (/dblp/publication/author: papers by an author), each property Q of
    P's type linking M to other objects (author again: co-authors; or
    part_of: the proceedings the author's papers are in). Q's targets are
    connected to id through the Ms, weighted by how many.
    """
    self.node(id)
    incoming, _ = self.read({'id': id, '/type/reflect/any_reverse': [{
        'link': None, 'limit': LINK_LIMIT, 'optional': True}]})
    counts = {}
    for l in incoming['/type/reflect/any_reverse']:
      if l['link'] not in HIDDEN_LINKS:
        counts[l['link']] = counts.get(l['link'], 0) + 1
    props = self.properties(set(counts))

    relations = [{'label': 'Direct links', 'via': None, 'to': None}]
    for via in sorted(counts, key=lambda p: -counts[p]):
      p = props.get(via)
      if not p or not p['schema']:
        continue
      targets, _ = self.read([{
          'type': '/type/property',
          'schema': {'id': p['schema']['id']},
          'id': None,
          'name': None,
          'unique': None,
          'expected_type': {'id': None, 'optional': True},
          'limit': 1000,
      }])
      for q in sorted(targets, key=lambda q: q['id'] != via):
        expected = q['expected_type'] and q['expected_type']['id']
        if not expected or expected.startswith('/type/'):
          continue  # a value, not an object
        relations.append({
            'label': '%s of, then %s' % (p['name'], q['name']),
            'via': via,
            'to': q['id'],
            'unique': bool(q['unique']),
            'count': counts[via],
        })
    return {'relations': relations}

  def expand(self, id, via=None, to=None, limit=EXPAND_NODE_LIMIT):
    """id's neighbours, as {"center", "nodes", "edges"}.

    Without via, its direct links, each an edge labelled by property and
    pointing the way the link does ("directed": true). With via and to,
    the objects reached through objects M that link to id by via and from
    M to them by to; an edge's weight is the number of Ms.
    """
    center = self.node(id)
    nodes, edges = {}, {}

    def add(n, label, incoming=False):
      if n['id'] == id:
        return
      nodes[n['id']] = {'id': n['id'], 'name': n['name'],
                        'types': n.get('type', [])}
      e = edges.setdefault((n['id'], incoming), {
          'from': n['id'] if incoming else id,
          'to': id if incoming else n['id'],
          'weight': 0,
          'labels': set(),
          'directed': not via,
      })
      e['weight'] += 1
      e['labels'].add(label)

    if not via:
      link = {'link': None, 'id': None, 'name': None, 'type': [],
              'limit': LINK_LIMIT, 'optional': True}
      result, _ = self.read({'id': id,
                             '/type/reflect/any_master': [link],
                             '/type/reflect/any_reverse': [dict(link)]})
      links = ([(l, False) for l in result['/type/reflect/any_master']] +
               [(l, True) for l in result['/type/reflect/any_reverse']])
      links = [(l, i) for l, i in links if l['link'] not in HIDDEN_LINKS]
      names = self.properties({l['link'] for l, _ in links})
      for l, incoming in links:
        p = names.get(l['link'])
        add(l, p['name'] if p else l['link'], incoming)
      middles = 0
    else:
      if not to:
        raise BadRequest('expand with via also needs to')
      result, _ = self.read({'id': id, '!' + via: [{
          'id': None,
          to: [{'id': None, 'name': None, 'type': [], 'optional': True,
                'limit': EXPAND_NODE_LIMIT}],
          'limit': EXPAND_VIA_LIMIT,
          'optional': True,
      }]})
      middles = result['!' + via]
      names = self.properties({to})
      label = names[to]['name'] if to in names else to
      for m in middles:
        for n in m[to]:
          add(n, label)
      middles = len(middles)

    weight = lambda n: sum(e['weight'] for (m, _), e in edges.items()
                           if m == n)
    chosen = set(sorted(nodes, key=lambda n: -weight(n))[:limit])
    return {
        'center': center,
        'nodes': [nodes[n] for n in nodes if n in chosen],
        'edges': [dict(e, labels=sorted(e['labels']))
                  for (n, _), e in edges.items() if n in chosen],
        'via_count': middles,
        'truncated': len(nodes) > limit or middles == EXPAND_VIA_LIMIT,
    }

  def search(self, text):
    words = text.split()
    if not words:
      return []
    # ~= matches whole words; let the last word be a prefix.
    pattern = ' '.join(words[:-1] + [words[-1] + '*'])
    result, _ = self.read([{
        'name~=': pattern,
        'id': None,
        'name': None,
        'type': [{'id': None, 'name': None, 'optional': True, 'limit': 5}],
        'limit': 50,
    }])
    return result


_graph = None
_graph_lock = threading.Lock()


def get_graph():
  """The process's Graph, made on first use (after gunicorn forks)."""
  global _graph
  with _graph_lock:
    if _graph is None:
      # pymql reads its settings from absl flags, which are normally
      # parsed by absl's app.run(); under a WSGI server, use defaults.
      if not flags.FLAGS.is_parsed():
        flags.FLAGS(['server'])
      addr = os.environ.get('GRAPHD_ADDR')
      if not addr:
        raise RuntimeError('GRAPHD_ADDR is not set')
      _graph = Graph(addr)
    return _graph


def required(params, name):
  if name not in params:
    raise BadRequest('missing parameter: %s' % name)
  return params[name]


def mqlread(params):
  try:
    query = json.loads(required(params, 'query'))
  except ValueError as e:
    raise BadRequest('query is not valid JSON: %s' % e)
  result, cursor = get_graph().read(query, params.get('cursor'))
  return {
      'code': '/api/status/ok',
      'result': result,
      'cursor': cursor,
  }


def topic(params):
  return get_graph().topic(required(params, 'id'))


def search(params):
  return {'result': get_graph().search(params.get('q', ''))}


def relations(params):
  return get_graph().relations(required(params, 'id'))


def expand(params):
  try:
    limit = int(params.get('limit', EXPAND_NODE_LIMIT))
  except ValueError:
    raise BadRequest('limit must be a number')
  return get_graph().expand(required(params, 'id'), params.get('via') or None,
                            params.get('to') or None,
                            max(1, min(limit, EXPAND_NODE_LIMIT)))


ROUTES = {
    '/api/mqlread': mqlread,
    '/api/topic': topic,
    '/api/search': search,
    '/api/relations': relations,
    '/api/expand': expand,
}

STATUS_TEXT = {
    200: 'OK',
    400: 'Bad Request',
    404: 'Not Found',
    405: 'Method Not Allowed',
}


def error_reply(message):
  return {'code': '/api/status/error', 'messages': [{'message': message}]}


def application(environ, start_response):
  """The WSGI entry point."""
  path = environ.get('PATH_INFO', '')
  # keep_blank_values: "cursor=" (start paging) must not be dropped.
  params = {
      k: v[-1] for k, v in urllib.parse.parse_qs(
          environ.get('QUERY_STRING', ''), keep_blank_values=True).items()
  }
  try:
    if environ['REQUEST_METHOD'] not in ('GET', 'HEAD'):
      status, body = 405, error_reply('only GET is supported')
    elif path not in ROUTES:
      raise NotFound('no such API: %s' % path)
    else:
      status, body = 200, ROUTES[path](params)
  except BadRequest as e:
    status, body = 400, error_reply(str(e))
  except NotFound as e:
    status, body = 404, error_reply(str(e))
  except error.MQLError as e:
    status, body = 400, {
        'code': '/api/status/error',
        'messages': [dict(e.error, message=str(e))],
    }
  data = json.dumps(body, indent=1, ensure_ascii=False, default=str).encode()
  start_response('%d %s' % (status, STATUS_TEXT[status]), [
      ('Content-Type', 'application/json; charset=utf-8'),
      ('Content-Length', str(len(data))),
  ])
  if environ['REQUEST_METHOD'] == 'HEAD':
    return [b'']
  return [data]
