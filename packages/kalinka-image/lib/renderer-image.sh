# shellcheck shell=bash
# The renderer-only image: the renderer installed alone, and proof that the
# finished root has what it needs and nothing a renderer does not. Defines only.

# Usage: install_renderer_only [DEB]   a local package, or the latest release's
install_renderer_only() {
  local deb="${1:-}"
  log "Installing the renderer"
  install -d "$ROOTFS/tmp/kalinka-install"
  if [ -n "$deb" ]; then
    install -m 644 "$deb" "$ROOTFS/tmp/kalinka-install/"
    apt_install --no-install-recommends "/tmp/kalinka-install/${deb##*/}"
  else
    install -m 755 "$REPO_ROOT/scripts/install-renderer.sh" "$ROOTFS/tmp/kalinka-install/"
    in_chroot env KALINKA_SUPERVISOR=0 /tmp/kalinka-install/install-renderer.sh
  fi
  rm -rf "$ROOTFS/tmp/kalinka-install"
}

# This image applies none of Imager's settings, but a card Imager customised must still start normally.
ignore_imager_first_run() {
  install -D -m 644 "$SCRIPT_DIR/overlays/debootstrap$IMAGER_TARGET" "$ROOTFS$IMAGER_TARGET"
  mask_imager_first_run
}

installed_in_root() {
  compgen -G "$ROOTFS/var/lib/dpkg/info/$1.list" > /dev/null \
    || compgen -G "$ROOTFS/var/lib/dpkg/info/$1:*.list" > /dev/null
}

eth0_takes_dhcp() {
  local file
  for file in "$ROOTFS"/etc/systemd/network/*.network; do
    grep -qsx 'Name=eth0' "$file" && grep -qsxE 'DHCP=(yes|ipv4)' "$file" && return 0
  done
  return 1
}

# The image has no login, so a cable that brings no address up is a box nobody can look into.
verify_renderer_network() {
  local link
  require_enabled multi-user.target systemd-networkd.service
  require_enabled sysinit.target systemd-resolved.service
  # A Pi 1's Ethernet sits on USB, and udev names a USB network device by its MAC.
  for link in 99-default.link 73-usb-net-by-mac.link; do
    [ "$(readlink "$ROOTFS/etc/systemd/network/$link")" = /dev/null ] \
      || die "$link is not masked, so the Ethernet would not be named eth0"
  done
  eth0_takes_dhcp || die "no network file asks for an address on eth0 by DHCP"
  [[ "$(readlink "$ROOTFS/etc/resolv.conf")" == */run/systemd/resolve/* ]] \
    || die "/etc/resolv.conf does not point at systemd-resolved, so the DNS servers DHCP hands out go unused"
}

# Run on the finished root: the server asks the renderer to upgrade, and the
# renderer can only do that through its watcher and install-renderer.sh.
verify_renderer_image() {
  local tool package
  installed_in_root kalinka-renderer || die "the renderer is not installed"
  require_enabled multi-user.target kalinka-renderer.service kalinka-renderer-upgrade.path
  require_enabled sysinit.target systemd-timesyncd.service
  verify_renderer_network
  [ -x "$ROOTFS/opt/kalinka/upgrade-renderer.sh" ] || die "the renderer's upgrade script is missing"
  for tool in curl python3 apt-get dpkg-deb; do
    [ -x "$ROOTFS/usr/bin/$tool" ] || die "$tool is missing, and the renderer cannot upgrade without it"
  done
  for package in kalinka-server kalinka-supervisor network-manager openssh-server; do
    ! installed_in_root "$package" || die "$package came in, though a renderer-only image has no use for it"
  done
  require_no_compiler
  verify_imager_first_run_ignored
  # Made at the first start, so each card has its own.
  [ -z "$(ls -A "$ROOTFS/var/lib/kalinka-renderer" 2>/dev/null)" ] \
    || die "the image carries a renderer id"
  [ ! -s "$ROOTFS/etc/machine-id" ] || [ "$(cat "$ROOTFS/etc/machine-id")" = uninitialized ] \
    || die "the image carries a machine-id"
  ! compgen -G "$ROOTFS/etc/ssh/ssh_host_*" > /dev/null || die "the image carries SSH host keys"
}

# Usage: verify_pi1_boot ROOT   what a Pi 1's firmware reads before the kernel runs
verify_pi1_boot() {
  local boot="$1/boot/firmware" file
  for file in bootcode.bin start.elf fixup.dat kernel.img cmdline.txt config.txt \
      bcm2708-rpi-b.dtb bcm2708-rpi-b-plus.dtb bcm2708-rpi-b-rev1.dtb; do
    [ -s "$boot/$file" ] || die "the boot partition has no $file, and a Pi 1 does not start without it"
  done
  grep -qx 'dtparam=audio=on' "$boot/config.txt" \
    || die "config.txt leaves the Pi's own audio off"
}
