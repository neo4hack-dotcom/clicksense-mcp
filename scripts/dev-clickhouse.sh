#!/usr/bin/env bash
# Local single-node ClickHouse for development and integration tests.
#   scripts/dev-clickhouse.sh start|stop|status|seed|reset
# HTTP on 127.0.0.1:18123 (not 8123, to stay out of the way of any other instance).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUN="$ROOT/.clickhouse"
CONF="$ROOT/dev/clickhouse/config.xml"
PIDFILE="$RUN/clickhouse.pid"
CH="${CLICKHOUSE_BIN:-clickhouse}"

client() { "$CH" client --port 19000 --user default "$@"; }

up() { curl -sf http://127.0.0.1:18123/ping >/dev/null 2>&1; }

start() {
  if up; then echo "already running"; return; fi
  mkdir -p "$RUN"/{data,tmp,user_files,log}
  cp "$ROOT/dev/clickhouse/users.xml" "$RUN/users.xml"
  cp "$CONF" "$RUN/config.xml"
  # No --daemon: it does not detach reliably on macOS builds. Plain background + pidfile.
  (cd "$RUN" && nohup "$CH" server --config-file="$RUN/config.xml" -- \
      --path="$RUN/data/" --tmp_path="$RUN/tmp/" --user_files_path="$RUN/user_files/" \
      > "$RUN/log/stdout.log" 2>&1 & echo $! > "$PIDFILE")
  for _ in $(seq 1 60); do up && { echo "ClickHouse up on :18123"; return; }; sleep 0.5; done
  echo "ClickHouse failed to start, see $RUN/log" >&2; exit 1
}

stop() {
  if [[ -f "$PIDFILE" ]]; then kill "$(cat "$PIDFILE")" 2>/dev/null || true; rm -f "$PIDFILE"; fi
  echo "stopped"
}

seed() { client --multiquery < "$ROOT/dev/seed.sql"; echo "seeded"; }

case "${1:-start}" in
  start) start ;;
  stop) stop ;;
  status) up && echo "up" || echo "down" ;;
  seed) seed ;;
  reset) stop; rm -rf "$RUN"; start; seed ;;
  *) echo "usage: $0 start|stop|status|seed|reset" >&2; exit 2 ;;
esac
