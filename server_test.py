"""Tests for server.py against a graphd started for the test.

ServerTest serves the WSGI application with wsgiref; NginxTest runs
run.sh, with nginx and gunicorn, and is skipped without them.

The graphd binary is --graphd_binary, else $GRAPHD, else
../graphd/out/bin/graphd beside this repository; the tests are skipped
without one. pymql must be importable (PYTHONPATH=..):

  PYTHONPATH=.. ~/.local/pymql-py3/bin/python server_test.py
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import wsgiref.simple_server

from absl import flags
from absl.testing import absltest

import pymql
from pymql.bootstrap import bootstrap
from pymql.mql import graph
from pymql.mql import scope

import server

FLAGS = flags.FLAGS

_TOP = os.path.dirname(os.path.abspath(__file__))
_CODE = os.path.dirname(_TOP)

flags.DEFINE_string(
    'graphd_binary',
    os.environ.get('GRAPHD',
                   os.path.join(_CODE, 'graphd', 'out', 'bin', 'graphd')),
    'graphd binary to start for the tests')

ROOT = {'user': '/user/root', 'privileged': scope.Privileged}

BAND = '/en/the_testers'
FIRST = '/en/first_light'
SECOND = '/en/second_wind'
ALICE = '/en/alice'
BOB = '/en/bob'


def _free_port():
  s = socket.socket()
  s.bind(('127.0.0.1', 0))
  port = s.getsockname()[1]
  s.close()
  return port


def _wait_for_port(port, process, what, timeout=10):
  deadline = time.time() + timeout
  while True:
    try:
      socket.create_connection(('127.0.0.1', port), 0.1).close()
      return
    except OSError:
      if process.poll() is not None or time.time() > deadline:
        raise RuntimeError('%s did not start' % what)
      time.sleep(0.05)


def _get(port, path, **params):
  """GETs path from 127.0.0.1:port; returns (status, body bytes)."""
  url = 'http://127.0.0.1:%d%s' % (port, path)
  if params:
    url += '?' + urllib.parse.urlencode(params)
  try:
    with urllib.request.urlopen(url) as r:
      return r.status, r.read()
  except urllib.error.HTTPError as e:
    return e.code, e.read()


def _addr(host_port):
  host, port = host_port.rsplit(':', 1)
  return host, int(port)


def _text(value):
  return {'value': value, 'lang': '/lang/en'}


def load_test_data(conn):
  """A /t domain with bands and albums, and one band with two albums."""
  mql = pymql.MQLService(connector=conn)
  mql.write({
      'create': 'unless_exists', 'id': None, 'type': '/type/domain',
      'key': {'namespace': '/', 'value': 't'}, 'name': _text('Sample domain'),
  }, **ROOT)
  for key, name in [('band', 'Band'), ('album', 'Album'),
                    ('singer', 'Singer'), ('song', 'Song')]:
    mql.write({
        'create': 'unless_exists', 'id': None, 'type': '/type/type',
        'key': {'namespace': '/t', 'value': key}, 'name': _text(name),
        '/type/type/domain': {'id': '/t'},
    }, **ROOT)
  for key, name, expected, unique in [
      ('artist', 'Artist', '/t/band', True),
      ('released', 'Released', '/type/datetime', True),
  ]:
    mql.write({
        'create': 'unless_exists', 'id': None, 'type': '/type/property',
        'key': {'namespace': '/t/album', 'value': key}, 'name': _text(name),
        '/type/property/schema': {'id': '/t/album'},
        '/type/property/expected_type': {'id': expected},
        '/type/property/unique': unique,
    }, **ROOT)
  mql.write({
      'create': 'unconditional', 'id': None, 'type': '/t/band',
      'key': {'namespace': '/en', 'value': 'the_testers'},
      'name': _text('The Testers'),
  }, **ROOT)
  for key, name, year in [('first_light', 'First Light', '1999'),
                          ('second_wind', 'Second Wind & Co', '2004')]:
    mql.write({
        'create': 'unconditional', 'id': None, 'type': '/t/album',
        'key': {'namespace': '/en', 'value': key}, 'name': _text(name),
        '/t/album/artist': {'id': BAND, 'connect': 'insert'},
        '/t/album/released': {'value': year, 'connect': 'insert'},
    }, **ROOT)

  # Singers and songs, for the graph view: Alice and Bob sing two duets,
  # and Alice a solo.
  mql.write({
      'create': 'unless_exists', 'id': None, 'type': '/type/property',
      'key': {'namespace': '/t/song', 'value': 'performer'},
      'name': _text('Performer'),
      '/type/property/schema': {'id': '/t/song'},
      '/type/property/expected_type': {'id': '/t/singer'},
      '/type/property/unique': False,
  }, **ROOT)
  for key, name in [('alice', 'Alice'), ('bob', 'Bob')]:
    mql.write({
        'create': 'unconditional', 'id': None, 'type': '/t/singer',
        'key': {'namespace': '/en', 'value': key}, 'name': _text(name),
    }, **ROOT)
  for key, name, performers in [('duet', 'Duet', [ALICE, BOB]),
                                ('duet_2', 'Duet Again', [ALICE, BOB]),
                                ('solo', 'Solo', [ALICE])]:
    mql.write({
        'create': 'unconditional', 'id': None, 'type': '/t/song',
        'key': {'namespace': '/en', 'value': key}, 'name': _text(name),
        '/t/song/performer': [{'id': p, 'connect': 'insert'}
                              for p in performers],
    }, **ROOT)


class GraphdTestCase(absltest.TestCase):
  """Starts a bootstrapped graphd holding the test data."""

  @classmethod
  def setUpClass(cls):
    super(GraphdTestCase, cls).setUpClass()
    if not os.access(FLAGS.graphd_binary, os.X_OK):
      raise unittest.SkipTest('no graphd binary at %s' % FLAGS.graphd_binary)

    cls.tmpdir = tempfile.mkdtemp(prefix='mql-browser-test-')
    port = _free_port()
    # graphd writes debug logs to its current directory.
    cls.graphd = subprocess.Popen(
        [FLAGS.graphd_binary, '-n',
         '-d', os.path.join(cls.tmpdir, 'db'),
         '-p', os.path.join(cls.tmpdir, 'graphd.pid'),
         '-i', 'tcp://127.0.0.1:%d' % port],
        cwd=cls.tmpdir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
      _wait_for_port(port, cls.graphd, 'graphd')

      conn = graph.TcpGraphConnector(addrs=[('127.0.0.1', port)])
      boot = bootstrap.Bootstrap(conn)
      boot.load_from_file(
          os.path.join(_CODE, 'pymql', 'bootstrap', 'otg.bootstrap'))
      boot.load_bootstrap()
      boot.load_root_user()
      boot.load_object_hints()
      load_test_data(conn)
      cls.graphd_addr = '127.0.0.1:%d' % port
    except:
      cls.stop_graphd()
      raise

  @classmethod
  def tearDownClass(cls):
    cls.stop_graphd()
    super(GraphdTestCase, cls).tearDownClass()

  @classmethod
  def stop_graphd(cls):
    cls.graphd.terminate()
    cls.graphd.wait()
    shutil.rmtree(cls.tmpdir, ignore_errors=True)


class QuietHandler(wsgiref.simple_server.WSGIRequestHandler):

  def log_message(self, *args):
    pass


class ServerTest(GraphdTestCase):
  """The Graph class, and the WSGI application served by wsgiref."""

  @classmethod
  def setUpClass(cls):
    super(ServerTest, cls).setUpClass()
    os.environ['GRAPHD_ADDR'] = cls.graphd_addr
    server._graph = None
    cls.graph = server.get_graph()
    cls.http = wsgiref.simple_server.make_server(
        '127.0.0.1', 0, server.application, handler_class=QuietHandler)
    threading.Thread(target=cls.http.serve_forever, daemon=True).start()

  @classmethod
  def tearDownClass(cls):
    cls.http.shutdown()
    cls.http.server_close()
    server._graph = None
    super(ServerTest, cls).tearDownClass()

  def get(self, path, **params):
    return _get(self.http.server_address[1], path, **params)

  def get_json(self, path, **params):
    status, body = self.get(path, **params)
    return status, json.loads(body)

  # Graph

  def testRead(self):
    result, cursor = self.graph.read({'id': BAND, 'name': None})
    self.assertEqual(result, {'id': BAND, 'name': 'The Testers'})
    self.assertIsNone(cursor)

  def testReadIsNotHtmlEscaped(self):
    result, _ = self.graph.read({'id': SECOND, 'name': None})
    self.assertEqual(result['name'], 'Second Wind & Co')

  def testTopicOfAnObjectWithIncomingLinks(self):
    t = self.graph.topic(BAND)
    self.assertEqual(t['name'], 'The Testers')
    self.assertEqual([ty['id'] for ty in t['types']], ['/t/band'])
    self.assertEqual(t['keys'], [{'namespace': '/en', 'value': 'the_testers'}])
    self.assertRegex(t['guid'], '^#[0-9a-f]{32}$')

    # The name and types are shown on their own, not as properties.
    self.assertEqual(t['properties'], [])

    [albums] = t['incoming']
    self.assertEqual(albums['type'], {'id': '/t/album', 'name': 'Album'})
    [artist] = albums['properties']
    self.assertEqual(artist['id'], '/t/album/artist')
    self.assertEqual(artist['name'], 'Artist')
    self.assertCountEqual(artist['values'], [
        {'id': FIRST, 'name': 'First Light'},
        {'id': SECOND, 'name': 'Second Wind & Co'},
    ])
    self.assertNotIn('type_info', t)

  def testTopicOfAnObjectWithProperties(self):
    t = self.graph.topic(FIRST)
    [section] = t['properties']
    self.assertEqual(section['type']['id'], '/t/album')
    props = {p['id']: p for p in section['properties']}
    self.assertEqual(props['/t/album/artist']['values'],
                     [{'id': BAND, 'name': 'The Testers'}])
    self.assertEqual(props['/t/album/artist']['expected_type'], '/t/band')
    self.assertTrue(props['/t/album/artist']['unique'])
    self.assertEqual(props['/t/album/released']['values'],
                     [{'value': '1999', 'type': '/type/datetime'}])
    # Properties are sorted by name.
    self.assertEqual([p['name'] for p in section['properties']],
                     ['Artist', 'Released'])
    self.assertEqual(t['incoming'], [])
    self.assertEqual(t['truncated'],
                     {'outgoing': False, 'incoming': False, 'values': False})

  def testTopicOfAType(self):
    t = self.graph.topic('/t/album')
    self.assertIn('/type/type', [ty['id'] for ty in t['types']])
    info = t['type_info']
    self.assertEqual([p['id'] for p in info['properties']],
                     ['/t/album/artist', '/t/album/released'])
    self.assertEqual(info['properties'][0]['expected_type'],
                     {'id': '/t/band', 'name': 'Band'})
    self.assertCountEqual([i['id'] for i in info['instances']],
                          [FIRST, SECOND])
    self.assertEqual(info['instance_count'], 2)

  def testTopicOfAMissingObject(self):
    with self.assertRaises(server.NotFound):
      self.graph.topic('/en/no_such_thing')

  def testSearchMatchesWordPrefixes(self):
    self.assertEqual([r['id'] for r in self.graph.search('Test')], [BAND])
    self.assertEqual([r['id'] for r in self.graph.search('second wi')],
                     [SECOND])
    [band] = self.graph.search('the testers')
    self.assertEqual(band['type'], [{'id': '/t/band', 'name': 'Band'}])

  def testSearchForNothing(self):
    self.assertEqual(self.graph.search('   '), [])
    self.assertEqual(self.graph.search('zzzzqq'), [])

  def testRelations(self):
    rels = self.graph.relations(ALICE)['relations']
    self.assertEqual(rels[0], {'label': 'Direct links', 'via': None, 'to': None})
    self.assertIn({
        'label': 'Performer of, then Performer',
        'via': '/t/song/performer',
        'to': '/t/song/performer',
        'unique': False,
        'count': 3,
    }, rels)

  def testExpandThroughSharedObjects(self):
    # Alice's co-performers, weighted by the songs they share.
    e = self.graph.expand(ALICE, '/t/song/performer', '/t/song/performer')
    self.assertEqual(e['center'], {'id': ALICE, 'name': 'Alice',
                                   'types': ['/t/singer']})
    self.assertEqual(e['nodes'], [{'id': BOB, 'name': 'Bob',
                                   'types': ['/t/singer']}])
    self.assertEqual(e['edges'], [{
        'from': ALICE, 'to': BOB, 'weight': 2, 'labels': ['Performer'],
        'directed': False,
    }])
    self.assertEqual(e['via_count'], 3)
    self.assertFalse(e['truncated'])

  def testExpandDirectLinks(self):
    e = self.graph.expand(BOB)
    self.assertCountEqual([n['name'] for n in e['nodes']],
                          ['Duet', 'Duet Again'])
    # The songs link to Bob, so the edges point at him.
    for edge in e['edges']:
      self.assertEqual(edge['to'], BOB)
      self.assertTrue(edge['directed'])
      self.assertEqual(edge['labels'], ['Performer'])

  def testExpandLimit(self):
    e = self.graph.expand(ALICE, limit=1)
    self.assertLen(e['nodes'], 1)
    self.assertTrue(e['truncated'])

  # HTTP API

  def testMqlread(self):
    status, body = self.get_json(
        '/api/mqlread', query=json.dumps({'id': BAND, 'name': None}))
    self.assertEqual(status, 200)
    self.assertEqual(body, {
        'code': '/api/status/ok',
        'result': {'id': BAND, 'name': 'The Testers'},
        'cursor': None,
    })

  def testMqlreadWithACursor(self):
    # An empty cursor starts paging; the last page's cursor is false.
    query = json.dumps([{'type': '/t/album', 'id': None, 'limit': 1}])
    ids, cursor = [], ''
    for _ in range(5):
      status, page = self.get_json('/api/mqlread', query=query, cursor=cursor)
      self.assertEqual(status, 200)
      self.assertLessEqual(len(page['result']), 1)
      ids += [r['id'] for r in page['result']]
      cursor = page['cursor']
      if cursor is False:
        break
      self.assertIsInstance(cursor, str)
    self.assertIs(cursor, False)
    self.assertCountEqual(ids, [FIRST, SECOND])

  def testMqlreadErrors(self):
    status, body = self.get_json('/api/mqlread', query='{not json')
    self.assertEqual(status, 400)
    self.assertEqual(body['code'], '/api/status/error')
    self.assertIn('not valid JSON', body['messages'][0]['message'])

    status, body = self.get_json(
        '/api/mqlread', query=json.dumps({'id': BAND, 'no_such_property': None}))
    self.assertEqual(status, 400)
    self.assertEqual(body['code'], '/api/status/error')
    self.assertEqual(body['messages'][0]['code'], '/api/status/error/mql/type')

    status, body = self.get_json('/api/mqlread')
    self.assertEqual(status, 400)
    self.assertIn('missing parameter: query', body['messages'][0]['message'])

  def testMqlreadExplain(self):
    self.graph.service = None  # Start with nothing cached.
    query = [{'type': '/t/album', 'name': None, 'artist': {'id': BAND}}]
    status, body = self.get_json(
        '/api/mqlread', query=json.dumps(query), explain='1')
    self.assertEqual(status, 200)
    self.assertCountEqual([r['name'] for r in body['result']],
                          ['First Light', 'Second Wind & Co'])
    explain = body['explain']
    requests = explain['requests']
    # Exactly one request is the read itself; resolving /t/album and
    # its properties takes others.
    self.assertEqual(explain['cache'], 'new')
    self.assertEqual(requests[0]['kind'], 'dateline')
    queries = [r for r in requests if r['kind'] == 'query']
    self.assertLen(queries, 1)
    self.assertTrue(queries[0]['query'].startswith('read '))
    self.assertIn('schema', {r['kind'] for r in requests})
    self.assertIn('id lookup', {r['kind'] for r in requests})
    for r in requests:
      self.assertGreaterEqual(r['ms'], 0)
      self.assertIsNone(r['error'])
      self.assertIn('dr', r['cost'])
    self.assertAlmostEqual(explain['graphd_ms'],
                           sum(r['ms'] for r in requests))
    self.assertGreaterEqual(explain['ms'], explain['graphd_ms'])

  def testCacheIsKeptUntilGraphdChanges(self):
    query = json.dumps({'id': BAND, 'name': None})

    def read(q=query):
      status, body = self.get_json('/api/mqlread', query=q, explain='1')
      self.assertEqual(status, 200, body)
      return body

    read()
    body = read()
    self.assertEqual(body['explain']['cache'], 'kept')
    # Only the dateline check and the query: schema and ids are cached.
    self.assertEqual([r['kind'] for r in body['explain']['requests']],
                     ['dateline', 'query'])

    # Another client writes: a new object, under a new key in a
    # namespace the server has cached.
    pymql.MQLService(connector=graph.TcpGraphConnector(
        addrs=[tuple(_addr(self.graphd_addr))])).write({
            'create': 'unconditional', 'id': None,
            'key': {'namespace': '/en', 'value': 'cache_probe'},
            'name': _text('Cache probe'),
        }, **ROOT)
    body = read(json.dumps({'id': '/en/cache_probe', 'name': None}))
    self.assertEqual(body['explain']['cache'], 'flushed')
    self.assertEqual(body['result'], {'id': '/en/cache_probe',
                                      'name': 'Cache probe'})
    self.assertEqual(read()['explain']['cache'], 'kept')

  def testMqlreadWithoutExplain(self):
    for explain in ({}, {'explain': '0'}):
      status, body = self.get_json(
          '/api/mqlread', query=json.dumps({'id': BAND, 'name': None}),
          **explain)
      self.assertEqual(status, 200)
      self.assertNotIn('explain', body)

  def testMqlreadExplainsErrors(self):
    status, body = self.get_json(
        '/api/mqlread', explain='1',
        query=json.dumps({'id': BAND, 'no_such_property': None}))
    self.assertEqual(status, 400)
    self.assertEqual(body['messages'][0]['code'], '/api/status/error/mql/type')
    self.assertNotEqual(body['explain']['requests'], [])
    # Without explain, no trace is left behind for the next read.
    status, body = self.get_json(
        '/api/mqlread', query=json.dumps({'id': BAND, 'name': None}))
    self.assertNotIn('explain', body)

  def testTopicApi(self):
    status, body = self.get_json('/api/topic', id=BAND)
    self.assertEqual(status, 200)
    self.assertEqual(body['name'], 'The Testers')
    status, body = self.get_json('/api/topic', id='/en/no_such_thing')
    self.assertEqual(status, 404)
    self.assertIn('no object', body['messages'][0]['message'])

  def testSearchApi(self):
    status, body = self.get_json('/api/search', q='testers')
    self.assertEqual(status, 200)
    self.assertEqual([r['id'] for r in body['result']], [BAND])

  def testGraphApis(self):
    status, body = self.get_json('/api/relations', id=ALICE)
    self.assertEqual(status, 200)
    self.assertEqual(body['relations'][0]['label'], 'Direct links')
    status, body = self.get_json('/api/expand', id=ALICE,
                                 via='/t/song/performer',
                                 to='/t/song/performer')
    self.assertEqual(status, 200)
    self.assertEqual([n['id'] for n in body['nodes']], [BOB])

    for params, status in [({'id': '/en/no_such_thing'}, 404),
                           ({'id': ALICE, 'via': '/t/song/performer'}, 400),
                           ({'id': ALICE, 'limit': 'many'}, 400),
                           ({}, 400)]:
      self.assertEqual(self.get_json('/api/expand', **params)[0], status,
                       params)
    self.assertEqual(self.get_json('/api/relations',
                                   id='/en/no_such_thing')[0], 404)

  def testUnknownApi(self):
    status, body = self.get_json('/api/nope')
    self.assertEqual(status, 404)
    self.assertEqual(body['code'], '/api/status/error')

  def testOnlyGet(self):
    url = 'http://127.0.0.1:%d/api/search' % self.http.server_address[1]
    req = urllib.request.Request(url, data=b'q=x', method='POST')
    with self.assertRaises(urllib.error.HTTPError) as e:
      urllib.request.urlopen(req)
    self.assertEqual(e.exception.code, 405)

  def testStaticFilesAreNotServed(self):
    # nginx serves static/; the application only answers /api/.
    self.assertEqual(self.get('/')[0], 404)
    self.assertEqual(self.get('/app.js')[0], 404)


class NginxTest(GraphdTestCase):
  """run.sh: nginx serving static/ and proxying /api/ to gunicorn."""

  @classmethod
  def setUpClass(cls):
    nginx = shutil.which('nginx') or '/opt/homebrew/bin/nginx'
    if not os.access(nginx, os.X_OK):
      raise unittest.SkipTest('no nginx')
    try:
      import gunicorn  # pylint: disable=g-import-not-at-top,unused-import
    except ImportError:
      raise unittest.SkipTest('no gunicorn')
    super(NginxTest, cls).setUpClass()
    cls.port = _free_port()
    # Not "cls.run": that would hide unittest.TestCase.run.
    cls.run_log = os.path.join(cls.tmpdir, 'run.log')
    env = dict(os.environ, PORT=str(cls.port), NGINX=nginx, WORKERS='2',
               PYTHON=sys.executable, RUNDIR=os.path.join(cls.tmpdir, 'run'))
    cls.run_sh = subprocess.Popen(
        [os.path.join(_TOP, 'run.sh'), cls.graphd_addr], env=env,
        stdout=subprocess.DEVNULL, stderr=open(cls.run_log, 'wb'),
        start_new_session=True)
    try:
      _wait_for_port(cls.port, cls.run_sh, 'run.sh')
      # nginx is up; wait for gunicorn behind it.
      deadline = time.time() + 10
      while _get(cls.port, '/api/search', q='x')[0] != 200:
        if time.time() > deadline:
          raise RuntimeError('gunicorn did not start')
        time.sleep(0.1)
    except:
      cls.stop_run()
      cls.stop_graphd()
      raise

  @classmethod
  def tearDownClass(cls):
    cls.stop_run()
    super(NginxTest, cls).tearDownClass()

  @classmethod
  def stop_run(cls):
    # run.sh, nginx and gunicorn are in their own process group.
    os.killpg(cls.run_sh.pid, signal.SIGTERM)
    cls.run_sh.wait()

  def get(self, path, **params):
    return _get(self.port, path, **params)

  def testStaticFiles(self):
    status, body = self.get('/')
    self.assertEqual(status, 200)
    self.assertIn(b'<script src="/app.js">', body)
    for path in ('/app.js', '/graph.js', '/style.css',
                 '/vendor/vis-network.min.js'):
      self.assertEqual(self.get(path)[0], 200, path)

  def testUnknownPagesGetTheBrowser(self):
    # The browser routes by URL hash, so any other path gets index.html.
    status, body = self.get('/no/such/page')
    self.assertEqual(status, 200)
    self.assertIn(b'<script src="/app.js">', body)

  def testApiIsProxied(self):
    status, body = self.get('/api/mqlread',
                            query=json.dumps({'id': BAND, 'name': None}))
    self.assertEqual(status, 200)
    self.assertEqual(json.loads(body)['result']['name'], 'The Testers')
    status, body = self.get('/api/topic', id='/en/no_such_thing')
    self.assertEqual(status, 404)
    self.assertEqual(json.loads(body)['code'], '/api/status/error')

  def testManual(self):
    manual = os.path.join(_CODE, 'mql-manual', 'html')
    if not os.path.isdir(manual):
      self.skipTest('no mql-manual checkout beside this one')
    status, body = self.get('/manual/')
    self.assertEqual(status, 200)
    self.assertIn(b'MQL Reference Guide', body)
    self.assertIn(b'href="ch03.html', body)
    for path in ('/manual/ch03.html', '/manual/files/stylesheet.css',
                 '/manual/files/logo-mqlreference.png'):
      self.assertEqual(self.get(path)[0], 200, path)
    # urllib follows the redirect from /manual to /manual/.
    status, body = self.get('/manual')
    self.assertEqual(status, 200)
    self.assertIn(b'MQL Reference Guide', body)
    self.assertEqual(self.get('/manual/no_such_page.html')[0], 404)

  def testStaticFilesStayInStatic(self):
    for path in ('/../server.py', '/%2e%2e/server.py', '/static/../server.py'):
      status, body = self.get(path)
      self.assertNotIn(b'import', body, path)


if __name__ == '__main__':
  absltest.main()
