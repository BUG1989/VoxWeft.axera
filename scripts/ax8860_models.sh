#!/usr/bin/env bash
# Optional read-only status helper. Service start/stop belongs to the board's
# deployment tooling; do not embed machine-specific model paths in this repo.
set -euo pipefail

ACTION=${1:-status}
BOARD_HOST=${AX_BOARD_HOST:?set AX_BOARD_HOST}
BOARD_USER=${AX_BOARD_USER:?set AX_BOARD_USER}
BOARD_SSH=${AX_BOARD_SSH:-ssh}

remote() {
  # shellcheck disable=SC2086
  $BOARD_SSH "$BOARD_USER@$BOARD_HOST" "$@"
}

case "$ACTION" in
  status)
    remote 'for p in 18081 18082 18083; do printf "%s " "$p"; curl -sS --max-time 3 "http://127.0.0.1:$p/health" || true; echo; done'
    ;;
  start|stop)
    echo "Start/stop is intentionally not embedded in this repository." >&2
    echo "Use your board deployment tool or a private local script." >&2
    exit 2
    ;;
  *)
    echo "usage: $0 {status}" >&2
    exit 2
    ;;
esac
