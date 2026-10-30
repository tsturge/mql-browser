#!/bin/bash
#
# run.sh -- run the MQL browser: server.py under gunicorn, nginx in front.
#
#   ./run.sh localhost:8200        (the graphd to read from)
#
# nginx serves static/ and passes /api/ to gunicorn on a Unix socket.
#
# Environment:
#   PORT          port nginx listens on (default 8080)
#   WORKERS       gunicorn worker processes (default 4)
#   NGINX         nginx binary (default: nginx on PATH, or Homebrew's)
#   PYTHON        Python with pymql's dependencies and gunicorn
#                 (default: python3)
#   PYMQL_PARENT  directory holding the pymql checkout (default: ..)
#   MANUAL        the MQL manual's HTML, served at /manual/
#                 (default: ../mql-manual/html)
#   RUNDIR        working directory for nginx and gunicorn (default: ./run)
#
# Both stop on Ctrl-C.

set -eu

if [ $# -ne 1 ]; then
  echo "usage: $0 graphd-host:port" >&2
  exit 2
fi

top=$(cd "$(dirname "$0")" && pwd)
port=${PORT:-8080}
workers=${WORKERS:-4}
python=${PYTHON:-python3}
pymql_parent=$(cd "${PYMQL_PARENT:-$top/..}" && pwd)
rundir=${RUNDIR:-$top/run}
manual=${MANUAL:-$top/../mql-manual/html}
manual=$(cd "$manual" 2> /dev/null && pwd || echo "$manual")
nginx=${NGINX:-$(command -v nginx || echo /opt/homebrew/bin/nginx)}

if [ ! -x "$nginx" ]; then
  echo "$0: no nginx; install it (brew install nginx) or set NGINX" >&2
  exit 2
fi
if ! "$python" -c 'import gunicorn' 2> /dev/null; then
  echo "$0: $python has no gunicorn; pip install gunicorn" >&2
  exit 2
fi

mkdir -p "$rundir/conf" "$rundir/logs" "$rundir/tmp"
socket=$rundir/api.sock
sed -e "s|@STATIC@|$top/static|" \
    -e "s|@PORT@|$port|" \
    -e "s|@API_SOCKET@|$socket|" \
    -e "s|@MANUAL@|$manual|" \
    "$top/nginx.conf.in" > "$rundir/conf/nginx.conf"
"$nginx" -t -q -p "$rundir" -c conf/nginx.conf

GRAPHD_ADDR=$1 PYTHONPATH="$pymql_parent" "$python" -m gunicorn \
    --bind "unix:$socket" --workers "$workers" --chdir "$top" \
    --access-logfile "$rundir/logs/api-access.log" \
    --error-logfile "$rundir/logs/api-error.log" \
    server:application &
api_pid=$!
"$nginx" -p "$rundir" -c conf/nginx.conf &
nginx_pid=$!
trap 'kill $api_pid $nginx_pid 2> /dev/null' EXIT INT TERM

echo "MQL browser on http://127.0.0.1:$port/ (graphd $1)"
# Until either one exits (macOS's bash 3.2 has no "wait -n").
while kill -0 $api_pid 2> /dev/null && kill -0 $nginx_pid 2> /dev/null; do
  sleep 1
done
