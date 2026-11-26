#!/usr/bin/env bash
#
# install-release.sh — install a published Kalinka backend release from GitHub.
#
# Downloads the .deb assets for a `kalinka-v*` GitHub release and installs them
# with apt so system dependencies are pulled in automatically. Every package
# in the bundle — server, plugins, SDK — is pure Python and arch-independent
# (_all), so the same artifacts install on any machine. On 32-bit ARM the
# bundle's kalinka-server-libs (armhf) adds the system libraries that the
# server's Python packages, built by piwheels, link against.
#
# Some pieces ship on their own release trains and are installed alongside,
# each best-effort so a lookup failure only warns:
#
#   * the browser player (kalinka-web), released from the app repo — the
#     server serves it at its root URL, and shows an install page without it;
#   * on request, the now-playing display for a screen on this machine
#     (kalinka-kiosk), also from the app repo, in per-arch packages. It brings
#     a graphics stack, so it is only installed when asked for, and upgraded
#     once it is there;
#   * the renderer (kalinka-renderer), which is what actually plays audio, in
#     per-arch packages picked by install-renderer.sh. Installing it here is
#     what makes a plain install play sound through this machine's sound card;
#     rendering boxes elsewhere on the network run that script themselves. It
#     goes in before the bundle so this machine is never left running a new
#     server against a renderer too old to talk to it.
#
# Re-running the script upgrades whatever is already installed, which is how
# the server's own auto-upgrade reaches all of them.
#
# Usage:
#   ./install-release.sh                 # install the latest kalinka-v* release
#   ./install-release.sh 3.2.0           # install a specific version
#   ./install-release.sh kalinka-v3.2.0  # same, full tag form
#
# Env:
#   KALINKA_REPO     owner/repo to pull the server release from (default: Kalinka-Player/KalinkaPlayer)
#   KALINKA_WEB_REPO owner/repo for the kalinka-web and kalinka-kiosk packages
#                    (default: Kalinka-Player/KalinkaAI)
#   KALINKA_WEB      set to 0 to skip installing the browser player
#   KALINKA_DISPLAY  set to 1 to install the now-playing display for a screen
#                    on this machine, and on a Raspberry Pi the KMS driver it
#                    draws through; 0 to leave an installed one un-upgraded
#   KALINKA_RENDERER set to 0 to skip installing the local renderer
#   KALINKA_RENDERER_INSTALLER  URL of the renderer installer to use instead of
#                    the published one at kalinkaplayer.com/install-renderer.sh
#                    (only needed to test an unmerged change)
#   GITHUB_TOKEN     optional, only to avoid the 60-req/hr anonymous API limit
#   NO_APT_UPDATE    set to 1 to skip `apt-get update` before installing
#   KALINKA_REINSTALL set to 1 to repair rather than upgrade: packages already
#                    at the release's version are installed again, and the
#                    server's Python environment is rebuilt from its wheels.
#                    The supervisor's Reinstall runs the installer this way.
#
# The download runs as your user; only the install step uses sudo. Running the
# whole script under sudo is fine too.
set -euo pipefail

REPO="${KALINKA_REPO:-Kalinka-Player/KalinkaPlayer}"
API="https://api.github.com/repos/${REPO}/releases"
RENDERER_INSTALLER="${KALINKA_RENDERER_INSTALLER:-https://kalinkaplayer.com/install-renderer.sh}"
REQUEST="${1:-}"   # optional version or tag
SELF="${BASH_SOURCE[0]:-}"   # empty when piped into bash

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

PRETTY_NAME=""
[ -r /etc/os-release ] && . /etc/os-release
ARCH="$(dpkg --print-architecture 2>/dev/null || true)"

# apt refuses the server on an older system python3, whatever comes first on PATH.
SYSTEM_PYTHON=/usr/bin/python3
[ -x "$SYSTEM_PYTHON" ] || SYSTEM_PYTHON=python3
if ! python_version="$("$SYSTEM_PYTHON" -c 'import sys
print("%d.%d.%d" % sys.version_info[:3])
sys.exit(sys.version_info < (3, 11))')"; then
  [ -n "$python_version" ] || die "$SYSTEM_PYTHON does not run, and the server needs it"
  die "Kalinka needs Python 3.11 or newer, and ${PRETTY_NAME:-this system} has Python ${python_version}.
  It installs on Debian 13, Raspberry Pi OS (64-bit) or DietPi on Debian 13, or Ubuntu 26.04:
  https://github.com/Kalinka-Player/KalinkaPlayer/blob/main/docs/installation.md#d-install-with-one-command"
