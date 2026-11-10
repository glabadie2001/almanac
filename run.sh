#!/usr/bin/env bash
# Almanac launcher for Git Bash / MSYS2 / WSL / POSIX shells.
#
# Usage:
#   ./run.sh                        Start Almanac + Mesh Hub and open the browser (or open tab if already running)
#   ./run.sh url                    Print the signed-in dashboard URL
#   ./run.sh mesh roster            Run Mesh CLI (roster | context | contacts | send | wait)
#   ./run.sh test                   Run the unit test suite
#   ./run.sh --port 8820 --open     Pass any flags directly to `python -m almanac`

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

find_python() {
  if [[ -n "${PYTHON:-}" ]] && "$PYTHON" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >/dev/null 2>&1; then
    printf '%s\n' "$PYTHON"
    return 0
  fi
  for cand in python python3; do
    if command -v "$cand" >/dev/null 2>&1 && "$cand" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >/dev/null 2>&1; then
      printf '%s\n' "$cand"
      return 0
    fi
  done
  if command -v py >/dev/null 2>&1 && py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >/dev/null 2>&1; then
    printf '%s\n' "py -3"
    return 0
  fi
  echo "Error: Python 3.11+ not found on PATH (set PYTHON=/path/to/python)." >&2
  return 1
}

PY_CMD="$(find_python)"
# shellcheck disable=SC2206
PY=($PY_CMD)

open_url() {
  local target="$1"
  if command -v cygstart >/dev/null 2>&1; then
    cygstart "$target" >/dev/null 2>&1 || true
  elif command -v cmd.exe >/dev/null 2>&1; then
    cmd.exe /c start "" "$target" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$target" >/dev/null 2>&1 || true
  elif command -v open >/dev/null 2>&1; then
    open "$target" >/dev/null 2>&1 || true
  fi
}

case "${1:-}" in
  "")
    # If Almanac is already listening on its configured port, open the signed-in tab instead of failing on port bind
    if "${PY[@]}" -c "
import json, urllib.request
from almanac.config import load
port = load('config.json')['port']
with urllib.request.urlopen(f'http://127.0.0.1:{port}/ident', timeout=1.0) as r:
    doc = json.loads(r.read().decode('utf-8'))
raise SystemExit(0 if doc.get('kind') == 'almanac' else 1)
" >/dev/null 2>&1; then
      LOGIN_URL="$("${PY[@]}" -m almanac --url)"
      echo "Almanac is already running -> $LOGIN_URL"
      open_url "$LOGIN_URL"
      exit 0
    fi
    exec "${PY[@]}" -m almanac --open
    ;;
  restart)
    shift
    "${PY[@]}" -c "
import json, os, signal, time, urllib.request
from almanac.config import load
port = load('config.json')['port']
with urllib.request.urlopen(f'http://127.0.0.1:{port}/ident', timeout=1.0) as r:
    doc = json.loads(r.read().decode('utf-8'))
if doc.get('kind') == 'almanac' and int(doc.get('pid') or 0) > 0:
    os.kill(int(doc['pid']), signal.SIGTERM)
    time.sleep(0.4)
" >/dev/null 2>&1 || true
    exec "${PY[@]}" -m almanac --open "$@"
    ;;
  url)
    shift
    exec "${PY[@]}" -m almanac --url "$@"
    ;;
  ingest)
    shift
    exec "${PY[@]}" -m almanac --ingest "$@"
    ;;
  mesh)
    shift
    exec "${PY[@]}" -m almanac.mcp_mesh --cli "$@"
    ;;
  test|tests)
    shift
    exec "${PY[@]}" -m unittest discover tests "$@"
    ;;
  *)
    exec "${PY[@]}" -m almanac "$@"
    ;;
esac

