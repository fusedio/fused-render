#!/usr/bin/env bash
# The watchfiles restart target for dev.sh: re-stage the builtin-mount zips
# from the live core_apps/ content, then exec the server. Because this runs on
# EVERY watchfiles restart (not once per dev.sh launch), an edit to a builtin
# sub-app (core_apps/learn, sessions) is picked up by the next
# restart: fresh zip -> startup's unconditional builtin-mount detach-refresh ->
# fresh content served. dev.sh's core_apps poll loop is what turns a non-.py
# edit into that restart.
#
#   dev_server_run.sh <python> [cli args…]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
bash "$REPO_ROOT/scripts/stage_builtin_zips.sh"

PY="$1"
shift
exec "$PY" -m fused_render.cli "$@"
