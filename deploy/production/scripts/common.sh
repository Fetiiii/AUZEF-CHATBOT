#!/bin/sh
# Shared helpers for AUZEF production operations. This file is sourced by the
# command wrappers; it is not intended to be executed directly.

APP_ROOT=${AUZEF_APP_ROOT:-/opt/auzef}
RELEASES_DIR=$APP_ROOT/releases
CURRENT_LINK=$APP_ROOT/current
PREVIOUS_LINK=$APP_ROOT/previous
CONFIG_FILE=${AUZEF_CONFIG_FILE:-/etc/auzef/backend.env}
CACHE_ROOT=${AUZEF_CACHE_ROOT:-/var/cache/auzef}
STATE_ROOT=${AUZEF_STATE_ROOT:-/var/lib/auzef}
LOCK_FILE=${AUZEF_LOCK_FILE:-$STATE_ROOT/locks/operation.lock}
READY_URL=${AUZEF_READY_URL:-http://127.0.0.1/health/ready}
LIVE_URL=${AUZEF_LIVE_URL:-http://127.0.0.1/health/live}
READINESS_TIMEOUT=${AUZEF_READINESS_TIMEOUT:-180}
READINESS_INTERVAL=${AUZEF_READINESS_INTERVAL:-3}
SYSTEMCTL_CMD=${AUZEF_SYSTEMCTL:-systemctl}
CURL_CMD=${AUZEF_CURL:-curl}
JOURNALCTL_CMD=${AUZEF_JOURNALCTL:-journalctl}
PYTHON_CMD=${AUZEF_PYTHON:-python3.11}
CHOWN_CMD=${AUZEF_CHOWN:-chown}

log() {
    printf '%s\n' "$*"
}

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

require_command() {
    [ "$#" -eq 1 ] || die "require_command tek command ister."
    command -v "$1" >/dev/null 2>&1 || die "Gerekli command bulunamadi: $1"
}

require_root() {
    case "${AUZEF_SKIP_ROOT_CHECK:-0}" in
        1|true) return 0 ;;
    esac
    [ "$(id -u)" -eq 0 ] || die "Bu operation root veya sudo ile calistirilmalidir."
}

acquire_operation_lock() {
    command -v flock >/dev/null 2>&1 || die "Operation lock icin flock bulunamadi."
    lock_parent=$(dirname "$LOCK_FILE")
    mkdir -p "$lock_parent" || die "Operation lock dizini olusturulamadi."
    exec 9>"$LOCK_FILE"
    flock -n 9 || die "Baska bir AUZEF deploy/rollback operation'i calisiyor."
}

validate_version() {
    [ "$#" -eq 1 ] || return 1
    printf '%s\n' "$1" | grep -Eq \
        '^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z]+([.-][0-9A-Za-z]+)*)?$'
}

_env_value() {
    key=$1
    awk -v wanted="$key" '
        /^[[:space:]]*(#|$)/ { next }
        {
            line=$0
            sub(/^[[:space:]]*/, "", line)
            separator=index(line, "=")
            if (!separator) next
            name=substr(line, 1, separator - 1)
            gsub(/[[:space:]]+$/, "", name)
            if (name != wanted) next
            value=substr(line, separator + 1)
            sub(/^[[:space:]]*/, "", value)
            gsub(/[[:space:]]+$/, "", value)
            if (value ~ /^".*"$/ || value ~ /^\047.*\047$/) {
                value=substr(value, 2, length(value) - 2)
            }
            result=value
            found=1
        }
        END {
            if (!found) exit 1
            print result
        }
    ' "$CONFIG_FILE"
}

_require_env_value() {
    key=$1
    value=$(_env_value "$key" 2>/dev/null) || \
        die "$CONFIG_FILE icinde zorunlu $key tanimli degil."
    [ -n "$value" ] || die "$CONFIG_FILE icinde zorunlu $key bos."
    case "$value" in
        *CHANGE_ME*|*change_me*)
            die "$CONFIG_FILE icindeki $key halen template placeholder iceriyor."
            ;;
    esac
    AUZEF_PREFLIGHT_VALUE=$value
}

preflight_environment() {
    [ -f "$CONFIG_FILE" ] || die "Production environment dosyasi bulunamadi: $CONFIG_FILE"
    [ -r "$CONFIG_FILE" ] || die "Production environment dosyasi okunamiyor: $CONFIG_FILE"
    if awk '
        /^[[:space:]]*(#|$)/ { next }
        index($0, "=") {
            value=substr($0, index($0, "=") + 1)
            if (toupper(value) ~ /CHANGE_ME/) { found=1; exit }
        }
        END { exit(found ? 0 : 1) }
    ' "$CONFIG_FILE"; then
        die "$CONFIG_FILE aktif bir template placeholder iceriyor."
    fi

    _require_env_value ADMIN_DATABASE_URL
    _require_env_value CHAT_DATABASE_URL
    _require_env_value MEILI_URL
    _require_env_value QDRANT_HOST
    _require_env_value QDRANT_PORT
    case "$AUZEF_PREFLIGHT_VALUE" in
        ''|*[!0-9]*) die "QDRANT_PORT gecerli bir sayi olmali." ;;
    esac
    [ "$AUZEF_PREFLIGHT_VALUE" -ge 1 ] 2>/dev/null && \
        [ "$AUZEF_PREFLIGHT_VALUE" -le 65535 ] 2>/dev/null || \
        die "QDRANT_PORT 1-65535 araliginda olmali."

    _require_env_value ADMIN_AUTH_ENFORCED
    [ "$AUZEF_PREFLIGHT_VALUE" = "true" ] || \
        die "Production'da ADMIN_AUTH_ENFORCED=true olmali."
    _require_env_value ADMIN_COOKIE_SECURE
    [ "$AUZEF_PREFLIGHT_VALUE" = "true" ] || \
        die "Production'da ADMIN_COOKIE_SECURE=true olmali."
    _require_env_value HF_HOME
    [ "$AUZEF_PREFLIGHT_VALUE" = "$CACHE_ROOT/huggingface" ] || \
        die "HF_HOME production cache contract'i ile eslesmiyor."

    unset AUZEF_PREFLIGHT_VALUE value key
}

validate_prepared_release() {
    [ "$#" -eq 1 ] || die "validate_prepared_release bir version veya release path ister."
    release_arg=$1
    case "$release_arg" in
        */*) release_dir=$release_arg ;;
        *)
            validate_version "$release_arg" || die "Gecersiz release version: $release_arg"
            release_dir=$RELEASES_DIR/$release_arg
            ;;
    esac

    [ -d "$release_dir" ] || die "Prepared release bulunamadi: $release_dir"
    for required_path in \
        VERSION BUILD_INFO SHA256SUMS backend/main.py backend/requirements.lock \
        frontend/index.html frontend/widget.js .venv/bin/python .venv/bin/uvicorn
    do
        [ -f "$release_dir/$required_path" ] || \
            die "Prepared release eksik: $required_path"
    done
    [ -x "$release_dir/.venv/bin/python" ] || die "Prepared release Python executable degil."
    [ -x "$release_dir/.venv/bin/uvicorn" ] || die "Prepared release Uvicorn executable degil."

    release_version=$(sed -n '1p' "$release_dir/VERSION")
    validate_version "$release_version" || die "Release VERSION guvenli degil."
    [ "$(basename "$release_dir")" = "$release_version" ] || \
        die "Release directory ile VERSION eslesmiyor."
    case "$release_arg" in
        */*) : ;;
        *) [ "$release_arg" = "$release_version" ] || die "Istenen version ile VERSION eslesmiyor." ;;
    esac

    (
        cd "$release_dir"
        sha256sum -c SHA256SUMS >/dev/null
    ) || die "Prepared release checksum validation basarisiz."
    VALIDATED_RELEASE_DIR=$release_dir
    VALIDATED_RELEASE_VERSION=$release_version
}

