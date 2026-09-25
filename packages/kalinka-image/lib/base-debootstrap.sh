# shellcheck shell=bash disable=SC2034  # build-image.sh reads these after sourcing
# A minimal Debian built from nothing with debootstrap. Defines functions and constants only.
#
# A target on this base also sets:
#
#   TARGET_PARTITION_LAYOUT  an sfdisk script
#   TARGET_BOOT_PART         1-based index of the FAT partition
#   TARGET_ROOT_PART         1-based index of the root partition
#   TARGET_BOOT_MOUNT        where the FAT partition is mounted
#   TARGET_BOOT_FSTAB_OPTS   its fstab mount options
#   TARGET_PACKAGES          kernel, firmware and bootloader packages
#   target_install_bootloader()  make the image bootable, and prove it did
#
# Env: SUITE and MIRROR, the Debian suite and mirror (default: trixie,
# deb.debian.org).

SUITE="${SUITE:-trixie}"
MIRROR="${MIRROR:-http://deb.debian.org/debian}"
ROOT_LABEL=kalinka-root

BASE_HOST_TOOLS=(debootstrap mkfs.ext4 mkfs.vfat)

# What no Kalinka package pulls in: a way in, onto the network and into the whole card, and ALSA's tools.
BASE_PACKAGES="ca-certificates curl openssl sudo openssh-server
               network-manager iw wireless-regdb
               cloud-guest-utils dosfstools e2fsprogs
               systemd-timesyncd dbus tzdata
               python3 python3-venv python3-pip
               alsa-utils"

# An older debootstrap lacks only the suite's name: every suite script is the same file.
base_check_host() {
  [ -e "/usr/share/debootstrap/scripts/$SUITE" ] || die \
    "this debootstrap has no script for $SUITE: ln -s sid /usr/share/debootstrap/scripts/$SUITE"
}

base_create_image() {
  log "Creating a $IMAGE_SIZE image and its partitions"
  truncate -s "$IMAGE_SIZE" "$IMAGE"
  printf '%s\n' "$TARGET_PARTITION_LAYOUT" | sfdisk --quiet "$IMAGE"
  attach_image "$TARGET_BOOT_PART" "$TARGET_ROOT_PART"

  mkfs.vfat -F 32 -n "$BOOT_LABEL" "$BOOT_DEV" >/dev/null
  # Features newer than some boot paths that must read this filesystem.
  mkfs.ext4 -q -L "$ROOT_LABEL" -O '^orphan_file,^metadata_csum_seed' "$ROOT_DEV"

  mkdir -p "$ROOTFS"
  mount_at "$ROOT_DEV" "$ROOTFS"

  log "Debootstrapping Debian $SUITE ($TARGET_ARCH)"
  debootstrap --arch="$TARGET_ARCH" \
    --components=main,contrib,non-free-firmware \
    "$SUITE" "$ROOTFS" "$MIRROR"

  mkdir -p "$ROOTFS$TARGET_BOOT_MOUNT"
  mount_at "$BOOT_DEV" "$ROOTFS$TARGET_BOOT_MOUNT"
}

base_configure_system() {
  log "Configuring the base system"
  cat > "$ROOTFS/etc/apt/sources.list.d/debian.sources" <<SOURCES
Types: deb
URIs: $MIRROR
Suites: $SUITE $SUITE-updates
Components: main contrib non-free-firmware
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg

Types: deb
URIs: http://security.debian.org/debian-security
Suites: $SUITE-security
Components: main contrib non-free-firmware
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
SOURCES
  : > "$ROOTFS/etc/apt/sources.list"

  printf 'nameserver 1.1.1.1\n' > "$ROOTFS/etc/resolv.conf"
  echo kalinka > "$ROOTFS/etc/hostname"
  printf '127.0.0.1\tlocalhost\n127.0.1.1\tkalinka\n::1\tlocalhost ip6-localhost ip6-loopback\n' \
    > "$ROOTFS/etc/hosts"
  echo 'LANG=C.UTF-8' > "$ROOTFS/etc/default/locale"

  cat > "$ROOTFS/etc/fstab" <<FSTAB
LABEL=$ROOT_LABEL	/	ext4	defaults,noatime	0	1
LABEL=$BOOT_LABEL	$TARGET_BOOT_MOUNT	vfat	$TARGET_BOOT_FSTAB_OPTS	0	2
FSTAB
}

base_install_packages() {
  log "Installing the kernel, firmware and bootloader"
  apt_install $TARGET_PACKAGES

  log "Installing the base system packages"
  apt_install $BASE_PACKAGES
}

base_finish() {
  log "Installing the first-boot machinery"
  install_overlay debootstrap
  echo "KALINKA_IMAGE_BOOT=$TARGET_BOOT_MOUNT" > "$ROOTFS/etc/default/kalinka-image"
  install -m 644 "$SCRIPT_DIR/boot/kalinka-firstboot.conf.example" \
    "$ROOTFS$TARGET_BOOT_MOUNT/"
  in_chroot systemctl enable kalinka-growroot.service kalinka-firstboot.service

  target_install_bootloader
}

# Identities no two machines may share; kalinka-firstboot.service makes new ones.
base_seal() {
  rm -f "$ROOTFS"/etc/ssh/ssh_host_*
  : > "$ROOTFS/etc/machine-id"
  rm -f "$ROOTFS/var/lib/dbus/machine-id"
}
