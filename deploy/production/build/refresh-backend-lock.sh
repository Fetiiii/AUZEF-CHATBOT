#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)
LOCK_FILE=$REPO_ROOT/backend/requirements.lock
IMAGE_TAG=auzef-backend-lock-refresh:python311-$$
TEMP_FILE=$(mktemp "${TMPDIR:-/tmp}/auzef-requirements-lock.XXXXXX")

cleanup() {
    rm -f "$TEMP_FILE"
    docker image rm "$IMAGE_TAG" >/dev/null 2>&1 || true
}
trap cleanup EXIT HUP INT TERM

command -v docker >/dev/null 2>&1 || {
    printf '%s\n' 'ERROR: requirements.lock yenilemek icin Docker gerekli.' >&2
    exit 1
}

printf '%s\n' 'Temiz Python 3.11 Linux image icinde dependency graph yeniden cozuluyor.'
printf '%s\n' 'Bu islem dependency update sayilir; olusan diff review ve full test gerektirir.'

docker build --no-cache --target base \
    --tag "$IMAGE_TAG" \
    --file "$REPO_ROOT/backend/Dockerfile" \
    "$REPO_ROOT/backend"

PYTHON_VERSION=$(docker run --rm --user root --entrypoint python "$IMAGE_TAG" --version 2>&1)
case "$PYTHON_VERSION" in
    "Python 3.11."*) ;;
    *)
        printf 'ERROR: Python 3.11 bekleniyordu, alinan: %s\n' "$PYTHON_VERSION" >&2
        exit 1
        ;;
esac

docker run --rm --user root --entrypoint python "$IMAGE_TAG" \
    -m pip freeze | LC_ALL=C sort > "$TEMP_FILE"

if ! awk '
    /^[[:space:]]*($|#)/ { next }
    $0 !~ /^[A-Za-z0-9][A-Za-z0-9._-]*==[A-Za-z0-9][A-Za-z0-9.!+_-]*$/ { exit 1 }
' "$TEMP_FILE"; then
    printf '%s\n' 'ERROR: Generated graph exact package==version satirlari disinda veri iceriyor.' >&2
    exit 1
fi

{
    printf '%s\n' '# Exact Python 3.11 Linux dependency graph regenerated from backend/requirements.txt.'
    printf '# Source runtime: %s. Review the diff and run the full suite before commit.\n' "$PYTHON_VERSION"
    cat "$TEMP_FILE"
} > "$LOCK_FILE"

PACKAGE_COUNT=$(grep -Ec '^[A-Za-z0-9][A-Za-z0-9._-]*==' "$LOCK_FILE")
printf 'Updated %s with %s exact packages.\n' "$LOCK_FILE" "$PACKAGE_COUNT"
