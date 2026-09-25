# shellcheck shell=bash disable=SC2034  # the other libraries read these after sourcing
# Errors, the scratch area with its loop device and mounts, and the chroot. Defines only.

BOOT_LABEL=KALINKA-BT

die() { echo "build-image: $*" >&2; exit 1; }

log() { echo; echo "==> $*"; }

require_root() {
  [ "$(id -u)" -eq 0 ] || die "must run as root (loop devices, mounts, chroot)"
}

require_host_tools() {
  local tool
  for tool in "$@"; do
    command -v "$tool" >/dev/null || die "missing host tool: $tool"
  done
}

require_foreign_arch_support() {
  local target_arch="$1" host_arch qemu_arch
  case "$(uname -m)" in
    x86_64)  host_arch=amd64 ;;
    aarch64) host_arch=arm64 ;;
    *) die "unsupported build host architecture: $(uname -m)" ;;
  esac
  [ "$host_arch" != "$target_arch" ] || return 0
  qemu_arch=$([ "$target_arch" = arm64 ] && echo aarch64 || echo x86_64)
  [ -e "/proc/sys/fs/binfmt_misc/qemu-$qemu_arch" ] || die \
    "building $target_arch on $host_arch needs qemu-user-static registered with binfmt_misc"
}

start_work() {
  WORK="$(mktemp -d)"
  ROOTFS="$WORK/rootfs"
  IMAGE="$WORK/kalinka.img"
  LOOP=""
  MOUNTED=()
  trap cleanup EXIT
}

cleanup() {
  local status=$?
  set +e
  local i
  for (( i=${#MOUNTED[@]}-1 ; i>=0 ; i-- )); do
    umount -l "${MOUNTED[i]}" 2>/dev/null
  done
  [ -n "$LOOP" ] && losetup -d "$LOOP" 2>/dev/null
  rm -rf "$WORK"
  exit $status
}

mount_at() { mount "$@" && MOUNTED+=("${*: -1}"); }

attach_image() {
  local boot_part="$1" root_part="$2"
  LOOP="$(losetup --find --show --partscan "$IMAGE")"
  BOOT_DEV="${LOOP}p${boot_part}"
  ROOT_DEV="${LOOP}p${root_part}"
  for _ in $(seq 50); do [ -b "$ROOT_DEV" ] && break; sleep 0.1; done
  [ -b "$ROOT_DEV" ] || die "the kernel did not expose $ROOT_DEV"
}

detach_image() {
  local i
  for (( i=${#MOUNTED[@]}-1 ; i>=0 ; i-- )); do umount "${MOUNTED[i]}"; done
  MOUNTED=()
  losetup -d "$LOOP"
  LOOP=""
}

mount_chroot_filesystems() {
  mount_at --bind /dev "$ROOTFS/dev"
  mount_at --bind /dev/pts "$ROOTFS/dev/pts"
  mount_at -t proc proc "$ROOTFS/proc"
  mount_at -t sysfs sys "$ROOTFS/sys"
  mount_at -t tmpfs tmpfs "$ROOTFS/run"
}

in_chroot() {
  chroot "$ROOTFS" env -i \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    DEBIAN_FRONTEND=noninteractive LC_ALL=C.UTF-8 HOME=/root \
    ${GITHUB_TOKEN:+GITHUB_TOKEN="$GITHUB_TOKEN"} "$@"
}

apt_install() {
  in_chroot apt-get install -y "$@"
}

# The checkout is the build user's; in the image the overlay and its directories are root's.
install_overlay() {
  cp -a --no-preserve=ownership "$SCRIPT_DIR/overlays/$1/." "$ROOTFS/"
}

require_enabled() {
  local wants="$1" unit
  shift
  for unit in "$@"; do
    [ -L "$ROOTFS/etc/systemd/system/$wants.wants/$unit" ] \
      || die "$unit is not enabled in $wants"
  done
}