atomic_symlink() {
    [ "$#" -eq 2 ] || return 1
    target=$1
    link=$2
    link_parent=$(dirname "$link")
    link_name=$(basename "$link")
    temporary=$link_parent/.${link_name}.tmp.$$
    [ -d "$link_parent" ] || return 1
    rm -f "$temporary" || return 1
    ln -s "$target" "$temporary" || return 1
    if ! mv -Tf "$temporary" "$link"; then
        rm -f "$temporary"
        return 1
    fi
}

resolve_release_link() {
    [ "$#" -eq 1 ] || return 1
    [ -L "$1" ] || return 1
    resolved=$(readlink -f -- "$1") || return 1
    [ -d "$resolved" ] || return 1
    releases_root=$(readlink -f -- "$RELEASES_DIR") || return 1
    [ "$(dirname "$resolved")" = "$releases_root" ] || return 1
    validate_version "$(basename "$resolved")" || return 1
    printf '%s\n' "$resolved"
}

_readiness_probe() {
    body_file=$(mktemp "${TMPDIR:-/tmp}/auzef-ready.XXXXXX") || return 1
    code=$("$CURL_CMD" --silent --show-error --output "$body_file" \
        --noproxy '*' --header 'Host: 127.0.0.1' \
        --connect-timeout "${AUZEF_HEALTH_CONNECT_TIMEOUT:-3}" \
        --max-time "${AUZEF_HEALTH_REQUEST_TIMEOUT:-10}" \
        --write-out '%{http_code}' "$READY_URL" 2>/dev/null) || code=000
    if [ "$code" = "200" ] && \
        grep -Eq '"status"[[:space:]]*:[[:space:]]*"(ready|degraded)"' "$body_file"; then
        rm -f "$body_file"
        return 0
    fi
    rm -f "$body_file"
    return 1
}

wait_for_readiness() {
    timeout=${1:-$READINESS_TIMEOUT}
    case "$timeout" in ''|*[!0-9]*) die "Readiness timeout sayi olmali." ;; esac
    started=$(date +%s)
    while :; do
        if _readiness_probe; then
            log "Readiness gate basarili (HTTP 200 ready/degraded)."
            return 0
        fi
        now=$(date +%s)
        [ $((now - started)) -lt "$timeout" ] || return 1
        sleep "$READINESS_INTERVAL"
    done
}

restart_backend() {
    "$SYSTEMCTL_CMD" restart auzef-backend.service
}

stop_backend() {
    "$SYSTEMCTL_CMD" stop auzef-backend.service
}