fi

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

# The sibling script when run from a checkout, else the published one, downloaded.
installer_script() {  # installer_script <name> <url> -> path
  if [ -f "$SELF" ] && [ -r "$(dirname "$SELF")/$1" ]; then
    echo "$(cd "$(dirname "$SELF")" && pwd)/$1"
  else
    download "$2" "$TMPDIR_DL/$1" >&2 && echo "$TMPDIR_DL/$1"
  fi
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
# for each supported arch-independent (_all) deb; the server must be among them.
# The parser is written to a file (not piped via `python3 -`) so the JSON can be
# fed on stdin without colliding with the program source.
TMPDIR_DL=""
PARSER="$(mktemp --suffix=.py)"
APP_PARSER="$(mktemp --suffix=.py)"
cleanup() { rm -rf "$PARSER" "$APP_PARSER" ${TMPDIR_DL:+"$TMPDIR_DL"}; }
trap cleanup EXIT

# Picks the newest non-draft/prerelease's <prefix>*<suffix> asset URL, if any.
cat > "$APP_PARSER" <<'PY'
import sys, json
prefix, suffix = sys.argv[1], sys.argv[2]
data = json.load(sys.stdin)
for r in (data if isinstance(data, list) else [data]):
    if r.get("draft") or r.get("prerelease"):
        continue
    for a in r.get("assets", []):
        name = a.get("name", "")
        if name.startswith(prefix) and name.endswith(suffix):
            print(a["browser_download_url"])
            sys.exit(0)
PY
cat > "$PARSER" <<'PY'
import sys, json
arch = sys.argv[1]
# The bundle's one per-architecture package: the kiosk and the renderer are looked up on their own.
per_arch = ("kalinka-server-libs_",)
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
urls = []
for name, url in assets.items():
    if name.endswith("_all.deb") or (
        arch and name.startswith(per_arch) and name.endswith(f"_{arch}.deb")
    ):
        urls.append(url)
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
parsed="$(printf '%s' "$json" | python3 "$PARSER" "$ARCH")" \
  || die "failed to parse release metadata"

TAG="$(printf '%s\n' "$parsed" | sed -n '1p')"
mapfile -t URLS < <(printf '%s\n' "$parsed" | sed '1d')
[ "${#URLS[@]}" -gt 0 ] || die "no installable .deb assets found for $TAG"

# --- resolve the packages released from the app repo ---------------------------
WEB_REPO="${KALINKA_WEB_REPO:-Kalinka-Player/KalinkaAI}"

WANT_DISPLAY="${KALINKA_DISPLAY:-}"
if [ -z "$WANT_DISPLAY" ]; then
  case "$(dpkg-query -W -f='${Status}' kalinka-kiosk 2>/dev/null || true)" in
    *" installed") WANT_DISPLAY=1 ;;
    *) WANT_DISPLAY=0 ;;
  esac
fi

app_json=""
if [ "${KALINKA_WEB:-1}" != "0" ] || [ "$WANT_DISPLAY" = "1" ]; then
  echo ">> Looking up the latest app packages in $WEB_REPO ..."
  app_json="$(fetch "https://api.github.com/repos/${WEB_REPO}/releases?per_page=30")" \
    || app_json=""
fi

app_asset() {  # app_asset <prefix> <suffix> -> URL of the newest such asset
  [ -n "$app_json" ] && printf '%s' "$app_json" | python3 "$APP_PARSER" "$1" "$2"
}

if [ "${KALINKA_WEB:-1}" != "0" ]; then
  if web_url="$(app_asset kalinka-web_ _all.deb)" && [ -n "$web_url" ]; then
    URLS+=("$web_url")
    echo "   found ${web_url##*/}"
  else
    echo ">> note: no kalinka-web package found in $WEB_REPO — installing without the" >&2
    echo "   browser player (set KALINKA_WEB=0 to silence, or install it later)." >&2
  fi
fi

