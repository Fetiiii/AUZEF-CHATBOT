#!/usr/bin/env bash
# Shared, read-only probes for the Docker Compose load-test tools.

LT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LT_ROOT="$(cd -- "$LT_DIR/../.." && pwd)"
LT_COMPOSE_FILE="${AUZEF_COMPOSE_FILE:-$LT_ROOT/docker-compose.yml}"
LT_DOCKER="${AUZEF_DOCKER_BIN:-docker}"
LT_STATE_DIR="${AUZEF_LOAD_STATE_DIR:-${TMPDIR:-/tmp}/auzef-load-test-${UID}}"

lt_compose() {
    "$LT_DOCKER" compose --project-directory "$LT_ROOT" -f "$LT_COMPOSE_FILE" "$@"
}

lt_container() {
    lt_compose ps -q "$1" 2>/dev/null | awk 'NR == 1 { print; exit }'
}

lt_running() {
    [ "$("$LT_DOCKER" inspect --format '{{.State.Running}}' "$1" 2>/dev/null)" = true ]
}

lt_limits() {
    "$LT_DOCKER" inspect --format '{{.HostConfig.Memory}}|{{.HostConfig.MemorySwap}}|{{.HostConfig.NanoCpus}}|{{.State.OOMKilled}}' "$1" 2>/dev/null
}

lt_stats() {
    "$LT_DOCKER" stats --no-stream --format '{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}|{{.PIDs}}' "$1" 2>/dev/null
}

lt_cgroup() {
    "$LT_DOCKER" exec "$1" sh -c '
        [ -r /sys/fs/cgroup/memory.events ] && [ -r /sys/fs/cgroup/cpu.stat ] || exit 1
        awk "{ print \"memory.\" \$1, \$2 }" /sys/fs/cgroup/memory.events
        awk "{ print \"cpu.\" \$1, \$2 }" /sys/fs/cgroup/cpu.stat
    ' 2>/dev/null
}

lt_value() {
    awk -v key="$2" '$1 == key { print $2; found = 1; exit } END { if (!found) print "N/A" }' <<< "$1"
}

lt_delta() {
    if [[ "$1" =~ ^[0-9]+$ && "$2" =~ ^[0-9]+$ && "$1" -ge "$2" ]]; then
        printf '%s\n' "$(( $1 - $2 ))"
    else
        printf 'N/A\n'
    fi
}

lt_gib() {
    awk -v bytes="$1" 'BEGIN { printf "%.2f GiB", bytes / 1073741824 }'
}

lt_workers() {
    "$LT_DOCKER" exec "$1" python -c '
args = open("/proc/1/cmdline", "rb").read().split(b"\0")
try:
    print(int(args[args.index(b"--workers") + 1]))
except (ValueError, IndexError):
    print("N/A")
' 2>/dev/null
}

lt_health() {
    # The backend has no published host port. Python stdlib is already present
    # in that container; this probes its loopback without a new host dependency.
    "$LT_DOCKER" exec "$1" python -c '
import json
import time
import urllib.error
import urllib.request

for name in ("live", "ready"):
    started = time.monotonic()
    code, body = "000", b""
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:8000/health/" + name, timeout=2
        ) as response:
            code = str(response.status)
            body = response.read(1024)
    except urllib.error.HTTPError as exc:
        code = str(exc.code)
        body = exc.read(1024)
    except Exception:
        pass
    elapsed_ms = round((time.monotonic() - started) * 1000)
    try:
        state = json.loads(body).get("status", "unknown")
    except (ValueError, AttributeError, TypeError):
        state = "unknown"
    if state not in ("ok", "ready", "degraded", "unready"):
        state = "unknown"
    print(f"{name}|{code}|{elapsed_ms}|{state}")
' 2>/dev/null
}

