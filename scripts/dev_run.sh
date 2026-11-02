#!/usr/bin/env bash
#
# Foreground dev launcher for the Kalinka server.
#
# Runs the editable-installed server against a per-user "fakeroot" under
# $KALINKA_PREFIX (default ~/kalinka) instead of the system FHS paths, so no
# root, no systemd, and no /etc|/var writes are needed. Output is streamed to
# the terminal and tee'd to <prefix>/var/log/kalinka/server.log.
#
# In-app restart works without systemd: the server touches a trigger file in
# <prefix>/run/kalinka (just like the prod kalinka-restart.path unit watches),
# and this script watches that same file, SIGTERMs the server, and relaunches —
# picking up any edited Python (editable install). Renderer (C++) changes need
# `make renderer-build` and a restart of the renderer, not of this script.
#
# Web UI: served from <prefix>/usr/share/kalinka-web when present. Symlink an
# app-repo build there to test it — rebuilds are picked up per request, no
# server restart, just refresh the browser:
#   mkdir -p "$KALINKA_PREFIX/usr/share"
#   ln -sfn <app-repo>/build/web "$KALINKA_PREFIX/usr/share/kalinka-web"
#
# Stop with Ctrl-C. Extra args (e.g. --debug) are forwarded to the server.
#
# Output here carries the full timestamped log format. Set KALINKA_LOG_FORMAT
# to "journal" to see the terser format systemd gets: stdout below is a pipe to
# tee, so the journal format is never detected on its own.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

export KALINKA_PREFIX="${KALINKA_PREFIX:-$HOME/kalinka}"

# Reuse an already-active venv ($VIRTUAL_ENV) rather than creating/assuming a
# second one. Explicit $VENV (e.g. from the Makefile) always wins; otherwise an
# active venv is preferred, falling back to ./.venv.
VENV="${VENV:-${VIRTUAL_ENV:-$REPO_ROOT/.venv}}"
PY="$VENV/bin/python"

ETC="$KALINKA_PREFIX/etc/kalinka"
STATE="$KALINKA_PREFIX/var/lib/kalinka"
LOGDIR="$KALINKA_PREFIX/var/log/kalinka"
RUNDIR="$KALINKA_PREFIX/run/kalinka"
CACHE="$KALINKA_PREFIX/var/cache/kalinka"

CONFIG="$ETC/kalinka_conf.cfg"
STATEFILE="$STATE/kalinka_state.json"
LOGFILE="$LOGDIR/server.log"
TRIGGER="$RUNDIR/restart-request"
PENDING="$STATE/pending_installs.json"
LAST_INSTALL="$STATE/last_install.json"

# Keep numba's JIT cache inside the fakeroot (matches the prod NUMBA_CACHE_DIR).
export NUMBA_CACHE_DIR="$CACHE/numba"

MEDIA="$KALINKA_PREFIX/srv/kalinka/music"
mkdir -p "$ETC" "$STATE" "$MEDIA" "$LOGDIR" "$RUNDIR" "$CACHE/numba" "$CACHE/artwork"

# Seed a config on first run so the server has something to read.
if [ ! -f "$CONFIG" ] && [ -f "$REPO_ROOT/kalinka_conf.cfg" ]; then
  cp "$REPO_ROOT/kalinka_conf.cfg" "$CONFIG"
  echo "[dev] seeded config -> $CONFIG"
fi

if [ ! -x "$PY" ]; then
  echo "[dev] venv not found at $VENV — run 'make dev-setup' first" >&2
  exit 1
fi

# A stale trigger left by a previous run must not cause an immediate restart.
rm -f "$TRIGGER" 2>/dev/null

echo "[dev] KALINKA_PREFIX = $KALINKA_PREFIX"
echo "[dev] config         = $CONFIG"
echo "[dev] state          = $STATEFILE"
echo "[dev] logs           = $LOGFILE  (Ctrl-C to stop)"
echo "[dev] restart trigger= $TRIGGER  (in-app Restart works)"
echo

# Dev analog of prod's bootstrap.sh: drain optional-package requests (written by
# a settings save or /server/restart to pending_installs.json) through the SAME
# install_pending helper, feeding it allow-list manifests generated from each
# plugin's optional_packages module (the deb ships these as
# /opt/kalinka/allowed_packages).
drain_pending_installs() {
  [ -f "$PENDING" ] || return 0
  echo "[dev] pending optional-package install found -> running install_pending" | tee -a "$LOGFILE"

  local manifests
  manifests="$(mktemp -d)"
  local op name out
  for op in "$REPO_ROOT"/packages/kalinka-plugin-*/src/kalinka_plugin_*/optional_packages.py; do
    [ -f "$op" ] || continue
    # The module prints its allow-list manifest JSON on __main__; plugins that
    # declare none (or the SDK's spec module) print nothing and are skipped.
    # _load_manifests keys off the JSON contents, not the filename.
    name="$(basename "$(dirname "$op")")"
    if out="$("$PY" "$op" 2>/dev/null)" && [ -n "$out" ]; then
      printf '%s' "$out" > "$manifests/$name.json"
    fi
  done

  "$PY" -m kalinka_server.install_pending "$PENDING" "$manifests" "$LAST_INSTALL" \
      2>&1 | tee -a "$LOGFILE" \
    || echo "[dev] install_pending exited non-zero; continuing" | tee -a "$LOGFILE"
  rm -rf "$manifests"
}

STOP=0
child=""
on_signal() {
  STOP=1
  [ -n "$child" ] && kill -TERM "$child" 2>/dev/null
}
trap on_signal INT TERM

trig_mtime() { stat -c %Y "$TRIGGER" 2>/dev/null || echo 0; }

while :; do
  last="$(trig_mtime)"
  restart_requested=0

  drain_pending_installs

  # Run the server directly (process substitution, not a pipe) so $! is the
  # server's PID and we can signal it precisely; mirror its output to the log.
  "$PY" -m kalinka_server --config "$CONFIG" --state "$STATEFILE" "$@" \
      > >(tee -a "$LOGFILE") 2>&1 &
  child=$!

  while kill -0 "$child" 2>/dev/null; do
    if [ "$(trig_mtime)" != "$last" ]; then
      echo "[dev] restart requested -> restarting server"
      restart_requested=1
      rm -f "$TRIGGER" 2>/dev/null
      kill -TERM "$child" 2>/dev/null
      break
    fi
    sleep 0.5
  done

  wait "$child"
  rc=$?
  child=""

  if [ "$STOP" = 1 ]; then
    echo "[dev] stopped"
    break
  fi
  if [ "$restart_requested" = 1 ]; then
    echo "[dev] relaunching…"
    echo
    continue
  fi

  # Server exited on its own (crash / explicit exit). Surface it rather than
  # masking a real error behind an endless relaunch loop.
  echo "[dev] server exited (rc=$rc) without a restart request; not relaunching." >&2
  exit "$rc"
done
