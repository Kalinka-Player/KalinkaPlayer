#!/usr/bin/env bash
#
# install-release.sh — install a published Kalinka backend release from GitHub.
#
# Downloads the .deb assets for a `kalinka-v*` GitHub release and installs them
# with apt so system dependencies are pulled in automatically. Every package
# in the bundle — server, plugins, SDK — is pure Python and arch-independent
# (_all), so the same artifacts install on any machine.
#
# Two pieces ship on their own release trains and are installed alongside, each
# best-effort so a lookup failure only warns:
#
#   * the browser player (kalinka-web), released from the app repo — the
#     server serves it at its root URL, and shows an install page without it;
#   * the renderer (kalinka-renderer), which is what actually plays audio, in
#     per-arch packages picked by install-renderer.sh. Installing it here is
#     what makes a plain install play sound through this machine's sound card;
#     rendering boxes elsewhere on the network run that script themselves. It
#     goes in before the bundle so this machine is never left running a new
#     server against a renderer too old to talk to it.
#
# Re-running the script upgrades whatever is already installed, which is how
# the server's own auto-upgrade reaches all three.
#
# Usage:
#   ./install-release.sh                 # install the latest kalinka-v* release
#   ./install-release.sh 3.2.0           # install a specific version
#   ./install-release.sh kalinka-v3.2.0  # same, full tag form
#
# Env:
#   KALINKA_REPO     owner/repo to pull the server release from (default: madenvel/KalinkaPlayer)
#   KALINKA_WEB_REPO owner/repo for the kalinka-web package (default: madenvel/KalinkaAI)
#   KALINKA_WEB      set to 0 to skip installing the browser player
#   KALINKA_RENDERER set to 0 to skip installing the local renderer
#   KALINKA_RENDERER_INSTALLER  URL of the renderer installer to use instead of
#                    the published one at kalinkaplayer.com/install-renderer.sh
#                    (only needed to test an unmerged change)
#   GITHUB_TOKEN     optional, only to avoid the 60-req/hr anonymous API limit
#   NO_APT_UPDATE    set to 1 to skip `apt-get update` before installing
#
# The download runs as your user; only the install step uses sudo. Running the
# whole script under sudo is fine too.
set -euo pipefail

REPO="${KALINKA_REPO:-madenvel/KalinkaPlayer}"
API="https://api.github.com/repos/${REPO}/releases"
RENDERER_INSTALLER="${KALINKA_RENDERER_INSTALLER:-https://kalinkaplayer.com/install-renderer.sh}"
REQUEST="${1:-}"   # optional version or tag

have() { command -v "$1" >/dev/null 2>&1; }

die() { echo "error: $*" >&2; exit 1; }

# --- pick a privilege escalator for the install step --------------------------
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  if have sudo; then SUDO="sudo"; else
    die "not root and 'sudo' not found — re-run as root or install sudo"
  fi
fi

have python3 || die "python3 is required (it is also a runtime dependency of the server)"

# --- fetch helpers (curl or wget) ---------------------------------------------
AUTH=()
[ -n "${GITHUB_TOKEN:-}" ] && AUTH=(-H "Authorization: Bearer ${GITHUB_TOKEN}")

fetch() {  # fetch <url> -> stdout
  local url="$1"
  if have curl; then curl -fsSL "${AUTH[@]}" "$url"
  elif have wget; then wget -qO- "$url"
  else die "need 'curl' or 'wget' to download"; fi
}

download() {  # download <url> <dest>
  local url="$1" dest="$2"
  if have curl; then curl -fSL "${AUTH[@]}" -o "$dest" "$url"
  else wget -O "$dest" "$url"; fi
}

# --- resolve the release ------------------------------------------------------
if [ -n "$REQUEST" ]; then
  case "$REQUEST" in
    kalinka-v*) TAG="$REQUEST" ;;
    v*)         TAG="kalinka-$REQUEST" ;;
    *)          TAG="kalinka-v$REQUEST" ;;
  esac
  echo ">> Looking up release $TAG in $REPO ..."
  json="$(fetch "$API/tags/$TAG")" || die "release '$TAG' not found in $REPO"
else
  echo ">> Looking up the latest kalinka-v* release in $REPO ..."
  json="$(fetch "$API?per_page=30")" || die "could not query releases for $REPO"
fi

# Parse the release JSON: print the tag on line 1, then one asset URL per line
# for every arch-independent (_all) deb; the server must be among them.
# The parser is written to a file (not piped via `python3 -`) so the JSON can be
# fed on stdin without colliding with the program source.
TMPDIR_DL=""
PARSER="$(mktemp --suffix=.py)"
WEB_PARSER="$(mktemp --suffix=.py)"
cleanup() { rm -rf "$PARSER" "$WEB_PARSER" ${TMPDIR_DL:+"$TMPDIR_DL"}; }
trap cleanup EXIT

# Picks the newest non-draft/prerelease's kalinka-web_*_all.deb asset URL, if any.
cat > "$WEB_PARSER" <<'PY'
import sys, json
data = json.load(sys.stdin)
for r in (data if isinstance(data, list) else [data]):
    if r.get("draft") or r.get("prerelease"):
        continue
    for a in r.get("assets", []):
        name = a.get("name", "")
        if name.startswith("kalinka-web_") and name.endswith("_all.deb"):
            print(a["browser_download_url"])
            sys.exit(0)
PY
cat > "$PARSER" <<'PY'
import sys, json
data = json.load(sys.stdin)
rel = None
if isinstance(data, list):
    for r in data:                     # newest first; skip drafts/prereleases
        if r.get("draft") or r.get("prerelease"):
            continue
        if str(r.get("tag_name", "")).startswith("kalinka-v"):
            rel = r
            break