lt_pg_query() {
    # Credentials remain inside the DB container. Only fixed, metadata-only SQL
    # supplied by these scripts is sent; psql errors are not echoed to operators.
    "$LT_DOCKER" exec "$1" sh -c '
        export PGPASSWORD="${POSTGRES_PASSWORD:-}"
        export PGCONNECT_TIMEOUT=2
        exec psql -X -A -t -F "|" -v ON_ERROR_STOP=1 \
          -U "${POSTGRES_USER:-postgres}" \
          -d "${POSTGRES_DB:-${POSTGRES_USER:-postgres}}" -c "$1"
    ' _ "$2" 2>/dev/null
}

lt_pg_summary() {
    lt_pg_query "$1" '
SELECT
  count(*) FILTER (WHERE state = '\''active'\''),
  count(*) FILTER (WHERE state = '\''idle'\''),
  count(*) FILTER (WHERE state LIKE '\''idle in transaction%'\''),
  count(*) FILTER (WHERE state LIKE '\''idle in transaction%'\''
                    AND clock_timestamp() - xact_start > interval '\''1 second'\''),
  count(*) FILTER (WHERE state LIKE '\''idle in transaction%'\''
                    AND clock_timestamp() - xact_start > interval '\''5 seconds'\''),
  COALESCE(round(max(extract(epoch FROM clock_timestamp() - xact_start))::numeric, 1), 0)
FROM pg_stat_activity
WHERE backend_type = '\''client backend'\'' AND pid <> pg_backend_pid()
  AND datname IS NOT NULL;'
}

lt_pg_detail() {
    lt_pg_query "$1" '
SELECT datname, pid, COALESCE(state, '\''unknown'\''),
       COALESCE(wait_event_type, '\''-'\''), COALESCE(wait_event, '\''-'\''),
       COALESCE(round(extract(epoch FROM clock_timestamp() - xact_start)::numeric, 1)::text, '\''-'\'')
FROM pg_stat_activity
WHERE backend_type = '\''client backend'\'' AND pid <> pg_backend_pid()
  AND datname IS NOT NULL
ORDER BY xact_start NULLS LAST, pid
LIMIT 200;'
}

lt_log_summary() {
    "$LT_DOCKER" logs --timestamps --since "$2" "$1" 2>/dev/null |
    awk '
        /"POST \/widget-chat HTTP\/[0-9.]+" [0-9][0-9][0-9]/ {
            line = $0
            sub(/^.*"POST \/widget-chat HTTP\/[0-9.]+" /, "", line)
            code = substr(line, 1, 3)
            if (code == "200") widget_200++
            if (code ~ /^4/) widget_4xx++
            if (code ~ /^5/) widget_5xx++
            if (code == "503") widget_503++
        }
        /QueuePool limit of size/ { pool++ }
        /connection timed out/ { timeout++ }
        /Query log yazılamadı/ { querylog++ }
        END {
            printf "widget_200 %d\nwidget_4xx %d\nwidget_5xx %d\nwidget_503 %d\n", widget_200, widget_4xx, widget_5xx, widget_503
            printf "pool %d\ntimeout %d\nquerylog %d\n", pool, timeout, querylog
        }
    '
}

lt_log_events() {
    # Never persist raw backend log lines: they can contain user content.
    "$LT_DOCKER" logs --timestamps --since "$2" "$1" 2>/dev/null |
    awk '
        {
            stamp = $1 ~ /^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9:.]+Z$/ ? $1 : "timestamp-unavailable"
            if ($0 ~ /QueuePool limit of size/) print stamp " | QueuePool limit"
            if ($0 ~ /connection timed out/) print stamp " | connection timed out"
            if ($0 ~ /Query log yazılamadı/) print stamp " | query-log failure"
            if ($0 ~ /TimeoutError/) print stamp " | TimeoutError"
            if ($0 ~ /health/ && $0 ~ /" (500|503) /) print stamp " | health HTTP 5xx"
            if ($0 ~ /"POST \/widget-chat HTTP\/[0-9.]+" [0-9][0-9][0-9]/) {
                line = $0
                sub(/^.*"POST \/widget-chat HTTP\/[0-9.]+" /, "", line)
                code = substr(line, 1, 3)
                if (code ~ /^5/) print stamp " | widget HTTP " code
            }
        }
    '
}
