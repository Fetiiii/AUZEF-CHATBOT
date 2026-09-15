#!/bin/sh
set -eu

usage() {
    printf 'Usage: %s <version> [--allow-dirty]\n' "$0" >&2
}

fail() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

VERSION=""
ALLOW_DIRTY=false
for argument in "$@"; do
    case "$argument" in
        --allow-dirty)
            ALLOW_DIRTY=true
            ;;
        --*)
            usage
            fail "Bilinmeyen option: $argument"
            ;;
        *)
            [ -z "$VERSION" ] || {
                usage
                fail "Yalniz bir version verilebilir."
            }
            VERSION=$argument
            ;;
    esac
done

[ -n "$VERSION" ] || {
    usage
    fail "Version argument zorunludur."
}

printf '%s\n' "$VERSION" | grep -Eq \
    '^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z]+([.-][0-9A-Za-z]+)*)?$' || \
    fail "Guvenli bir version kullanin (ornek: 4.1.0 veya 4.1.0-rc1)."

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)
RELEASE_NAME=auzef-$VERSION
DIST_DIR=$REPO_ROOT/dist/releases
ARTIFACT=$DIST_DIR/$RELEASE_NAME.tar.gz
LOCK_FILE=$REPO_ROOT/backend/requirements.lock
CPU_INDEX_DIRECTIVE='--extra-index-url https://download.pytorch.org/whl/cpu'

for command_name in docker git tar sha256sum mktemp find sort awk grep ln du date chmod; do
    command -v "$command_name" >/dev/null 2>&1 || \
        fail "Eksik build prerequisite: $command_name"
done

cd "$REPO_ROOT"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || fail "Git worktree bulunamadi."
[ -f "$LOCK_FILE" ] || fail "Committed dependency lock bulunamadi: $LOCK_FILE"
[ ! -e "$ARTIFACT" ] || fail "Artifact zaten var ve overwrite edilmeyecek: $ARTIFACT"

