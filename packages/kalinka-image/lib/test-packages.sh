# shellcheck shell=bash

stage_test_packages() {
  local source="$1" destination="$2" architecture="$3" file package version arch filename
  local -A seen=()
  [ -d "$source" ] || die "TEST_DEBS_DIR is not a directory: $source"
  mkdir -p "$destination"
  : > "$destination/packages.tsv"
  for file in "$source"/*.deb; do
    [ -f "$file" ] || continue
    package="$(dpkg-deb -f "$file" Package)"
    version="$(dpkg-deb -f "$file" Version)"
    arch="$(dpkg-deb -f "$file" Architecture)"
    case "$package" in kalinka-*) ;; *) die "not a Kalinka test package: $package" ;; esac
    [ "$arch" = all ] || [ "$arch" = "$architecture" ] || die "wrong architecture for $package: $arch"
    [ -z "${seen[$package]:-}" ] || die "more than one test package for $package"
    seen[$package]=1
    filename="$(basename "$file")"
    cp "$file" "$destination/$filename"
    printf '%s\t%s\t%s\t%s\t%s\n' "$package" "$version" "$arch" "$filename" \
      "$(sha256sum "$destination/$filename" | cut -d ' ' -f1)" >> "$destination/packages.tsv"
  done
  for package in kalinka-server kalinka-plugin-sdk kalinka-renderer kalinka-plugin-bluetooth; do
    [ -n "${seen[$package]:-}" ] || die "missing test package: $package"
  done
}

configure_test_inputs() {
  local root="$1" manifest="$2"
  mkdir -p "$root/etc/kalinka"
  python3 - "$root/etc/kalinka/kalinka_conf.cfg" "$manifest" <<'PY'
import json, pathlib, sys
path, manifest = map(pathlib.Path, sys.argv[1:])
data = json.loads(path.read_text()) if path.exists() else {}
packages = {line.split('\t')[0] for line in manifest.read_text().splitlines()}
data['base_config.server.auto_upgrade'] = False
for plugin in ('bluetooth', 'airplay'):
    if f'kalinka-plugin-{plugin}' in packages:
        data[f'input_modules.{plugin}.enabled'] = True
path.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')
PY
  chmod 600 "$root/etc/kalinka/kalinka_conf.cfg"
}

install_test_packages() {
  local source="${IGconf_kalinka_test_debs:-}" file
  [ -n "$source" ] || return 0
  log "Installing local Bluetooth test packages"
  mkdir -p "$ROOTFS/tmp/kalinka-test-debs"
  local -a packages=()
  for file in "$source"/*.deb; do
    cp "$file" "$ROOTFS/tmp/kalinka-test-debs/"
    packages+=("/tmp/kalinka-test-debs/$(basename "$file")")
  done
  in_chroot apt-get install -y --no-install-recommends --allow-downgrades "${packages[@]}"
  install -D -m 644 "$source/packages.tsv" "$ROOTFS/usr/share/kalinka/test-packages.tsv"
  configure_test_inputs "$ROOTFS" "$source/packages.tsv"
  in_chroot chown kalusr:kalusr /etc/kalinka/kalinka_conf.cfg
  in_chroot systemctl disable bluealsa.service bluealsa-aplay.service 2>/dev/null || true
  in_chroot systemctl enable kalinka-bluealsa.service
  rm -rf "$ROOTFS/tmp/kalinka-test-debs"
}

verify_test_packages() {
  local source="${IGconf_kalinka_test_debs:-}" package version _metadata actual
  [ -n "$source" ] || return 0
  while IFS=$'\t' read -r package version _metadata; do
    # shellcheck disable=SC2016
    actual="$(in_chroot dpkg-query -W -f='${Version}' "$package")"
    [ "$actual" = "$version" ] || die "$package is $actual, expected $version"
  done < "$source/packages.tsv"
  in_chroot /opt/kalinka/venv/bin/python -c \
    'from kalinka_plugin_sdk import LivePolicy; from kalinka_plugin_bluetooth import KalinkaPluginBluetooth'
  if grep -q '^kalinka-plugin-airplay[[:space:]]' "$source/packages.tsv"; then
    in_chroot /opt/kalinka/venv/bin/python -c 'import kalinka_plugin_airplay'
    in_chroot /usr/libexec/kalinka-plugin-airplay/shairport-sync -V
  fi
  in_chroot /usr/bin/kalinka-renderer --help >/dev/null
  in_chroot /opt/kalinka/venv/bin/pip check
  require_enabled multi-user.target kalinka-bluealsa.service
  [ -f "$ROOTFS/usr/share/kalinka-web/index.html" ] || die 'the test image has no browser UI'
  [ "$(in_chroot stat -c '%U %a' /etc/kalinka/kalinka_conf.cfg)" = 'kalusr 600' ] \
    || die 'the server cannot read its test settings'
}
