"""A WSGI web service around MQL, for the MQL browser.

  GET /api/mqlread?query=JSON[&cursor=C][&explain=1][&asof=T]
                                           run an MQL read
  GET /api/topic?id=ID[&asof=T]            everything about one object
  GET /api/history?id=ID                   changes to an object's links
  GET /api/search?q=TEXT                   objects whose names match
  GET /api/relations?id=ID                 ways to expand ID in a graph
  GET /api/expand?id=ID[&via=P&to=Q]       ID's neighbours in a graph
  GET /api/path?a=ID&b=ID[&via=P&to=Q]     a shortest path between two

Replies are JSON. mqlread follows Freebase's API: {"code":
"/api/status/ok", "result": ..., "cursor": ...}, or on error {"code":
"/api/status/error", "messages": [...]} with HTTP status 400.
asof=T reads the graph as it was at timestamp T (UTC, like
2026-11-04T18:28:44; graphd keeps every version of everything).
With explain=1, the reply (an error too) also has "explain": the
graphd requests the read made, with their costs; see Trace.

It runs under gunicorn behind nginx, which serves the browser (static/);
see run.sh. GRAPHD_ADDR (host:port) names the graphd:

  GRAPHD_ADDR=localhost:8200 gunicorn server:application
"""

import json
import os
import re
import socket
import threading
import time
import traceback
import urllib.parse

from absl import flags

from pymql import MQLService
from pymql.mql import error
from pymql.mql import graph
from pymql.mql.grparse import coststr_to_dict

# Most links of one kind (outgoing, incoming, values) shown for an object.
LINK_LIMIT = 500
# Most instances shown for a type.
INSTANCE_LIMIT = 100

# Expanding a node in the graph view follows at most this many links
# into it (papers by an author, say), and returns this many neighbours.
EXPAND_VIA_LIMIT = 1000
EXPAND_NODE_LIMIT = 200

# Most changes to an object's links shown, in each direction.
HISTORY_LIMIT = 500

# Links shown elsewhere on the page, or not interesting.
HIDDEN_LINKS = {
    '/type/object/type',
    '/type/object/name',
    '/type/object/permission',
    '/type/namespace/keys',
}


# Shortest paths: the search gives up past this many steps, objects
# reached, or seconds.
PATH_MAX_STEPS = 10
PATH_MAX_VISITED = 2000000
PATH_SECONDS = 60
# graphd returns at most this many results to a read, silently dropping
# the rest; the path search splits reads that reach it.
GRAPHD_RESULT_CAP = 65536
# Objects per graphd read in the path search, and the page size asked
# for (more than graphd gives).
PATH_CHUNK = 2000
PATH_PAGESIZE = 1000000


class BadRequest(Exception):
  pass


class NotFound(Exception):
  pass


class ExplainedError(Exception):
  """An MQL error, with the Trace of the read that raised it."""

  def __init__(self, mql_error, trace):
    super().__init__(str(mql_error))
    self.mql_error = mql_error
    self.trace = trace


class Trace(object):
  """The graphd requests one MQL read makes, for explain=1.

  cache says whether the read used the cached schema and ids ("kept"),
  or started afresh ("new", or "flushed" because graphd had changed), or
  did not use the cache ("bypassed": reads of the past never do).
  Each request is {"kind", "query", "ms", "cost", "error"}: kind says
  what it was for ("query" is the read itself; "schema" loads types and
  properties; "id lookup" resolves ids and namespace keys; "dateline"
  checks whether graphd has changed);
  query is the graphd request; ms the time to its reply; cost graphd's
  account of the work (see the "cost" reply in graphd's
  doc/gr-protocol.txt).
  """

  def __init__(self):
    self.requests = []
    self.cache = None
    self.start = time.time()
    self.ms = None

  def finish(self):
    self.ms = (time.time() - self.start) * 1000

  def as_json(self):
    graphd_ms = sum(r['ms'] for r in self.requests)
    return {
        'ms': self.ms,
        'graphd_ms': graphd_ms,
        'cache': self.cache,
        'requests': self.requests,
    }


