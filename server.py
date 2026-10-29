"""A small web service around MQL, and a browser for the graph.

  GET /api/mqlread?query=JSON[&cursor=C]   run an MQL read
  GET /api/topic?id=ID                     everything about one object
  GET /api/search?q=TEXT                   objects whose names match
  GET /                                    the browser (static/)

Replies are JSON. mqlread follows Freebase's API: {"code":
"/api/status/ok", "result": ..., "cursor": ...}, or on error {"code":
"/api/status/error", "messages": [...]} with HTTP status 400.

Usage: server.py --graphd_addr=host:port [--port=8080]
"""

import http.server
import json
import mimetypes
import os
import threading
import urllib.parse

from absl import app
from absl import flags

from pymql import MQLService
from pymql.mql import error
from pymql.mql import graph

FLAGS = flags.FLAGS
# pymql's bootstrap defines the same flag; the tests import both.
if 'graphd_addr' not in FLAGS:
  flags.DEFINE_string('graphd_addr', None, 'graphd address, host:port')
flags.DEFINE_string('host', '127.0.0.1', 'address to listen on')
flags.DEFINE_integer('port', 8080, 'port to listen on')

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')

# Most links of one kind (outgoing, incoming, values) shown for an object.
LINK_LIMIT = 500
# Most instances shown for a type.
INSTANCE_LIMIT = 100

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


class Handler(http.server.BaseHTTPRequestHandler):
  graph = None

  def do_GET(self):
    url = urllib.parse.urlparse(self.path)
    params = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query, keep_blank_values=True).items()}
    try:
      if url.path == '/api/mqlread':
        self.mqlread(params)
      elif url.path == '/api/topic':
        self.send_json(200, self.graph.topic(self.required(params, 'id')))
      elif url.path == '/api/search':
        self.send_json(200, {'result': self.graph.search(params.get('q', ''))})
      elif url.path.startswith('/api/'):
        raise NotFound('no such API: %s' % url.path)
      else:
        self.static(url.path)
    except BadRequest as e:
      self.send_json(400, {'code': '/api/status/error',
                           'messages': [{'message': str(e)}]})
    except NotFound as e:
      self.send_json(404, {'code': '/api/status/error',
                           'messages': [{'message': str(e)}]})
    except error.MQLError as e:
      self.send_json(400, {'code': '/api/status/error',
                           'messages': [dict(e.error, message=str(e))]})

  def required(self, params, name):
    if name not in params:
      raise BadRequest('missing parameter: %s' % name)
    return params[name]

  def mqlread(self, params):
    try:
      query = json.loads(self.required(params, 'query'))
    except ValueError as e:
      raise BadRequest('query is not valid JSON: %s' % e)
    result, cursor = self.graph.read(query, params.get('cursor'))
    self.send_json(200, {
        'code': '/api/status/ok',
        'result': result,
        'cursor': cursor,
    })

  def static(self, path):
    if path == '/':
      path = '/index.html'
    file = os.path.normpath(os.path.join(STATIC, path.lstrip('/')))
    if not file.startswith(STATIC + os.sep) or not os.path.isfile(file):
      raise NotFound('no such page: %s' % path)
    with open(file, 'rb') as f:
      body = f.read()
    self.send_response(200)
    self.send_header('Content-Type',
                     mimetypes.guess_type(file)[0] or 'application/octet-stream')
    self.send_header('Content-Length', str(len(body)))
    self.end_headers()
    self.wfile.write(body)

  def send_json(self, status, obj):
    body = json.dumps(obj, indent=1, ensure_ascii=False, default=str).encode()
    self.send_response(status)
    self.send_header('Content-Type', 'application/json; charset=utf-8')
    self.send_header('Content-Length', str(len(body)))
    self.end_headers()
    self.wfile.write(body)

  def log_message(self, fmt, *args):
    pass


def main(argv):
  if not FLAGS.graphd_addr:
    raise app.UsageError('--graphd_addr is required')
  Handler.graph = Graph(FLAGS.graphd_addr)
  server = http.server.ThreadingHTTPServer((FLAGS.host, FLAGS.port), Handler)
  print('serving http://%s:%d/ for graphd %s' %
        (FLAGS.host, FLAGS.port, FLAGS.graphd_addr), flush=True)
  server.serve_forever()


if __name__ == '__main__':
  app.run(main)