kiosk_url=""
if [ "$WANT_DISPLAY" = "1" ]; then
  if [ -n "$ARCH" ] \
     && kiosk_url="$(app_asset kalinka-kiosk_ "_${ARCH}.deb")" && [ -n "$kiosk_url" ]; then
    URLS+=("$kiosk_url")
    echo "   found ${kiosk_url##*/}"
  else
    echo ">> note: no kalinka-kiosk package for ${ARCH:-this machine} found in $WEB_REPO —" >&2
    echo "   installing without the now-playing display." >&2
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

# Existing supervisor installations join this upgrade; ordinary servers stay unchanged.
if [ "${SKIP_SUPERVISOR:-0}" != 1 ] && \
   [ "$(dpkg-query -W -f='${db:Status-Status}' kalinka-supervisor 2>/dev/null || true)" = installed ]; then
  supervisor_script="$(installer_script install-supervisor.sh \
    "${KALINKA_SUPERVISOR_INSTALLER:-https://raw.githubusercontent.com/$REPO/main/scripts/install-supervisor.sh}")"
  ALLOW_MISSING_RELEASE=1 bash "$supervisor_script"
fi

# --- renderer -----------------------------------------------------------------
# Ahead of the bundle, because a release can move the renderer protocol and a
# renderer speaks the version before its own as well as its own: new renderer
# with old server works, old renderer with new server may not. Going first
# means the box is never left in the pairing that does not.
#
# Best-effort either way: a platform with no renderer package still ends up
# with the server installed below.
install_renderer() {
  local script
  script="$(installer_script install-renderer.sh "$RENDERER_INSTALLER")" || return
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
# Recommends of the whole transaction would follow fpcalc's ffmpeg to Mesa and LLVM on a headless box.
APT_INSTALL=(apt-get "${APT_OPTS[@]}" install -y --no-install-recommends)

# Named, as the installs below take no recommends; each maps to what is lost without it.
declare -A EXTRAS=(
  [libchromaprint-tools]="AcoustID has no fpcalc to fingerprint tracks with"
  [nfs-common]="NFS shares cannot be mounted under Configuration → Storage"
)

# Best-effort: every one of them serves an optional feature.
install_extras() {
  # Simulated alone, so one apt cannot install costs only itself and the lock is waited out once.
  local pkg found=()
  for pkg in "${!EXTRAS[@]}"; do
    apt-get -s install --no-install-recommends --no-upgrade "$pkg" >/dev/null 2>&1 && found+=("$pkg")
  done
  [ "${#found[@]}" -gt 0 ] || return 0
  echo ">> Installing what Kalinka's optional features use ..."
  # Auto-upgrade reruns this, and Kalinka leaves updating the operating system to its owner.
  $SUDO "${APT_INSTALL[@]}" --no-upgrade "${found[@]}" || true
}

report_missing_extras() {
  local pkg missing=()
  for pkg in "${!EXTRAS[@]}"; do
    case "$(dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null || true)" in
      *" installed") ;;
      *) missing+=("$pkg")
         echo ">> note: $pkg could not be installed, so ${EXTRAS[$pkg]}." >&2 ;;
    esac
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    echo "   The rest of Kalinka works as it is. Add what is missing later with" >&2
    echo "   apt-get install --no-install-recommends ${missing[*]}" >&2
  fi
}

deb_package() { local name="${1##*/}"; printf '%s' "${name%%_*}"; }

deb_wanted() {  # deb_wanted <package>_<version>_<arch>.deb -> "installed <version>"
  local version="${1##*/}"
  version="${version#*_}"
  printf 'installed %s' "${version%_*}"
}

dpkg_state() {  # dpkg_state <package> -> "<status> <version>", or nothing when dpkg has no record
  local state
  state="$(dpkg-query -W -f='${db:Status-Status} ${Version}' "$1" 2>/dev/null || true)"
  printf '%s' "${state% }"
}