def request_kind(msg, stack):
  """What a graphd request was for, from it and the stack sending it."""
  if msg.startswith('status '):
    return 'dateline'
  modules = set()
  for frame in stack:
    path = frame.filename.replace(os.sep, '/')
    if '/pymql/' not in path:
      continue
    module = path.rsplit('/', 1)[-1]
    if module == 'hijson.py' and frame.name == 'graph_read':
      return 'query'
    modules.add(module)
  if 'schema.py' in modules:
    return 'schema'
  if modules & {'lookup.py', 'namespace.py'}:
    return 'id lookup'
  return 'other'


class TracingConnector(graph.TcpGraphConnector):
  """A graphd connection that records requests while trace is set."""

  trace = None

  def transmit_query(self, msg, policy, deadline, **kwargs):
    trace = self.trace
    if trace is None:
      return super().transmit_query(msg, policy, deadline, **kwargs)
    entry = {
        'kind': request_kind(msg, traceback.extract_stack()),
        'query': msg,
        'cost': None,
        'error': None,
    }
    trace.requests.append(entry)
    start = time.time()
    try:
      result = super().transmit_query(msg, policy, deadline, **kwargs)
      entry['cost'] = coststr_to_dict(result.cost)
      return result
    except error.MQLError as e:
      entry['error'] = str(e)
      entry['cost'] = coststr_to_dict(getattr(e, 'cost', None))
      raise
    finally:
      entry['ms'] = (time.time() - start) * 1000


