# MQL Browser

A web service around [MQL](../pymql) and a browser for the graph behind
it: an object's name, ids and types, its properties grouped by type, and
the objects that link to it.

- `server.py` is the web service, a WSGI application run by gunicorn. It
  runs MQL reads through pymql against the graphd named by `GRAPHD_ADDR`
  and answers:
  - `GET /api/mqlread?query=JSON[&cursor=C]`: an MQL read, with
    Freebase's reply format (`{"code": "/api/status/ok", "result": ...}`).
    An empty `cursor` starts paging.
  - `GET /api/topic?id=ID`: everything the browser shows for an object
  - `GET /api/search?q=TEXT`: objects whose names match, the last word as
    a prefix
  - `GET /api/relations?id=ID` and `GET /api/expand?id=ID[&via=P&to=Q]`:
    an object's neighbours, for the graph view
- `static/` is the browser, a single page using those APIs.
- nginx serves `static/` and passes `/api/` to gunicorn over a Unix
  socket (`nginx.conf.in`). It also serves the MQL manual, from the
  [mql-manual](../mql-manual) checkout beside this one, at `/manual/`.

## Running

It needs pymql's Python 3 dependencies (see pymql's README), gunicorn
(`pip install gunicorn==23.0.0`), nginx (`brew install nginx`), and a
running graphd that has been bootstrapped for MQL. `run.sh` takes the
graphd's address:

```
$ PYTHON=~/.local/pymql-py3/bin/python ./run.sh localhost:8200
MQL browser on http://127.0.0.1:8080/ (graphd localhost:8200)
```

It writes nginx's config, both servers' logs and gunicorn's socket under
`run/`, and stops both servers on Ctrl-C. `PORT` changes nginx's port
(8080), `WORKERS` the number of gunicorn workers (4), and `MANUAL` where
the manual's HTML is (`../mql-manual/html`).

## Tests

`server_test.py` starts its own graphd on a free port in a temporary
directory, bootstraps it and loads a small test domain. `ServerTest`
tests the application, served by Python's wsgiref; `NginxTest` runs
`run.sh` and tests the static files and the API through nginx and
gunicorn, and is skipped without them. It looks for the graphd binary in
`--graphd_binary`, then `$GRAPHD`, then `../graphd/out/bin/graphd`, and
is skipped if there is none:

```
$ PYTHONPATH=.. ~/.local/pymql-py3/bin/python server_test.py
```

## Pages

- `#/view/ID`: an object. A type's page also lists its properties and
  some of its instances.
- `#/search/TEXT`: name search. Entering an id (`/en/...` or a `#guid`)
  in the search box goes straight to the object.
- `#/query`: an MQL query editor; ids in the results link to their pages.
- `#/graph/ID`: a graph of objects and the links between them, drawn
  with [vis-network](https://visjs.github.io/vis-network/) (9.1.9, in
  `static/vendor/`). Double-click a node to expand it (or contract it
  again); delete nodes, cluster a node's neighbours, and hide edges below
  a weight. "Delete orphans" removes the nodes with no edges shown, so
  raising the minimum weight and then deleting orphans prunes the graph
  to its strongest connections. A node expands by one of its relations:
  - its direct links, as arrows the way the links point; or
  - the objects reached through the objects linking to it, weighted by
    how many: through `/dblp/publication/author`, an author's co-authors
    ("Author of, then Author"), or the proceedings their papers are in
    ("Author of, then Part of"); a proceedings' authors ("Part of, then
    Author"); a paper's co-cited papers ("Cites of, then Cites").

  Double-clicking uses a co-relation like co-authors where there is one.

The service is read-only, and it listens only on 127.0.0.1.
