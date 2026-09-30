#!/usr/bin/env bash
# Daily internal-pilot decision_trace archive (single host, Docker Compose).
#
# The backend container logs through the json-file driver with rotation
# (5 x 20 MB) and a recreated container starts with an empty log, so pilot
# evaluation data would be lost. This script copies ONE UTC day of
# decision_trace lines out of `docker logs` into a gzip file.
#
#   deploy/internal-pilot/pilot_log_archive.sh [YYYY-MM-DD]   (default: yesterday UTC)
#
# Environment (optional):
#   PILOT_BACKEND_CONTAINER   default auzef_backend
#   PILOT_LOG_ARCHIVE_DIR     default /var/log/auzef-pilot
#   PILOT_LOG_RETENTION_DAYS  default 120 (older archives are deleted)
#
# Safety:
#   * only lines containing "decision_trace=" are kept; the trace never carries
#     raw message text, conversation tokens, IPs, TC/SMS data or API keys
#   * an existing archive for the day is never overwritten (idempotent)
#   * files are written 0640 in a 0750 directory, with a .sha256 sidecar
#
# Schedule once per day shortly after 00:00 UTC, e.g. cron:
#   15 0 * * *  /opt/auzef/deploy/internal-pilot/pilot_log_archive.sh >> /var/log/auzef-pilot/archive.log 2>&1
# Also run it manually BEFORE recreating the backend container (a recreated
# container loses its log), passing today's date to capture the partial day.
set -euo pipefail

CONTAINER="${PILOT_BACKEND_CONTAINER:-auzef_backend}"
ARCHIVE_DIR="${PILOT_LOG_ARCHIVE_DIR:-/var/log/auzef-pilot}"
RETENTION_DAYS="${PILOT_LOG_RETENTION_DAYS:-120}"
DAY="${1:-$(date -u -d 'yesterday' +%F)}"

if ! [[ "$DAY" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || ! date -u -d "$DAY" +%F >/dev/null 2>&1; then
  echo "invalid date: $DAY (expected YYYY-MM-DD)" >&2
  exit 2
fi
if ! [[ "$RETENTION_DAYS" =~ ^[0-9]+$ ]]; then
  echo "PILOT_LOG_RETENTION_DAYS must be a non-negative integer" >&2
  exit 2
fi

NEXT_DAY="$(date -u -d "$DAY + 1 day" +%F)"
TARGET="$ARCHIVE_DIR/decision-trace-$DAY.log.gz"
umask 027
mkdir -p "$ARCHIVE_DIR"
chmod 0750 "$ARCHIVE_DIR"

if [[ -e "$TARGET" ]]; then
  echo "archive exists, not overwritten: $TARGET"
else
  TMP="$(mktemp "$ARCHIVE_DIR/.decision-trace-$DAY.XXXXXX")"
  trap 'rm -f "$TMP"' EXIT
  docker logs --since "${DAY}T00:00:00Z" --until "${NEXT_DAY}T00:00:00Z" "$CONTAINER" 2>&1 \
    | grep -F 'decision_trace=' | gzip -9 > "$TMP" || true
  LINES="$(gzip -dc "$TMP" | wc -l | tr -d ' ')"
  mv "$TMP" "$TARGET"
  trap - EXIT
  chmod 0640 "$TARGET"
  (cd "$ARCHIVE_DIR" && sha256sum "$(basename "$TARGET")" > "$(basename "$TARGET").sha256")
  chmod 0640 "$TARGET.sha256"
  echo "archived $LINES decision_trace lines for $DAY -> $TARGET"
  if [[ "$LINES" == "0" ]]; then
    echo "WARNING: no decision_trace lines for $DAY (no traffic, container recreated, or wrong container)" >&2
  fi
fi

if [[ "$RETENTION_DAYS" -gt 0 ]]; then
  find "$ARCHIVE_DIR" -maxdepth 1 -type f -name 'decision-trace-*.log.gz*' -mtime +"$RETENTION_DAYS" -delete
fi
