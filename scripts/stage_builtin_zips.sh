#!/usr/bin/env bash
# Rebuild the builtin-mount zips (learn.zip, sessions.zip) into
# .dev-zips/ from the repo's live core_apps/ content — the dev-server analog of
# build_dmg.sh step 4e. Called by dev.sh once at startup AND by
# dev_server_run.sh before every watchfiles server restart, so an edit under
# core_apps/ lands in a fresh zip that the restarting server force-remounts
# (ensure_builtin_mounts' unconditional detach-refresh).
#
# A zip whose env override (FUSED_RENDER_<NAME>_ZIP) points somewhere OTHER
# than .dev-zips/ is skipped — the caller is deliberately mounting their own.
# The *.json sidecar exclusions mirror .gitignore: the dev server writes
# `<file>.json` next to any opened file, and a sidecar baked into the zip
# would pin the shipped view to this machine's session.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEV_ZIPS="$REPO_ROOT/.dev-zips"

command -v zip >/dev/null 2>&1 || exit 0
mkdir -p "$DEV_ZIPS"

stage() { # $1 = content dir name (learn|sessions), $2 = env var name
  local src="$REPO_ROOT/core_apps/$1" dest="$DEV_ZIPS/$1.zip" override="${!2:-}"
  [[ -d "$src" ]] || return 0
  if [[ -n "$override" && "$override" != "$dest" ]]; then
    return 0
  fi
  rm -f "$dest"
  # zip's * doesn't cross '/', hence the doubled patterns (same as build_dmg.sh).
  (cd "$src" && zip -qr -X "$dest" . \
    -x '.DS_Store' -x '*/.DS_Store' -x '__pycache__/*' -x '*/__pycache__/*' \
    -x '*.html.json' -x '*/*.html.json' -x '*.md.json' -x '*/*.md.json' \
    -x '*.py.json' -x '*/*.py.json' -x '*.txt.json' -x '*/*.txt.json')
}

stage learn FUSED_RENDER_LEARN_ZIP
stage sessions FUSED_RENDER_SESSIONS_ZIP
