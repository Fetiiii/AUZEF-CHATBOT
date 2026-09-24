#!/usr/bin/env bash
# Offline, non-destructive checks with a fake Docker command.
set -euo pipefail
tools_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
test_dir="$(mktemp -d)"
trap 'rm -rf -- "$test_dir"' EXIT

cat > "$test_dir/docker" <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail
case "$1" in
    compose)
        case "${*: -3}" in
            'ps -q backend') printf 'backend-id\n' ;;
            'ps -q db') printf 'db-id\n' ;;
            'ps -q meilisearch') printf 'meili-id\n' ;;
            'ps -q qdrant') printf 'qdrant-id\n' ;;
            'ps -q frontend') printf 'frontend-id\n' ;;
            *) [ "${*: -1}" = version ] || exit 1 ;;
        esac ;;
    inspect)
        if [[ "$3" == *State.Running* ]]; then
            printf 'true\n'
        elif [[ "$3" == *'.Image'* ]]; then
            printf 'sha256:fixture-image\n'
        elif [ "${FAKE_LOAD_CASE:-ok}" = bad_limits ]; then
            printf '4294967296|6442450944|2000000000|false\n'
        else
            printf '8589934592|12884901888|6000000000|false\n'
        fi ;;
    stats) printf 'auzef_backend|130.0%%|3.4GiB / 8GiB|42.5%%|540\n' ;;
    exec)
        if [ "$3" = python ]; then
            if [[ "$5" == *'/proc/1/cmdline'* ]]; then
                printf '2\n'
            elif [[ "$5" == *'multiprocessing.spawn'* ]]; then
                printf '2\n'
            elif [[ "$5" == *'json.load(response)'* ]]; then
                printf 'db_admin|ok\ndb_chat|ok\nmeilisearch|ok\nqdrant|ok\n'
            elif [[ "$5" == *'HF_HOME'* ]]; then
                :
            elif [ "${FAKE_LOAD_CASE:-ok}" = live_down ]; then
                printf 'live|503|12|unknown\nready|200|25|ready\n'
            else
                printf 'live|200|12|ok\nready|200|25|ready\n'
            fi
        elif [[ "$5" == *memory.events* ]]; then
            if [ "${FAKE_LOAD_CASE:-ok}" = memory_pressure ]; then
                if [ -e "$FAKE_LOAD_COUNTER_FILE" ]; then
                    memory_max=3
                else
                    memory_max=2
                    : > "$FAKE_LOAD_COUNTER_FILE"
                fi
                printf 'memory.max %s\nmemory.oom 0\nmemory.oom_kill 0\ncpu.nr_throttled 4\ncpu.throttled_usec 10000\n' "$memory_max"
            else
                printf 'memory.max 2\nmemory.oom 0\nmemory.oom_kill 0\ncpu.nr_throttled 4\ncpu.throttled_usec 10000\n'
            fi
        elif [[ "$5" == *PGPASSWORD* ]]; then
            case "${*: -1}" in
                *'SELECT 1;'*) printf '1\n' ;;
                *'count(*) FILTER'*)
                    [[ "${*: -1}" == *"state = 'active'"* ]]
                    printf '0|1|0|0|0|0\n' ;;
            esac
        else
            exit 1
        fi ;;
    logs)
        printf '2026-09-23T12:00:00.000000000Z INFO: 127.0.0.1 - "POST /widget-chat HTTP/1.1" 200 OK\n'
        printf '2026-09-23T12:00:01.000000000Z SECRET_STUDENT_MESSAGE do not capture\n'
        if [ "${FAKE_LOAD_CASE:-ok}" = incident ]; then
            printf '2026-09-23T12:00:02.000000000Z QueuePool limit of size 5 overflow 10 reached, connection timed out; SECRET_API_KEY\n'
            printf '2026-09-23T12:00:03.000000000Z INFO: 127.0.0.1 - "POST /widget-chat HTTP/1.1" 503 Service Unavailable\n'
        fi ;;
    *) exit 1 ;;
esac
MOCK
chmod +x "$test_dir/docker"
export AUZEF_DOCKER_BIN="$test_dir/docker"
export AUZEF_LOAD_STATE_DIR="$test_dir/state"
export AUZEF_LOAD_SNAPSHOT_ROOT="$test_dir/snapshots"
export AUZEF_EXPECT_GIT_COMMIT="$(git -C "$tools_dir/../.." rev-parse HEAD)"

if ! "$tools_dir/auzef-load-preflight" > "$test_dir/preflight"; then
    awk '{ print }' "$test_dir/preflight" >&2
    exit 1
fi
grep -q 'RESULT: PASS' "$test_dir/preflight"
if FAKE_LOAD_CASE=bad_limits "$tools_dir/auzef-load-preflight" > "$test_dir/preflight-bad"; then
    printf 'Expected bad limits to fail preflight\n' >&2
    exit 1
fi
grep -q 'RESULT: FAIL' "$test_dir/preflight-bad"

"$tools_dir/auzef-load-watch" --once --no-clear > "$test_dir/watch"
grep -q 'STATUS: OK' "$test_dir/watch"
FAKE_LOAD_CASE=memory_pressure FAKE_LOAD_COUNTER_FILE="$test_dir/cgroup-calls" \
    "$tools_dir/auzef-load-watch" --once --no-clear > "$test_dir/watch-warn"
grep -q 'STATUS: WARN' "$test_dir/watch-warn"
FAKE_LOAD_CASE=live_down "$tools_dir/auzef-load-watch" --once --no-clear > "$test_dir/watch-live-down"
grep -q 'STATUS: STOP' "$test_dir/watch-live-down"
FAKE_LOAD_CASE=incident "$tools_dir/auzef-load-watch" --once --no-clear > "$test_dir/watch-incident"
grep -q 'STATUS: STOP' "$test_dir/watch-incident"

snapshot_line="$(FAKE_LOAD_CASE=incident "$tools_dir/auzef-load-snapshot")"
snapshot_dir="${snapshot_line#Snapshot: }"
[ "$(stat -c '%a' "$snapshot_dir")" = 700 ]
for file in summary.txt docker-stats.txt backend-limits.txt memory-events.txt cpu-stat.txt \
            postgres-activity-summary.txt health.txt backend-errors.txt widget-statuses.txt; do
    [ -f "$snapshot_dir/$file" ]
done
grep -q 'QueuePool limit' "$snapshot_dir/backend-errors.txt"
if grep -R -E 'SECRET_STUDENT_MESSAGE|SECRET_API_KEY' "$snapshot_dir" >/dev/null; then
    printf 'Sensitive fake log content leaked into snapshot\n' >&2
    exit 1
fi
printf 'load-test smoke: PASS\n'