if ! awk -v cpu_index="$CPU_INDEX_DIRECTIVE" '
    /^[[:space:]]*($|#)/ { next }
    $0 == cpu_index { cpu_index_count++; next }
    $0 !~ /^[A-Za-z0-9][A-Za-z0-9._-]*==[A-Za-z0-9][A-Za-z0-9.!+_-]*$/ { exit 1 }
    END { if (cpu_index_count != 1) exit 1 }
' "$LOCK_FILE"; then
    fail "requirements.lock official CPU index ve exact package==version pinleri icermelidir."
fi
if grep -Eiq '^(cuda-|nvidia-|triton==)' "$LOCK_FILE"; then
    fail "CPU-only production lock cuda-*, nvidia-* veya triton package iceremez."
fi
grep -Eq '^torch==[0-9][A-Za-z0-9.!_-]*\+cpu$' "$LOCK_FILE" || \
    fail "CPU-only production lock torch +cpu distribution icermelidir."

DIRTY=false
if [ -n "$(git status --porcelain --untracked-files=normal)" ]; then
    DIRTY=true
    [ "$ALLOW_DIRTY" = true ] || \
        fail "Git worktree dirty. Commit/stash edin veya test icin --allow-dirty kullanin."
fi

GIT_COMMIT=$(git rev-parse HEAD)
GIT_REF=$(git symbolic-ref --quiet --short HEAD 2>/dev/null || git rev-parse --short HEAD)
BUILD_TIMESTAMP=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

mkdir -p "$DIST_DIR"
WORK_DIR=$(mktemp -d "$DIST_DIR/.build.XXXXXX")
RELEASE_DIR=$WORK_DIR/$RELEASE_NAME
TEMP_ARCHIVE=$WORK_DIR/$RELEASE_NAME.tar.gz
FRONTEND_IMAGE=auzef-frontend-release:$VERSION-$$
FRONTEND_CONTAINER=""

cleanup() {
    if [ -n "$FRONTEND_CONTAINER" ]; then
        docker rm "$FRONTEND_CONTAINER" >/dev/null 2>&1 || true
    fi
    docker image rm "$FRONTEND_IMAGE" >/dev/null 2>&1 || true
    rm -rf "$WORK_DIR"
}
trap cleanup EXIT HUP INT TERM

mkdir -p "$RELEASE_DIR/backend" "$RELEASE_DIR/frontend"

# Copy only the Python application runtime. Tests, caches, container tooling,
# mutable state and development entrypoints are deliberately outside this list.
(
    cd "$REPO_ROOT/backend"
    tar \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        --exclude='*.pyo' \
        -cf - \
        main.py requirements.txt requirements.lock \
        admin core integrations routers scripts services
) | (
    cd "$RELEASE_DIR/backend"
    tar -xf -
)

# Reuse the repository's Node 20 / npm ci --legacy-peer-deps / production
# Angular build stage, then extract only dist/chatbot-web from that image.
docker build --target build \
    --tag "$FRONTEND_IMAGE" \
    --file "$REPO_ROOT/chatbot-web/Dockerfile" \
    "$REPO_ROOT/chatbot-web"
FRONTEND_CONTAINER=$(docker create "$FRONTEND_IMAGE")
docker cp "$FRONTEND_CONTAINER:/app/dist/chatbot-web/." "$RELEASE_DIR/frontend/"
docker rm "$FRONTEND_CONTAINER" >/dev/null
FRONTEND_CONTAINER=""

printf '%s\n' "$VERSION" > "$RELEASE_DIR/VERSION"
{
    printf 'version=%s\n' "$VERSION"
    printf 'git_commit=%s\n' "$GIT_COMMIT"
    printf 'git_ref=%s\n' "$GIT_REF"
    printf 'built_at_utc=%s\n' "$BUILD_TIMESTAMP"
    printf 'dirty=%s\n' "$DIRTY"
} > "$RELEASE_DIR/BUILD_INFO"

[ -f "$RELEASE_DIR/backend/main.py" ] || fail "backend/main.py artifact'ta eksik."
[ -f "$RELEASE_DIR/backend/requirements.lock" ] || fail "backend/requirements.lock artifact'ta eksik."
[ -f "$RELEASE_DIR/frontend/index.html" ] || fail "Angular index.html build output'unda eksik."
[ -f "$RELEASE_DIR/frontend/widget.js" ] || fail "Angular widget.js build output'unda eksik."

if find "$RELEASE_DIR" -type d \
    \( -name tests -o -name __pycache__ -o -name .pytest_cache -o -name node_modules -o -name .venv \) \
    -print | grep -q .; then
    fail "Artifact yasakli runtime disi bir dizin iceriyor."
fi
if find "$RELEASE_DIR" -type f \( -name '.env' -o -name '.env.*' -o -name '*.pyc' -o -name '*.pyo' \) \
    -print | grep -q .; then
    fail "Artifact secret veya cache dosyasi iceriyor."
fi

(
    cd "$RELEASE_DIR"
    find . -type f ! -name SHA256SUMS -print | LC_ALL=C sort |
        while IFS= read -r file_path; do
            sha256sum "$file_path"
        done > SHA256SUMS
    sha256sum -c SHA256SUMS >/dev/null
)

tar -C "$WORK_DIR" -czf "$TEMP_ARCHIVE" "$RELEASE_NAME"
if tar -tzf "$TEMP_ARCHIVE" | awk -F/ -v expected="$RELEASE_NAME" '$1 != expected { exit 1 }'; then
    :
else
    fail "Archive birden fazla veya beklenmeyen top-level path iceriyor."
fi

# WORK_DIR dist/releases altinda oldugu icin hard link ayni filesystem'de ve
# atomic olarak olusur. ln mevcut hedefi overwrite etmez.
ln "$TEMP_ARCHIVE" "$ARTIFACT" || fail "Artifact publish edilemedi; hedef mevcut olabilir."
chmod 0644 "$ARTIFACT"

ARTIFACT_SIZE=$(du -h "$ARTIFACT" | awk '{print $1}')
printf 'Release artifact: %s (%s)\n' "$ARTIFACT" "$ARTIFACT_SIZE"
printf 'Version: %s, commit: %s, ref: %s, dirty=%s\n' \
    "$VERSION" "$GIT_COMMIT" "$GIT_REF" "$DIRTY"
