#!/usr/bin/env bash
# Install the separately released supervisor through the existing Python/apt path.
set -euo pipefail
repo="${KALINKA_REPO:-madenvel/KalinkaPlayer}"
request="${1:-}"
arch="$(dpkg --print-architecture)"
case "$arch" in amd64|arm64) ;; *) echo "Unsupported supervisor architecture: $arch" >&2; exit 1;; esac
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
chmod 755 "$stage"
auth=()
[ -z "${GITHUB_TOKEN:-}" ] || auth=(-H "Authorization: Bearer $GITHUB_TOKEN")
api="https://api.github.com/repos/$repo/releases"
if [ -n "$request" ]; then
  case "$request" in kalinka-supervisor-v*) tag="$request";; v*) tag="kalinka-supervisor-$request";; *) tag="kalinka-supervisor-v$request";; esac
  api="$api/tags/$tag"
else
  api="$api?per_page=100"
fi
curl -fsSL --retry 3 "${auth[@]}" "$api" -o "$stage/release.json"
if python3 - "$stage/release.json" "$arch" > "$stage/selection" <<'PY'
import json, re, sys
from urllib.parse import urlparse
with open(sys.argv[1]) as f:
    data = json.load(f)
releases = data if isinstance(data, list) else [data]
release = next((r for r in releases if not r.get('draft') and not r.get('prerelease')
                and r.get('tag_name', '').startswith('kalinka-supervisor-v')), None)
if release is None:
    print('No stable supervisor release found', file=sys.stderr)
    sys.exit(3)
version = release['tag_name'].removeprefix('kalinka-supervisor-v')
if not re.fullmatch(r'[0-9][0-9A-Za-z.+~:-]*', version):
    sys.exit('Invalid supervisor release version')
name = f'kalinka-supervisor_{version}_{sys.argv[2]}.deb'
assets = {a['name']: a['browser_download_url'] for a in release.get('assets', [])}
for asset in (name, 'SHA256SUMS'):
    url = assets.get(asset, '')
    if urlparse(url).scheme != 'https':
        sys.exit(f'Missing HTTPS asset: {asset}')
print(name)
print(assets[name])
print(assets['SHA256SUMS'])
print(version)
PY
then
  :
else
  status=$?
  if [ "$status" = 3 ] && [ "${ALLOW_MISSING_RELEASE:-0}" = 1 ] && [ -z "$request" ]; then
    exit 0
  fi
  exit "$status"
fi
mapfile -t selection < "$stage/selection"
name="${selection[0]}"
curl -fsSL --retry 3 "${auth[@]}" "${selection[1]}" -o "$stage/$name"
curl -fsSL --retry 3 "${auth[@]}" "${selection[2]}" -o "$stage/SHA256SUMS"
python3 - "$stage" "$name" <<'PY'
import hashlib, pathlib, sys
root, name = pathlib.Path(sys.argv[1]), sys.argv[2]
entries = [line.split() for line in (root / 'SHA256SUMS').read_text().splitlines()]
expected = [e[0] for e in entries if len(e) == 2 and e[1].lstrip('*').removeprefix('./') == name]
if len(expected) != 1 or hashlib.sha256((root / name).read_bytes()).hexdigest() != expected[0]:
    sys.exit('Supervisor checksum mismatch or missing entry')
PY
[ "$(dpkg-deb -f "$stage/$name" Package)" = kalinka-supervisor ]
[ "$(dpkg-deb -f "$stage/$name" Architecture)" = "$arch" ]
[ "$(dpkg-deb -f "$stage/$name" Version)" = "${selection[3]}" ]
sudo_cmd=()
[ "$(id -u)" -eq 0 ] || sudo_cmd=(sudo)
# A release check must not silently downgrade a deliberately newer installation.
installed="$(dpkg-query -W -f='${db:Status-Status} ${Version}' kalinka-supervisor 2>/dev/null || true)"
case "$installed" in installed\ *) installed="${installed#installed }";; *) installed="";; esac
if [ -n "$installed" ] && dpkg --compare-versions "$installed" ge "${selection[3]}"; then
  echo "Supervisor $installed is already current"
  exit 0
fi
"${sudo_cmd[@]}" apt-get -o DPkg::Lock::Timeout=300 install -y --no-install-recommends "$stage/$name"
