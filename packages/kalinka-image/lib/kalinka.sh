# shellcheck shell=bash
# Installing Kalinka into the image and proving it works there. Defines only.

# What fpcalc's recommends would drag onto a headless box; the README says why each is named.
EXCLUDED_PACKAGES=(va-driver-all vdpau-driver-all
                   mesa-va-drivers i965-va-driver intel-media-va-driver
                   libvdpau-va-gl1 mesa-vdpau-drivers mesa-vulkan-drivers
                   mesa-libgallium libllvm19 libgl1 'nvidia-*'
                   modemmanager ppp usb-modeswitch dnsmasq-base
                   xauth bash-completion ncurses-term groff-base)

# An unbounded journal is what filled the Pi this image replaces.
limit_journal() {
  mkdir -p "$ROOTFS/etc/systemd/journald.conf.d"
  printf '[Journal]\nSystemMaxUse=200M\n' \
    > "$ROOTFS/etc/systemd/journald.conf.d/kalinka.conf"
}

installed_packages() {
  in_chroot dpkg-query -W -f='${Package} ${Status}\n' \
    | awk '$4 == "installed" { print $1 }' | sort
}

# A base may ship refused packages itself; only what this build brings in counts.
snapshot_packages() {
  installed_packages > "$WORK/packages.before"
}

# Prints the packages in <after> but not <before> that match one of the named globs.
packages_landed() {
  local before="$1" after="$2" pkg pattern
  shift 2
  { grep -Fxv -f "$before" "$after" || true; } | while read -r pkg; do
    for pattern in "$@"; do
      # shellcheck disable=SC2254  # the pattern is meant as a glob
      case "$pkg" in $pattern) echo "$pkg"; break ;; esac
    done
  done
}

install_kalinka() {
  local version="$1"
  log "Installing Kalinka"
  install -d "$ROOTFS/tmp/kalinka-install"
  install -m 755 "$REPO_ROOT/scripts/install-release.sh" \
    "$REPO_ROOT/scripts/install-renderer.sh" "$ROOTFS/tmp/kalinka-install/"
  in_chroot env NO_APT_UPDATE=1 /tmp/kalinka-install/install-release.sh ${version:+"$version"}
  rm -rf "$ROOTFS/tmp/kalinka-install"
}

verify_kalinka() {
  # Built now, so a first boot that cannot reach PyPI still plays.
  log "Pre-building the server venv"
  in_chroot /opt/kalinka/bootstrap.sh
  [ -x "$ROOTFS/opt/kalinka/venv/bin/kalinka-server" ] \
    || die "bootstrap.sh left no kalinka-server in the venv"

  # Checked by use: a refused library leaves fpcalc installed and unable to load.
  in_chroot python3 -c "
import math, struct, wave
w = wave.open('/tmp/fpcalc-check.wav', 'w')
w.setnchannels(1); w.setsampwidth(2); w.setframerate(44100)
w.writeframes(b''.join(struct.pack('<h', int(12000 * math.sin(2 * math.pi * 440 * i / 44100)))
                       for i in range(44100 * 6)))
w.close()"
  local fingerprint
  fingerprint="$(in_chroot fpcalc /tmp/fpcalc-check.wav || true)"
  rm -f "$ROOTFS/tmp/fpcalc-check.wav"
  case "$fingerprint" in
    *FINGERPRINT=*) ;;
    *) die "fpcalc cannot fingerprint audio in this image" ;;
  esac

  local landed
  landed="$(packages_landed "$WORK/packages.before" <(installed_packages) "${EXCLUDED_PACKAGES[@]}")"
  [ -z "$landed" ] \
    || die "packages this image refuses landed anyway: $(echo "$landed" | tr '\n' ' ')"

  # install-release.sh installs the renderer best-effort; without it nothing plays.
  require_enabled multi-user.target kalinka.service kalinka-renderer.service
}
