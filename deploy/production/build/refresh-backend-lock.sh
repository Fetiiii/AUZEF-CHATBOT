#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)
LOCK_FILE=$REPO_ROOT/backend/requirements.lock
DOCKERFILE=$SCRIPT_DIR/backend-lock.Dockerfile
CPU_TORCH_VERSION=2.14.0+cpu
PYTORCH_CPU_INDEX=https://download.pytorch.org/whl/cpu
CPU_INDEX_DIRECTIVE="--extra-index-url $PYTORCH_CPU_INDEX"
RESOLVER_IMAGE=auzef-backend-lock-resolver:python311-cpu-$$
VALIDATION_IMAGE=auzef-backend-lock-validation:python311-cpu-$$
TEMP_FILE=$(mktemp "${TMPDIR:-/tmp}/auzef-requirements-lock.XXXXXX")
CANDIDATE_NAME=.requirements.lock.cpu.$$
CANDIDATE_LOCK=$REPO_ROOT/backend/$CANDIDATE_NAME

cleanup() {
    rm -f "$TEMP_FILE"
    rm -f "$CANDIDATE_LOCK"
    docker image rm "$RESOLVER_IMAGE" >/dev/null 2>&1 || true
    docker image rm "$VALIDATION_IMAGE" >/dev/null 2>&1 || true
}
trap cleanup EXIT HUP INT TERM

command -v docker >/dev/null 2>&1 || {
    printf '%s\n' 'ERROR: requirements.lock yenilemek icin Docker gerekli.' >&2
    exit 1
}
[ -f "$DOCKERFILE" ] || {
    printf 'ERROR: CPU lock Dockerfile bulunamadi: %s\n' "$DOCKERFILE" >&2
    exit 1
}

printf '%s\n' 'Production target: Python 3.11, Linux, CPU-only APP runtime.'
printf '%s\n' 'Development container snapshot GPU paketleri icerebilir; production baseline kaynagi degildir.'
printf '%s\n' 'Temiz CPU resolver graph olusturuluyor. Bu bilincli bir dependency update islemidir.'

docker build --no-cache --target resolver \
    --build-arg "TORCH_CPU_VERSION=$CPU_TORCH_VERSION" \
    --build-arg "PYTORCH_CPU_INDEX=$PYTORCH_CPU_INDEX" \
    --tag "$RESOLVER_IMAGE" \
    --file "$DOCKERFILE" \
    "$REPO_ROOT/backend"

PYTHON_VERSION=$(docker run --rm --entrypoint python "$RESOLVER_IMAGE" --version 2>&1)
case "$PYTHON_VERSION" in
    "Python 3.11."*) ;;
    *)
        printf 'ERROR: Python 3.11 bekleniyordu, alinan: %s\n' "$PYTHON_VERSION" >&2
        exit 1
        ;;
esac

docker run --rm --entrypoint python "$RESOLVER_IMAGE" \
    -m pip freeze | LC_ALL=C sort > "$TEMP_FILE"

if ! awk '
    /^[[:space:]]*($|#)/ { next }
    $0 !~ /^[A-Za-z0-9][A-Za-z0-9._-]*==[A-Za-z0-9][A-Za-z0-9.!+_-]*$/ { exit 1 }
' "$TEMP_FILE"; then
    printf '%s\n' 'ERROR: Generated graph exact package==version satirlari disinda veri iceriyor.' >&2
    exit 1
fi

if grep -Eiq '^(cuda-|nvidia-|triton==)' "$TEMP_FILE"; then
    printf '%s\n' 'ERROR: CPU resolver graph yasakli CUDA/NVIDIA/Triton package iceriyor.' >&2
    exit 1
fi
grep -Fxq "torch==$CPU_TORCH_VERSION" "$TEMP_FILE" || {
    printf 'ERROR: Beklenen CPU torch distribution bulunamadi: torch==%s\n' "$CPU_TORCH_VERSION" >&2
    exit 1
}

{
    printf '%s\n' '# Production target: Python 3.11, Linux, CPU-only APP runtime.'
    printf '%s\n' '# Resolved in a clean CPU-only container; development/GPU snapshots are not the baseline.'
    printf '# Source runtime: %s. Review the diff and run the full suite before commit.\n' "$PYTHON_VERSION"
    printf '%s\n' "$CPU_INDEX_DIRECTIVE"
    cat "$TEMP_FILE"
} > "$CANDIDATE_LOCK"

# Reinstall the candidate from scratch with the exact production command.
# Docker receives no GPU device, and the validation stage also masks GPU
# visibility before checking imports and torch.cuda.is_available().
docker build --no-cache --target validation \
    --build-arg "LOCK_SOURCE=$CANDIDATE_NAME" \
    --tag "$VALIDATION_IMAGE" \
    --file "$DOCKERFILE" \
    "$REPO_ROOT/backend"

mv "$CANDIDATE_LOCK" "$LOCK_FILE"

PACKAGE_COUNT=$(grep -Ec '^[A-Za-z0-9][A-Za-z0-9._-]*==' "$LOCK_FILE")
printf 'Updated %s with %s exact CPU packages.\n' "$LOCK_FILE" "$PACKAGE_COUNT"