# Only a note: a repair on a system without the BlueZ it needs removes the Bluetooth plugin.
report_missing_debs() {
  local deb package wanted state missing=0
  for deb in "$TMPDIR_DL"/*.deb; do
    package="$(deb_package "$deb")"
    wanted="$(deb_wanted "$deb")"
    state="$(dpkg_state "$package")"
    [ "$state" = "$wanted" ] && continue
    echo ">> note: $package did not install: dpkg reports it as '${state:-unknown}', not '$wanted'." >&2
    missing=1
  done
  [ "$missing" = 0 ] || echo "   The server runs without what is missing; apt's output above says why." >&2
}

if [ "${NO_APT_UPDATE:-0}" != "1" ]; then
  echo ">> apt-get update"
  $SUDO apt-get "${APT_OPTS[@]}" update
fi

# The bundle's postinst starts kalinka.service, which looks for fpcalc only then.
install_extras

BUNDLE_OPTS=()
if [ "${KALINKA_REINSTALL:-0}" = 1 ]; then
  BUNDLE_OPTS=(--reinstall)
  echo ">> Rebuilding the server's Python environment ..."
  # The bundle's postinst restarts the server, whose bootstrap recreates a missing environment.
  $SUDO systemctl stop kalinka.service 2>/dev/null || true
  # Should the install fail, the server is still started rather than left stopped.
  trap 'cleanup; $SUDO systemctl --no-block start kalinka.service 2>/dev/null || true' EXIT
  $SUDO rm -rf /opt/kalinka/venv
fi

echo ">> Installing ${#URLS[@]} package(s) with apt ..."
# apt resolves install order among the bundle packages (server depends on the
# SDK) and pulls system dependencies from the configured repos. A leading ./ or
# absolute path tells apt these are local files, not repo package names.
if ! $SUDO "${APT_INSTALL[@]}" "${BUNDLE_OPTS[@]}" "$TMPDIR_DL"/*.deb; then
  echo ">> apt-get install failed; falling back to dpkg -i + apt-get -f install"
  $SUDO dpkg -i "$TMPDIR_DL"/*.deb || true
  $SUDO "${APT_INSTALL[@]}" -f \
    || die "$TAG is not installed: apt could not repair the failed install. apt's output above says why."
fi

# The repair succeeds by removing what it cannot configure, and a failed upgrade keeps the old server.
server_debs=("$TMPDIR_DL"/kalinka-server_*.deb)
server_wanted="$(deb_wanted "${server_debs[0]}")"
server_state="$(dpkg_state kalinka-server)"
[ "$server_state" = "$server_wanted" ] \
  || die "$TAG is not installed: dpkg reports kalinka-server as '${server_state:-unknown}', not '$server_wanted'. apt's output above says why."

# --- display driver -----------------------------------------------------------
# flutter-pi draws through KMS, which DietPi ships commented out beside a 16 MB GPU split.
BOOT_CONFIG=/boot/firmware/config.txt
KMS_OVERLAY='dtoverlay=vc4-kms-v3d([,[:blank:]].*)?'
KMS_OVERLAY_RE="^[[:blank:]]*$KMS_OVERLAY\$"

enable_display_driver() {
  $SUDO sed -Ei '/^[[:blank:]]*gpu_mem(_[0-9]+)?[[:blank:]]*=/d' "$BOOT_CONFIG"
  grep -Eq "$KMS_OVERLAY_RE" "$BOOT_CONFIG" && return 0
  $SUDO sed -Ei "0,/^[[:blank:]]*#[[:blank:]]*($KMS_OVERLAY)\$/s//\\1/" "$BOOT_CONFIG"
  grep -Eq "$KMS_OVERLAY_RE" "$BOOT_CONFIG" \
    || printf '\n[all]\ndtoverlay=vc4-kms-v3d,noaudio\n' | $SUDO tee -a "$BOOT_CONFIG" >/dev/null
}

BOOT_CONFIG_CHANGED=0
if [ -n "$kiosk_url" ] && [ -f "$BOOT_CONFIG" ]; then
  boot_config_before="$(cksum < "$BOOT_CONFIG")"
  enable_display_driver
  [ "$(cksum < "$BOOT_CONFIG")" = "$boot_config_before" ] || BOOT_CONFIG_CHANGED=1
fi

# --- report -------------------------------------------------------------------
echo
echo ">> Installed:"
for pkg in kalinka-server kalinka-plugin-sdk kalinka-plugin-localfiles \
           kalinka-plugin-musiccast kalinka-plugin-jamendo kalinka-plugin-upnp \
           kalinka-plugin-dummydevice kalinka-web kalinka-kiosk \
           kalinka-renderer; do
  dpkg-query -W -f='   ${Package} ${Version}\n' "$pkg" 2>/dev/null || true
done
report_missing_extras
report_missing_debs

echo
echo ">> Done. $TAG installed."
if [ "$BOOT_CONFIG_CHANGED" = 1 ]; then
  echo "   The display driver was switched on in $BOOT_CONFIG: reboot for the display to start."
fi