elif isinstance(data, dict) and data.get("assets") is not None:
    rel = data
if not rel:
    sys.stderr.write("no matching kalinka-v* release found\n")
    sys.exit(1)
assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
urls = [u for n, u in assets.items() if n.endswith("_all.deb")]
if not any(n.startswith("kalinka-server_") and n.endswith("_all.deb")
           for n in assets):
    sys.stderr.write(
        "release %s has no arch-independent server package; it predates the "
        "platform-agnostic server — install it with an older installer\n"
        % rel.get("tag_name"))
    sys.exit(1)
print(rel["tag_name"])
for u in urls:
    print(u)
PY
parsed="$(printf '%s' "$json" | python3 "$PARSER")" \
  || die "failed to parse release metadata"

TAG="$(printf '%s\n' "$parsed" | sed -n '1p')"
mapfile -t URLS < <(printf '%s\n' "$parsed" | sed '1d')
[ "${#URLS[@]}" -gt 0 ] || die "no installable .deb assets found for $TAG"

# --- resolve the browser player (kalinka-web) from the app repo ----------------
if [ "${KALINKA_WEB:-1}" != "0" ]; then
  WEB_REPO="${KALINKA_WEB_REPO:-madenvel/KalinkaAI}"
  echo ">> Looking up the latest kalinka-web package in $WEB_REPO ..."
  if web_json="$(fetch "https://api.github.com/repos/${WEB_REPO}/releases?per_page=30")" \
     && web_url="$(printf '%s' "$web_json" | python3 "$WEB_PARSER")" \
     && [ -n "$web_url" ]; then
    URLS+=("$web_url")
    echo "   found ${web_url##*/}"
  else
    echo ">> note: no kalinka-web package found in $WEB_REPO — installing without the" >&2
    echo "   browser player (set KALINKA_WEB=0 to silence, or install it later)." >&2
  fi
fi

echo ">> Release: $TAG   packages: ${#URLS[@]}"

# --- download into a temp dir -------------------------------------------------
# 0755 (not mktemp's default 0700) so apt's sandbox user `_apt` can traverse in
# to read the local .deb files; otherwise apt warns and falls back to fetching
# unsandboxed as root.
TMPDIR_DL="$(mktemp -d)"
chmod 755 "$TMPDIR_DL"

for url in "${URLS[@]}"; do
  name="${url##*/}"
  echo "   downloading $name"
  download "$url" "$TMPDIR_DL/$name"
done

# --- renderer -----------------------------------------------------------------
# Ahead of the bundle, because a release can move the renderer protocol and a
# renderer speaks the version before its own as well as its own: new renderer
# with old server works, old renderer with new server may not. Going first
# means the box is never left in the pairing that does not.
#
# Best-effort either way: a platform with no renderer package still ends up
# with the server installed below.
install_renderer() {
  local script self
  self="${BASH_SOURCE[0]:-}"
  if [ -f "$self" ] && [ -r "$(dirname "$self")/install-renderer.sh" ]; then
    # Run from a checkout: use the sibling script, not the published one.
    script="$(cd "$(dirname "$self")" && pwd)/install-renderer.sh"
  else
    script="$TMPDIR_DL/install-renderer.sh"
    download "$RENDERER_INSTALLER" "$script"
  fi
  KALINKA_REPO="$REPO" bash "$script"
}

if [ "${KALINKA_RENDERER:-1}" != "0" ]; then
  echo
  echo ">> Installing the renderer on this machine ..."
  if ! install_renderer; then
    echo ">> note: the renderer could not be installed — the server install" >&2
    echo "   below still goes ahead, and the browser player plays audio in the" >&2
    echo "   browser. Install it later with scripts/install-renderer.sh, or set" >&2
    echo "   KALINKA_RENDERER=0 to skip this step." >&2
  fi
fi

# --- install ------------------------------------------------------------------
# Wait for a held dpkg/apt lock (e.g. unattended-upgrades) instead of failing
# outright — this script also runs unattended from kalinka-upgrade.service.
APT_OPTS=(-o DPkg::Lock::Timeout=300)

if [ "${NO_APT_UPDATE:-0}" != "1" ]; then
  echo ">> apt-get update"
  $SUDO apt-get "${APT_OPTS[@]}" update
fi

echo ">> Installing ${#URLS[@]} package(s) with apt ..."
# apt resolves install order among the bundle packages (server depends on the
# SDK) and pulls system dependencies from the configured repos. A leading ./ or
# absolute path tells apt these are local files, not repo package names.
# Recommends bring fpcalc and Smart Search's build tools even where apt skips them, as on DietPi.
if ! $SUDO apt-get "${APT_OPTS[@]}" install -y --install-recommends "$TMPDIR_DL"/*.deb; then
  echo ">> apt-get install failed; falling back to dpkg -i + apt-get -f install"
  $SUDO dpkg -i "$TMPDIR_DL"/*.deb || true
  $SUDO apt-get "${APT_OPTS[@]}" -f install -y
fi

# --- report -------------------------------------------------------------------
echo
echo ">> Installed:"
if have dpkg-query; then
  for pkg in kalinka-server kalinka-plugin-sdk kalinka-plugin-localfiles \
             kalinka-plugin-musiccast kalinka-plugin-jamendo \
             kalinka-plugin-dummydevice kalinka-web kalinka-renderer; do
    dpkg-query -W -f='   ${Package} ${Version}\n' "$pkg" 2>/dev/null || true
  done
fi

echo
echo ">> Done. $TAG installed."
