# MQL Browser

A web service around [MQL](../pymql) and a browser for the graph behind
it: an object's name, ids and types, its properties grouped by type, and
the objects that link to it.

- `server.py` is the web service. It runs MQL reads through pymql against
  a graphd and answers:
  - `GET /api/mqlread?query=JSON[&cursor=C]`: an MQL read, with
    Freebase's reply format (`{"code": "/api/status/ok", "result": ...}`)
  - `GET /api/topic?id=ID`: everything the browser shows for an object
  - `GET /api/search?q=TEXT`: objects whose names match, the last word as
    a prefix
- `static/` is the browser, a single page using those APIs.
- nginx serves `static/` and passes `/api/` to `server.py`.

## Running

It needs pymql's Python 3 dependencies (see pymql's README), nginx, and
a running graphd that has been bootstrapped for MQL:

```
$ PYTHON=~/.local/pymql-py3/bin/python ./run.sh --graphd_addr=localhost:8200
MQL browser on http://127.0.0.1:8080/
```

`run.sh` writes nginx's config, logs and temporary files under `run/`.
`PORT` and `API_PORT` change the ports (8080 and 8081).

Without nginx, `server.py` serves `static/` itself:

```
$ PYTHONPATH=.. ~/.local/pymql-py3/bin/python server.py \
    --graphd_addr=localhost:8200 --port=8080
```

## Tests

`server_test.py` starts its own graphd on a free port in a temporary
directory, bootstraps it, loads a small test domain, and tests the API
and the static files. It looks for the graphd binary in
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

The service is read-only, and it listens only on 127.0.0.1.
