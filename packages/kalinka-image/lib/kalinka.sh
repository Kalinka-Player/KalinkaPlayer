# shellcheck shell=bash
# Installing Kalinka into the image and proving it works there. Defines only.

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

# What the base shipped, so a check on what this build installed can leave it out.
snapshot_packages() {
  installed_packages > "$WORK/packages.before"
}

install_kalinka() {
  local version="$1"
  log "Installing Kalinka"
  install -d "$ROOTFS/tmp/kalinka-install"
  install -m 755 "$REPO_ROOT/scripts/install-release.sh" \
    "$REPO_ROOT/scripts/install-renderer.sh" "$ROOTFS/tmp/kalinka-install/"
  # in_chroot clears the environment, so the display has to be asked for here.
  in_chroot env SKIP_SUPERVISOR=1 NO_APT_UPDATE=1 KALINKA_DISPLAY="${TARGET_DISPLAY:-0}" \
    /tmp/kalinka-install/install-release.sh ${version:+"$version"}
  rm -rf "$ROOTFS/tmp/kalinka-install"
}

verify_kalinka() {
  # Built now, so a first boot that cannot reach PyPI still plays.
  log "Pre-building the server venv"
  in_chroot /opt/kalinka/bootstrap.sh
  [ -x "$ROOTFS/opt/kalinka/venv/bin/kalinka-server" ] \
    || die "bootstrap.sh left no kalinka-server in the venv"

  # install-release.sh only warns when fpcalc does not come; an image must have it working.
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

  # install-release.sh installs the renderer best-effort; without it nothing plays.
  require_enabled multi-user.target kalinka.service kalinka-renderer.service
}
