#!/usr/bin/env bash
#
# install-renderer.sh — install a published Kalinka renderer release from GitHub.
#
# The renderer has its own release train (`kalinka-renderer-v*` tags),
# separate from the `kalinka-v*` app bundle: a playback box may run only the
# renderer, a server box may run no renderer at all. This script installs the
# package matching THIS machine — the .deb on Debian/Ubuntu, the .rpm on
# Fedora — for its architecture, and the package's systemd unit starts the
# renderer immediately and on every boot.
#
# Usage:
#   ./install-renderer.sh                          # latest renderer release
#   ./install-renderer.sh 0.1.0                    # specific version
#   ./install-renderer.sh kalinka-renderer-v0.1.0  # same, full tag form
#
# Env:
#   KALINKA_REPO   owner/repo to pull the release from (default: madenvel/KalinkaPlayer)
#   GITHUB_TOKEN   optional, only to avoid the 60-req/hr anonymous API limit
#
# The download runs as your user; only the install step uses sudo. Running the
# whole script under sudo is fine too.
set -euo pipefail

REPO="${KALINKA_REPO:-madenvel/KalinkaPlayer}"
API="https://api.github.com/repos/${REPO}/releases"
REQUEST="${1:-}"   # optional version or tag

have() { command -v "$1" >/dev/null 2>&1; }

die() { echo "error: $*" >&2; exit 1; }

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  if have sudo; then SUDO="sudo"; else
    die "not root and 'sudo' not found — re-run as root or install sudo"
  fi
fi

# --- detect package format, distro and architecture ---------------------------
ID="" VERSION_ID=""
[ -r /etc/os-release ] && . /etc/os-release
PLATFORM="${ID:-unknown}-${VERSION_ID:-unknown}"

if have dpkg; then
  FORMAT=deb
  ARCH="$(dpkg --print-architecture)"
  [ "$ARCH" = arm64 ] || [ "$ARCH" = amd64 ] \
    || die "unsupported architecture: $ARCH (need arm64 or amd64)"
elif have rpm; then
  FORMAT=rpm
  case "$(uname -m)" in
    aarch64) ARCH=aarch64 ;;
    x86_64)  ARCH=x86_64 ;;
    *) die "unsupported architecture: $(uname -m) (need aarch64 or x86_64)" ;;
  esac
else
  die "neither dpkg nor rpm found — install the flatpak instead (see packages/kalinka-renderer/flatpak/README.md)"
fi

have python3 || die "python3 is required to parse the release metadata"

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
    kalinka-renderer-v*) TAG="$REQUEST" ;;
    v*)                  TAG="kalinka-renderer-$REQUEST" ;;
    *)                   TAG="kalinka-renderer-v$REQUEST" ;;
  esac
  echo ">> Looking up release $TAG in $REPO ..."
  json="$(fetch "$API/tags/$TAG")" || die "release '$TAG' not found in $REPO"
else
  echo ">> Looking up the latest kalinka-renderer-v* release in $REPO ..."
  json="$(fetch "$API?per_page=30")" || die "could not query releases for $REPO"
fi

# Prints the tag on line 1 and the chosen asset URL on line 2. Debs are
# platform-specific (built against one distro's sonames): prefer the exact
# distro match, then another release of the same distro, then any deb of the
# right arch — apt's dependency check will refuse it if the sonames don't fit.
PARSER="$(mktemp --suffix=.py)"
TMPDIR_DL=""
cleanup() { rm -rf "$PARSER" ${TMPDIR_DL:+"$TMPDIR_DL"}; }
trap cleanup EXIT

cat > "$PARSER" <<'PY'
import sys, json
fmt, arch, platform = sys.argv[1:4]
data = json.load(sys.stdin)
rel = None
if isinstance(data, list):
    for r in data:                     # newest first; skip drafts/prereleases
        if r.get("draft") or r.get("prerelease"):
            continue
        if str(r.get("tag_name", "")).startswith("kalinka-renderer-v"):
            rel = r
            break
elif isinstance(data, dict) and data.get("assets") is not None:
    rel = data
if not rel:
    sys.stderr.write("no kalinka-renderer-v* release found\n")
    sys.exit(1)
names = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
if fmt == "deb":
    debs = sorted(n for n in names if n.endswith(f".{arch}.deb"))
    exact = [n for n in debs if n.endswith(f".{platform}.{arch}.deb")]
    family = [n for n in debs if f".{platform.split('-')[0]}-" in n]
    pick = next(iter(exact or family or debs), None)
    choice = names[pick] if pick else None
    if pick and not exact:
        sys.stderr.write(
            f"note: this release has no deb built for {platform}; trying {pick},\n"
            "      which apt will refuse if its library versions differ.\n")
else:
    choice = next((u for n, u in names.items()
                   if n.endswith(f".{arch}.rpm") and "debuginfo" not in n
                   and "debugsource" not in n), None)
if not choice:
    sys.stderr.write(f"release {rel.get('tag_name')} has no {fmt} for {arch}\n")
    sys.exit(1)
print(rel["tag_name"])
print(choice)
PY
parsed="$(printf '%s' "$json" | python3 "$PARSER" "$FORMAT" "$ARCH" "$PLATFORM")" \
  || die "failed to pick a package from the release"

TAG="$(printf '%s\n' "$parsed" | sed -n '1p')"
URL="$(printf '%s\n' "$parsed" | sed -n '2p')"
NAME="${URL##*/}"

echo ">> Release: $TAG   package: $NAME"

TMPDIR_DL="$(mktemp -d)"
chmod 755 "$TMPDIR_DL"
download "$URL" "$TMPDIR_DL/$NAME"

# --- install ------------------------------------------------------------------
if [ "$FORMAT" = deb ]; then
  APT_OPTS=(-o DPkg::Lock::Timeout=300)
  echo ">> Installing with apt ..."
  if ! $SUDO apt-get "${APT_OPTS[@]}" install -y --no-install-recommends "$TMPDIR_DL/$NAME"; then
    # apt finishes configuring whatever else the box left half-installed, and
    # that failure lands on this exit status too. Ask dpkg what actually
    # happened before blaming the package we came to install.
    pkg="$(dpkg-deb -f "$TMPDIR_DL/$NAME" Package 2>/dev/null || true)"
    want="$(dpkg-deb -f "$TMPDIR_DL/$NAME" Version 2>/dev/null || true)"
    have="$(dpkg-query -W -f='${Version}' "$pkg" 2>/dev/null || true)"
    if [ -z "$want" ] || [ "$want" != "$have" ]; then
      case "$NAME" in
        *".$PLATFORM.$ARCH.deb") die "apt could not install $NAME" ;;
      esac
      cat >&2 <<EOF

error: $NAME is built for another distro, and $TAG ships no deb for $PLATFORM,
       so apt cannot satisfy its library versions. Install the flatpak instead
       (see packages/kalinka-renderer/flatpak/README.md), or build a package on
       this machine with 'make renderer-deb'.
EOF
      exit 1
    fi
    echo ">> note: apt reported a failure, but $pkg $have is installed — the" >&2
    echo "   failure was another package's, left half-configured on this box." >&2
  fi
else
  echo ">> Installing with dnf ..."
  $SUDO dnf install -y "$TMPDIR_DL/$NAME"
fi

echo
echo ">> Done. $TAG installed."
echo "   The kalinka-renderer service is running and enabled at boot:"
echo "   systemctl status kalinka-renderer"
