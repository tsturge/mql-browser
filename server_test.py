"""Tests for server.py against a graphd started for the test.

The graphd binary is --graphd_binary, else $GRAPHD, else
../graphd/out/bin/graphd beside this repository; the tests are skipped
without one. pymql must be importable (PYTHONPATH=..):

  PYTHONPATH=.. ~/.local/pymql-py3/bin/python server_test.py
"""

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

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


def _free_port():
  s = socket.socket()
  s.bind(('127.0.0.1', 0))
  port = s.getsockname()[1]
  s.close()
  return port


def _text(value):
  return {'value': value, 'lang': '/lang/en'}


def load_test_data(conn):
  """A /t domain with bands and albums, and one band with two albums."""
  mql = pymql.MQLService(connector=conn)
  mql.write({
      'create': 'unless_exists', 'id': None, 'type': '/type/domain',
      'key': {'namespace': '/', 'value': 't'}, 'name': _text('Sample domain'),
  }, **ROOT)
  for key, name in [('band', 'Band'), ('album', 'Album')]:
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


class ServerTest(absltest.TestCase):

  @classmethod
  def setUpClass(cls):
    super(ServerTest, cls).setUpClass()
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
    cls.http = None
    try:
      deadline = time.time() + 10
      while True:
        try:
          socket.create_connection(('127.0.0.1', port), 0.1).close()
          break
        except OSError:
          if cls.graphd.poll() is not None or time.time() > deadline:
            raise RuntimeError('graphd did not start')
          time.sleep(0.05)

      conn = graph.TcpGraphConnector(addrs=[('127.0.0.1', port)])
      boot = bootstrap.Bootstrap(conn)
      boot.load_from_file(
          os.path.join(_CODE, 'pymql', 'bootstrap', 'otg.bootstrap'))
      boot.load_bootstrap()
      boot.load_root_user()
      boot.load_object_hints()
      load_test_data(conn)

      cls.graph = server.Graph('127.0.0.1:%d' % port)
      cls.start_http(cls.graph)
    except:
      cls.stop()
      raise

  @classmethod
  def start_http(cls, graph_):
    server.Handler.graph = graph_
    cls.http = server.http.server.ThreadingHTTPServer(
        ('127.0.0.1', 0), server.Handler)
    threading.Thread(target=cls.http.serve_forever, daemon=True).start()

  @classmethod
  def tearDownClass(cls):
    cls.stop()
    super(ServerTest, cls).tearDownClass()

  @classmethod
  def stop(cls):
    if cls.http:
      cls.http.shutdown()
      cls.http.server_close()
    cls.graphd.terminate()
    cls.graphd.wait()
    shutil.rmtree(cls.tmpdir, ignore_errors=True)

  def get(self, path, **params):
    """GETs path from the server; returns (status, body bytes)."""
    url = 'http://127.0.0.1:%d%s' % (self.http.server_address[1], path)
    if params:
      url += '?' + urllib.parse.urlencode(params)
    try:
      with urllib.request.urlopen(url) as r:
        return r.status, r.read()
    except urllib.error.HTTPError as e:
      return e.code, e.read()

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

  def testUnknownApi(self):
    status, body = self.get_json('/api/nope')
    self.assertEqual(status, 404)
    self.assertEqual(body['code'], '/api/status/error')

  # Static files

  def testStaticFiles(self):
    status, body = self.get('/')
    self.assertEqual(status, 200)
    self.assertIn(b'<script src="/app.js">', body)
    for path in ('/app.js', '/style.css'):
      self.assertEqual(self.get(path)[0], 200, path)

  def testStaticFilesStayInStatic(self):
    self.assertEqual(self.get('/../server.py')[0], 404)
    self.assertEqual(self.get('/%2e%2e/server.py')[0], 404)
    self.assertEqual(self.get('/no_such_file.js')[0], 404)


if __name__ == '__main__':
  absltest.main()
