#!/bin/bash
#
# run.sh -- run the MQL browser: server.py for /api/, nginx in front.
#
#   ./run.sh --graphd_addr=localhost:8200
#
# Environment:
#   PORT       port nginx listens on (default 8080)
#   API_PORT   port server.py listens on (default 8081)
#   NGINX      nginx binary (default: nginx on PATH)
#   PYTHON     Python with pymql's dependencies (default: python3)
#   PYMQL_PARENT  directory holding the pymql checkout (default: ..)
#   RUNDIR     nginx's working directory (default: ./run)
#
# Both stop on Ctrl-C.

set -eu

top=$(cd "$(dirname "$0")" && pwd)
port=${PORT:-8080}
api_port=${API_PORT:-8081}
nginx=${NGINX:-nginx}
python=${PYTHON:-python3}
pymql_parent=$(cd "${PYMQL_PARENT:-$top/..}" && pwd)
rundir=${RUNDIR:-$top/run}

if ! command -v "$nginx" > /dev/null; then
  echo "$0: no nginx; install it (brew install nginx) or set NGINX" >&2
  exit 2
fi

mkdir -p "$rundir/conf" "$rundir/logs" "$rundir/tmp"
sed -e "s|@STATIC@|$top/static|" \
    -e "s|@PORT@|$port|" \
    -e "s|@API_PORT@|$api_port|" \
    "$top/nginx.conf.in" > "$rundir/conf/nginx.conf"
"$nginx" -t -q -p "$rundir" -c conf/nginx.conf

PYTHONPATH="$pymql_parent" "$python" "$top/server.py" \
    --host=127.0.0.1 --port="$api_port" "$@" &
api_pid=$!
"$nginx" -p "$rundir" -c conf/nginx.conf &
nginx_pid=$!
trap 'kill $api_pid $nginx_pid 2> /dev/null' EXIT INT TERM

echo "MQL browser on http://127.0.0.1:$port/"
# Until either one exits (macOS's bash 3.2 has no "wait -n").
while kill -0 $api_pid 2> /dev/null && kill -0 $nginx_pid 2> /dev/null; do
  sleep 1
done
