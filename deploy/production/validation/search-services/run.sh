#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
COMPOSE_FILE=$SCRIPT_DIR/compose.yml
PROJECT_NAME=auzef-search-compat-$$

cleanup() {
    docker compose --project-name "$PROJECT_NAME" --file "$COMPOSE_FILE" \
        down --volumes --remove-orphans --rmi local >/dev/null 2>&1 || true
}
trap cleanup EXIT HUP INT TERM

command -v docker >/dev/null 2>&1 || {
    printf '%s\n' 'ERROR: Docker compatibility validation icin gereklidir.' >&2
    exit 1
}
docker compose version >/dev/null 2>&1 || {
    printf '%s\n' 'ERROR: Docker Compose plugin bulunamadi.' >&2
    exit 1
}

printf '%s\n' 'Search service compatibility images hazirlaniyor...'
docker compose --project-name "$PROJECT_NAME" --file "$COMPOSE_FILE" \
    pull qdrant meilisearch
docker compose --project-name "$PROJECT_NAME" --file "$COMPOSE_FILE" \
    build validator
docker compose --project-name "$PROJECT_NAME" --file "$COMPOSE_FILE" \
    up --detach qdrant meilisearch

if ! docker compose --project-name "$PROJECT_NAME" --file "$COMPOSE_FILE" \
    run --rm --no-deps validator; then
    printf '%s\n' 'Compatibility validation failed; service logs follow.' >&2
    docker compose --project-name "$PROJECT_NAME" --file "$COMPOSE_FILE" \
        logs --no-color qdrant meilisearch >&2 || true
    exit 1
fi

printf '%s\n' 'Search service compatibility validation passed.'