class RawGraphd(object):
  """A connection for flat graphd reads that return only guids.

  The path search reads hundreds of thousands of links; through MQL,
  building its nested results would take most of the time. This sends
  graphd reads directly and pulls the guids out of the reply.
  """

  ROW = re.compile(r'\(([0-9a-f]{32}(?: (?:[0-9a-f]{32}|null))*)\)')

  def __init__(self, addr, deadline):
    host, port = addr.rsplit(':', 1)
    self.deadline = deadline
    self.sock = socket.create_connection((host, int(port)), timeout=10)
    self.file = self.sock.makefile('rb')

  def close(self):
    self.file.close()
    self.sock.close()

  def read(self, constraint):
    """Rows of guids (or None) from read <constraint>."""
    left = self.deadline - time.time()
    if left <= 0:
      raise PathGiveUp('took more than %d seconds' % PATH_SECONDS)
    self.sock.settimeout(left + 5)
    self.sock.sendall(('read cost="tu=%d" %s\n' % (left * 1000, constraint))
                      .encode())
    line = self.file.readline().decode()
    if line.startswith('error EMPTY'):
      return []
    if line.startswith('error TOOHARD'):
      raise PathGiveUp('took more than %d seconds' % PATH_SECONDS)
    if not line.startswith('ok '):
      raise RuntimeError('graphd: %s' % line.strip()[:500])
    return [[None if g == 'null' else g for g in row.split(' ')]
            for row in self.ROW.findall(line)]

  def read_for(self, guids, make_constraint):
    """read() for each chunk of guids; make_constraint(guid list)."""
    rows = []
    pending = [guids[i:i + PATH_CHUNK]
               for i in range(0, len(guids), PATH_CHUNK)]
    while pending:
      chunk = pending.pop()
      result = self.read(make_constraint('(%s)' % ' '.join(chunk)))
      if len(result) >= GRAPHD_RESULT_CAP and len(chunk) > 1:
        # graphd dropped some: ask again in halves.
        pending += [chunk[:len(chunk) // 2], chunk[len(chunk) // 2:]]
      else:
        rows += result
    return rows


class PathGiveUp(Exception):
  pass


class Graph(object):
  """MQL reads against one graphd, one at a time."""

  def __init__(self, addr):
    host, port = addr.rsplit(':', 1)
    self.addr = addr
    self.connector = TracingConnector(addrs=[(host, int(port))])
    self.lock = threading.Lock()
    # One MQLService, kept while graphd is unchanged: it caches schema
    # (types and properties) and namespaces (ids), which otherwise cost
    # dozens of graphd requests a read. graphd's dateline changes with
    # every write, so a new dateline means a new service and empty
    # caches; reads never see stale ids or schema.
    self.service = None
    self.dateline = None

  def current_dateline(self):
    """graphd's dateline: an odometer that every write advances."""
    r = self.connector.transmit_query(
        'status cost="" dateline="" ()', self.connector._get_policy(), None)
    return r.dateline

  def mql_service(self, trace):
    """The service for a read, new if graphd has changed since the last."""
    dateline = self.current_dateline()
    if self.service is None or dateline != self.dateline:
      if trace:
        trace.cache = 'new' if self.service is None else 'flushed'
      self.service = MQLService(connector=self.connector)
      self.dateline = dateline
    elif trace:
      trace.cache = 'kept'
    return self.service

  def read(self, query, cursor=None, trace=None, asof=None):
    """Runs an MQL read; with a Trace, records its graphd requests.

    With asof, a timestamp, reads the graph as it was then.
    """
    env = {'escape': False}
    if asof:
      env['as_of_time'] = asof
    if cursor is not None:
      # As in Freebase's API, an empty cursor starts paging; pymql wants
      # True for that.
      env['cursor'] = True if cursor in ('', 'true') else cursor
    with self.lock:
      self.connector.trace = trace
      try:
        if asof:
          # Not the cached service: the past's namespaces and schema
          # would be cached in it, and seen by reads of the present.
          service = MQLService(connector=self.connector)
          if trace:
            trace.cache = 'bypassed'
        else:
          service = self.mql_service(trace)
        r = service.read(query, **env)
      except error.MQLError:
        raise
      except Exception:
        # Something unexpected: don't trust the service's caches.
        self.service = None
        raise
      finally:
        self.connector.trace = None
        if trace:
          trace.finish()
    return r.result, r.cursor

  def topic(self, id, asof=None):
    """The object, its types, and its links grouped by property.

    With asof, a timestamp, as they were then.
    """
    read = lambda q: self.read(q, asof=asof)
    obj, _ = read({
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
      result, _ = read({'id': id, prop: [clause]})
      links = [l for l in result[prop] if l['link'] not in HIDDEN_LINKS]
      return links, len(result[prop]) == LINK_LIMIT

    outgoing, more_out = reflect('/type/reflect/any_master',
                                 {'id': None, 'name': None})
    incoming, more_in = reflect('/type/reflect/any_reverse',
                                {'id': None, 'name': None})
    values, more_values = reflect('/type/reflect/any_value',
                                  {'value': None, 'type': None})

    props = self.properties({l['link'] for l in outgoing + incoming + values},
                            asof)
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
        'asof': asof,
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
      topic['type_info'] = self.type_info(id, asof)
    return topic

  def history(self, id, limit=HISTORY_LIMIT):
    """Every change to the links from and to an object, newest first.

    graphd never overwrites anything: a change writes a new version of
    a link, and a deletion a version that is no longer live. MQL's
    /type/link "operation" and "valid" reach those versions. Each change
    is {"timestamp", "operation" ("insert", "update" or "delete"),
    "valid" (whether it is still current), "direction" ("out" for the
    object's own properties, "in" for links to it), "property", "object"
    (the object at the other end, if any), "value" (a literal value or
    a key, if any), "creator", "previous" (for an update, the object
    and value it replaced, when that is shown too)}. The object's
    creation is the last change, with operation "create".
    """
    obj, _ = self.read({'id': id, 'name': None, 'timestamp': None,
                        'creator': None})
    if obj is None:
      raise NotFound('no object with id %s' % id)
    clause = {
        'type': '/type/link',
        'operation': None,
        'valid': None,
        'timestamp': None,
        'creator': None,
        'master_property': {'id': None, 'name': None, 'unique': None},
        'target_value': None,
        'sort': '-timestamp',
        'limit': limit,
    }
    end = {'id': None, 'name': None}
    outgoing, _ = self.read([dict(clause, source={'id': id},
                                  target=dict(end, optional=True))])
    incoming, _ = self.read([dict(clause, target={'id': id}, source=end)])

    def change(l, direction, other):
      # Text values are links to their language: show just the text.
      if other and l['target_value'] is not None and (
          other['id'] or '').startswith('/lang/'):
        other = None
      return {
          'timestamp': l['timestamp'],
          'operation': l['operation'],
          'valid': l['valid'],
          'direction': direction,
          'property': l['master_property'],
          'object': other,
          'value': l['target_value'],
          'creator': l['creator'],
          'previous': None,
      }

    changes = ([change(l, 'out', l['target']) for l in outgoing] +
               [change(l, 'in', l['source']) for l in incoming])
    changes.sort(key=lambda c: c['timestamp'])
    # An update replaces the property's last value (updates are only
    # made to unique properties).
    last = {}
    for c in changes:
      key = (c['direction'], c['property']['id'])
      if c['operation'] == 'update' and key in last:
        c['previous'] = last[key]
      if c['operation'] == 'delete':
        last.pop(key, None)
      else:
        last[key] = {'object': c['object'], 'value': c['value']}
    changes.reverse()
    changes.append({
        'timestamp': obj['timestamp'],
        'operation': 'create',
        'valid': True,
        'direction': None,
        'property': None,
        'object': None,
        'value': None,
        'creator': obj['creator'],
        'previous': None,
    })
    return {
        'id': obj['id'],
        'name': obj['name'],
        'changes': changes,
        'truncated': len(outgoing) == limit or len(incoming) == limit,
    }

  def properties(self, ids, asof=None):
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
    }], asof=asof)
    return {p['id']: p for p in result}

  def type_info(self, id, asof=None):
    """A type's properties and some of its instances."""
    read = lambda q: self.read(q, asof=asof)
    props, _ = read([{
        'type': '/type/property',
        'schema': {'id': id},
        'id': None,
        'name': None,
        'unique': None,
        'expected_type': {'id': None, 'name': None, 'optional': True},
        'sort': 'name',
        'limit': 1000,
    }])
    instances, _ = read([{
        'type': id,
        'id': None,
        'name': None,
        'limit': INSTANCE_LIMIT,
    }])
    count, _ = read({'type': id, 'return': 'estimate-count'})
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

  def path(self, a, b, via=None, to=None):
    """A shortest path from a to b, by one kind of step.

    Without via, a step is a direct link either way (not those in
    HIDDEN_LINKS, nor links to values). With via and to, a step goes
    from X through an object M that links to X by via, to an object M
    links to by to: from an author through a paper to a co-author, with
    via and to both /dblp/publication/author. Searches from both ends
    at once, in graphd directly (RawGraphd).

    Returns {"found", "nodes" (from a to b: id, name, types), "steps"
    (between consecutive nodes: "label"; without via, "property" and
    "forward", whether the link points along the path; with via,
    "middles", up to three objects in between, and "shared", how many),
    "relation" (with via: via, to and their names), "visited",
    "seconds", "reason" (why not found)}.
    """
    if bool(via) != bool(to):
      raise BadRequest('path with via also needs to, and to needs via')
    start = time.time()
    ends = self._guids([a, b])
    for id in (a, b):
      if id not in ends:
        raise NotFound('no object with id %s' % id)
    ga, gb = ends[a], ends[b]
    if via:
      props = self._guids([via, to])
      for id in (via, to):
        if id not in props:
          raise NotFound('no property with id %s' % id)
      p, q = props[via], props[to]
      hidden = set()
    else:
      p = q = None
      hidden = set(self._guids(sorted(HIDDEN_LINKS)).values())

    raw = RawGraphd(self.addr, start + PATH_SECONDS)
    try:
      # Per side: distance and how each object was reached, {guid:
      # (previous guid, middle guid or None, typeguid, whether a direct
      # link points from previous to it)}.
      sides = [{'dist': {ga: 0}, 'parent': {ga: None}, 'frontier': [ga],
                'depth': 0, 'forward': True},
               {'dist': {gb: 0}, 'parent': {gb: None}, 'frontier': [gb],
                'depth': 0, 'forward': False}]
      meet, reason = (ga if ga == gb else None), None
      while meet is None:
        if not sides[0]['frontier'] or not sides[1]['frontier']:
          reason = 'not connected'
          break
        if sides[0]['depth'] + sides[1]['depth'] >= PATH_MAX_STEPS:
          reason = 'more than %d steps apart' % PATH_MAX_STEPS
          break
        visited = len(sides[0]['dist']) + len(sides[1]['dist'])
        if visited > PATH_MAX_VISITED:
          reason = 'gave up after reaching %d objects' % visited
          break
        # Grow the side with the smaller frontier, by a whole level.
        side, other = sorted(sides, key=lambda s: len(s['frontier']))
        steps = self._path_steps(raw, side['frontier'], p, q, hidden,
                                 side['forward'])
        new = []
        for prev, middle, typeguid, n, out in steps:
          if n not in side['dist']:
            side['dist'][n] = side['depth'] + 1
            side['parent'][n] = (prev, middle, typeguid, out)
            new.append(n)
        side['frontier'] = new
        side['depth'] += 1
        meets = [n for n in new if n in other['dist']]
        if meets:
          meet = min(meets, key=lambda n: other['dist'][n])
      if meet is not None:
        nodes, steps = self._path_through(raw, meet, sides, p, q)
    except PathGiveUp as e:
      meet, reason = None, str(e)
    finally:
      raw.close()

    result = {
        'found': meet is not None,
        'visited': len(sides[0]['dist']) + len(sides[1]['dist']),
        'reason': reason,
    }
    if meet is None:
      result.update(nodes=[], steps=[], relation=None)
    else:
      result.update(self._describe_path(nodes, steps, via, to))
    result['seconds'] = time.time() - start
    return result

  def _guids(self, ids):
    """{id: guid without the "#"} for those of ids that exist."""
    result, _ = self.read([{'id|=': ids, 'id': None, 'guid': None,
                            'limit': len(ids)}])
    return {r['id']: r['guid'][1:] for r in result}

  def _path_steps(self, raw, frontier, p, q, hidden, forward):
    """(from, middle, typeguid, to, out) for every step out of frontier.

    out: for a direct link, whether it points from "from" to "to".
    """
    if p is None:
      out = raw.read_for(frontier, lambda gs: (
          '(left=%s value=null pagesize=%d result=((left typeguid right)))'
          % (gs, PATH_PAGESIZE)))
      into = raw.read_for(frontier, lambda gs: (
          '(right=%s value=null pagesize=%d result=((right typeguid left)))'
          % (gs, PATH_PAGESIZE)))
      return ([(x, None, t, y, True) for x, t, y in out
               if y and t not in hidden] +
              [(x, None, t, y, False) for x, t, y in into
               if y and t not in hidden])
    # Forward, X <-p- M -q-> Y; the side searching back from b goes the
    # other way, Y <-q- M -p-> X.
    first, second = (p, q) if forward else (q, p)
    starts = raw.read_for(frontier, lambda gs: (
        '(typeguid=%s right=%s pagesize=%d result=((left right)))'
        % (first, gs, PATH_PAGESIZE)))
    via_middle = {}
    for m, x in starts:
      via_middle.setdefault(m, x)
    ends = raw.read_for(sorted(via_middle), lambda gs: (
        '(typeguid=%s left=%s pagesize=%d result=((left right)))'
        % (second, gs, PATH_PAGESIZE)))
    return [(via_middle[m], m, second, y, None) for m, y in ends if y]

  def _path_through(self, raw, meet, sides, p, q):
    """The guids from a to b through meet, and the steps between."""
    a_side, b_side = sides
    nodes, steps = [meet], []
    n = meet
    while a_side['parent'][n]:
      prev, middle, typeguid, out = a_side['parent'][n]
      nodes.insert(0, prev)
      steps.insert(0, (middle, typeguid, out))
      n = prev
    n = meet
    while b_side['parent'][n]:
      # Found searching back from b: the link runs the other way.
      prev, middle, typeguid, out = b_side['parent'][n]
      nodes.append(prev)
      steps.append((middle, typeguid, None if out is None else not out))
      n = prev
    if p is None:
      return nodes, [{'middles': [], 'shared': None, 'typeguid': t,
                      'forward': out} for _, t, out in steps]
    # Every object in between each pair, not just the one found.
    described = []
    for x, y in zip(nodes, nodes[1:]):
      from_x = {m for m, in raw.read(
          '(typeguid=%s right=%s pagesize=%d result=((left)))'
          % (p, x, PATH_PAGESIZE))}
      to_y = {m for m, in raw.read(
          '(typeguid=%s right=%s pagesize=%d result=((left)))'
          % (q, y, PATH_PAGESIZE))}
      shared = sorted(from_x & to_y)
      described.append({'middles': shared[:3], 'shared': len(shared),
                        'typeguid': None, 'forward': None})
    return nodes, described

  def _describe_path(self, nodes, steps, via, to):
    """Ids, names and types for a path's guids."""
    guids = set(nodes)
    for st in steps:
      guids.update(st['middles'])
      if st['typeguid']:
        guids.add(st['typeguid'])
    found, _ = self.read([{
        'guid|=': ['#' + g for g in sorted(guids)],
        'guid': None, 'id': None, 'name': None, 'type': [],
        'limit': len(guids),
    }])
    by_guid = {r['guid'][1:]: {'id': r['id'], 'name': r['name'],
                               'types': r['type']} for r in found}
    relation = None
    if via:
      names = self.properties({via, to})
      relation = {
          'via': via, 'to': to,
          'via_name': names[via]['name'] if via in names else via,
          'to_name': names[to]['name'] if to in names else to,
      }
      label = '%s of, then %s' % (relation['via_name'], relation['to_name'])
    return {
        'relation': relation,
        'nodes': [by_guid[g] for g in nodes],
        'steps': [{
            'label': (by_guid[st['typeguid']]['name'] or
                      by_guid[st['typeguid']]['id']) if not via else label,
            'property': None if via else by_guid[st['typeguid']]['id'],
            'forward': st['forward'],
            'middles': [by_guid[m] for m in st['middles']],
            'shared': st['shared'],
        } for st in steps],
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
  trace = Trace() if params.get('explain') in ('1', 'true') else None
  try:
    result, cursor = get_graph().read(query, params.get('cursor'), trace,
                                      params.get('asof') or None)
  except error.MQLError as e:
    if trace:
      raise ExplainedError(e, trace)
    raise
  reply = {
      'code': '/api/status/ok',
      'result': result,
      'cursor': cursor,
  }
  if trace:
    reply['explain'] = trace.as_json()
  return reply


def topic(params):
  return get_graph().topic(required(params, 'id'), params.get('asof') or None)


def history(params):
  return get_graph().history(required(params, 'id'))


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


def path(params):
  return get_graph().path(required(params, 'a'), required(params, 'b'),
                          params.get('via') or None, params.get('to') or None)


ROUTES = {
    '/api/mqlread': mqlread,
    '/api/topic': topic,
    '/api/history': history,
    '/api/search': search,
    '/api/relations': relations,
    '/api/expand': expand,
    '/api/path': path,
}

STATUS_TEXT = {
    200: 'OK',
    400: 'Bad Request',
    404: 'Not Found',
    405: 'Method Not Allowed',
}


def error_reply(message):
  return {'code': '/api/status/error', 'messages': [{'message': message}]}


def mql_error_reply(e):
  return {
      'code': '/api/status/error',
      'messages': [dict(e.error, message=str(e))],
  }


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
    status, body = 400, mql_error_reply(e)
  except ExplainedError as e:
    status, body = 400, mql_error_reply(e.mql_error)
    body['explain'] = e.trace.as_json()
  data = json.dumps(body, indent=1, ensure_ascii=False, default=str).encode()
  start_response('%d %s' % (status, STATUS_TEXT[status]), [
      ('Content-Type', 'application/json; charset=utf-8'),
      ('Content-Length', str(len(data))),
  ])
  if environ['REQUEST_METHOD'] == 'HEAD':
    return [b'']
  return [data]
