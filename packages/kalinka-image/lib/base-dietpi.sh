# shellcheck shell=bash disable=SC2034  # build-image.sh reads these after sourcing
# DietPi, which brings the Raspberry Pi kernel and the overlays for DAC HATs.
# Defines functions and constants only.
#
# A target on this base also sets DIETPI_IMAGE, the image's name on dietpi.com
# without .img.xz.
#
# Env: DIETPI_CACHE keeps downloads between builds (default: ./cache);
# DIETPI_SHA256 insists on one exact .img.xz, as dietpi.com replaces them in place.

DIETPI_URL=https://dietpi.com/downloads/images
DIETPI_SIGNER=974105F494304547F1A9E5E00442B9ADE65643FE
DIETPI_KEY="$SCRIPT_DIR/keys/dietpi.asc"
DIETPI_CACHE="${DIETPI_CACHE:-$SCRIPT_DIR/cache}"
DIETPI_BOOT_MOUNT=/boot/firmware
# python3 for install-release.sh, adduser for the postinsts, ALSA to pick a card offline, SFTP for Dropbear.
DIETPI_PACKAGES="python3 adduser alsa-utils openssh-sftp-server"
DIETPI_ALSA_ID=5
DIETPI_RAMLOG_ID=103

# Read back from the image, so a key DietPi renames fails the build, not ships its password.
declare -A DIETPI_SETTINGS=(
  [AUTO_SETUP_NET_HOSTNAME]=kalinka
  [AUTO_SETUP_GLOBAL_PASSWORD]=""
  [AUTO_SETUP_AUTOMATED]=0
  [SURVEY_OPTED_IN]=0
  [AUTO_SETUP_LOGGING_INDEX]=0
)

BASE_HOST_TOOLS=(curl gpg gpgv e2fsck resize2fs fatlabel)

# shellcheck source=../overlays/dietpi/usr/lib/kalinka-image/dietpi-conf.sh
. "$SCRIPT_DIR/overlays/dietpi/usr/lib/kalinka-image/dietpi-conf.sh"

base_check_host() {
  [ -r "$DIETPI_KEY" ] || die "no DietPi signing key at $DIETPI_KEY"
}

# Downloads only when dietpi.com has something newer than the cache.
fetch_dietpi_image() {
  local file="$DIETPI_CACHE/$DIETPI_IMAGE.img.xz" url="$DIETPI_URL/$DIETPI_IMAGE.img.xz" code
  local -a since=()
  mkdir -p "$DIETPI_CACHE"
  log "Fetching $DIETPI_IMAGE"
  [ -e "$file" ] && since=(-z "$file")
  code="$(curl -fL --retry 3 -R "${since[@]}" -o "$file.part" -w '%{http_code}' "$url")"
  case "$code" in
    200) mv "$file.part" "$file" ;;
    304) rm -f "$file.part" ;;
    *) rm -f "$file.part"; die "$url answered $code" ;;
  esac
  curl -fL --retry 3 -o "$file.asc" "$url.asc"

  verify_signature "$DIETPI_KEY" "$DIETPI_SIGNER" "$file" "$file.asc"
  local sum
  sum="$(sha256sum "$file" | cut -d' ' -f1)"
  [ -z "${DIETPI_SHA256:-}" ] || [ "$sum" = "$DIETPI_SHA256" ] \
    || die "$DIETPI_IMAGE.img.xz is $sum, not the DIETPI_SHA256 asked for"
  log "$DIETPI_IMAGE.img.xz sha256 $sum"
}

# gpgv trusts any key in the keyring; the pinned primary key must be the signer.
verify_signature() {
  local key="$1" signer="$2" data="$3" sig="$4" home status
  home="$(mktemp -d)"
  gpg --homedir "$home" --dearmor < "$key" > "$home/keyring.gpg" 2>/dev/null \
    || { rm -rf "$home"; die "cannot read the signing key $key"; }
  status="$(gpgv --homedir "$home" --status-fd 1 --keyring "$home/keyring.gpg" "$sig" "$data" 2>/dev/null || true)"
  rm -rf "$home"
  grep -q '^\[GNUPG:\] GOODSIG ' <<<"$status" || die "$data: bad or missing signature"
  [ "$(awk '$2 == "VALIDSIG" { print $NF }' <<<"$status")" = "$signer" ] \
    || die "$data: signed, but not by $signer"
}

# cmdline.txt finds the root by PARTUUID, which carries the disk id.
grow_root_partition() {
  local image="$1" size="$2" table id
  table="$(sfdisk -d "$image")"
  [ "$(sed -n 's/^label: //p' <<<"$table")" = dos ] \
    && [ "$(grep -c ' : start=' <<<"$table")" = 2 ] \
    || die "expected an MBR image with two partitions in $image"
  id="$(sfdisk --disk-id "$image")"
  truncate -s ">$size" "$image"
  printf ',+\n' | sfdisk --quiet --no-reread --no-tell-kernel -N 2 "$image"
  [ "$(sfdisk --disk-id "$image")" = "$id" ] || die "growing the root partition changed the disk id"
}

base_create_image() {
  fetch_dietpi_image
  log "Unpacking and growing $DIETPI_IMAGE to $IMAGE_SIZE"
  xz -dc "$DIETPI_CACHE/$DIETPI_IMAGE.img.xz" > "$IMAGE"
  grow_root_partition "$IMAGE" "$IMAGE_SIZE"
  attach_image 1 2
  e2fsck -fy "$ROOT_DEV" || [ $? -le 1 ] || die "the DietPi root filesystem does not check clean"
  resize2fs "$ROOT_DEV"
  # Nothing mounts it by label, and a named drive is easier to find on a desktop.
  fatlabel "$BOOT_DEV" "$BOOT_LABEL" >/dev/null

  mkdir -p "$ROOTFS"
  mount_at "$ROOT_DEV" "$ROOTFS"
  mount_at "$BOOT_DEV" "$ROOTFS$DIETPI_BOOT_MOUNT"
  # DietPi's /tmp is a tmpfs at run time; the directory beneath it is closed to apt's sandbox.
  mount_at -t tmpfs -o mode=1777 tmpfs "$ROOTFS/tmp"

  grep -q "root=PARTUUID=$(blkid -s PARTUUID -o value "$ROOT_DEV") " "$ROOTFS$DIETPI_BOOT_MOUNT/cmdline.txt" \
    || die "cmdline.txt does not name this root partition"
  grep -q "^UUID=$(blkid -s UUID -o value "$ROOT_DEV") / " "$ROOTFS/etc/fstab" \
    || die "fstab does not name this root filesystem"
  # shellcheck source=/dev/null
  . "$ROOTFS/boot/dietpi/.version"
  DIETPI_VERSION="$G_DIETPI_VERSION_CORE.$G_DIETPI_VERSION_SUB.$G_DIETPI_VERSION_RC"
  log "DietPi v$DIETPI_VERSION"
}

base_configure_system() { :; }

base_install_packages() {
  log "Bringing DietPi's packages up to date"
  in_chroot apt-get -y upgrade
  log "Installing what Kalinka needs beyond DietPi"
  apt_install $DIETPI_PACKAGES
}

apply_dietpi_settings() {
  local root="$1" key
  for key in "${!DIETPI_SETTINGS[@]}"; do
    dietpi_conf_set "$root$DIETPI_BOOT_MOUNT/dietpi.txt" "$key" "${DIETPI_SETTINGS[$key]}"
  done
  # First boot imports the card's copy only over an older one; a password may wait here.
  cat "$root$DIETPI_BOOT_MOUNT/dietpi.txt" > "$root/boot/dietpi.txt"
  chmod 600 "$root/boot/dietpi.txt"
  touch -d @0 "$root/boot/dietpi.txt"
}

# Kalinka's log export reads the journal, which DietPi keeps in RAM by default.
keep_logs_on_disk() {
  local root="$1"
  dietpi_conf_set "$root/boot/dietpi/.installed" "aSOFTWARE_INSTALL_STATE[$DIETPI_RAMLOG_ID]" 0
  dietpi_conf_set "$root/boot/dietpi/.installed" INDEX_LOGGING 0
  sed -i '/[[:blank:]]\/var\/log[[:blank:]]/d' "$root/etc/fstab"
  mkdir -p "$root/etc/systemd/journald.conf.d"
  printf '[Journal]\nStorage=persistent\n' > "$root/etc/systemd/journald.conf.d/kalinka-persistent.conf"
}

configure_dietpi_files() {
  local root="$1"
  apply_dietpi_settings "$root"
  keep_logs_on_disk "$root"
  # ALSA came in with the image, so DietPi's tools must not fetch it again.
  dietpi_conf_set "$root/boot/dietpi/.installed" "aSOFTWARE_INSTALL_STATE[$DIETPI_ALSA_ID]" 2
}

base_finish() {
  log "Setting DietPi up for Kalinka"
  install_overlay dietpi
  in_chroot systemctl enable kalinka-soundcard.service
  in_chroot systemctl disable dietpi-ramlog.service
  configure_dietpi_files "$ROOTFS"
  # A locked password still lets an SSH key from AUTO_SETUP_SSH_PUBKEY in.
  in_chroot usermod -p '!' root
  in_chroot usermod -p '!' dietpi
  # fpcalc and the toolchain came as recommends, which DietPi's autoremove would take.
  printf 'APT::AutoRemove::RecommendsImportant "true";\n' \
    > "$ROOTFS/etc/apt/apt.conf.d/98kalinka-image"
  printf 'Kalinka Player\n%s v%s\n' "$DIETPI_IMAGE" "$DIETPI_VERSION" > "$ROOTFS/boot/dietpi/.prep_info"
  verify_dietpi_setup
}

verify_dietpi_setup() {
  local key copy
  for copy in "$ROOTFS$DIETPI_BOOT_MOUNT/dietpi.txt" "$ROOTFS/boot/dietpi.txt"; do
    for key in "${!DIETPI_SETTINGS[@]}"; do
      [ "$(dietpi_conf_get "$copy" "$key")" = "${DIETPI_SETTINGS[$key]}" ] \
        || die "$key did not take in $copy"
    done
  done
  local user
  for user in root dietpi; do
    [ "$(in_chroot getent shadow "$user" | cut -d: -f2)" = '!' ] || die "$user can still log in"
  done
  [ "$(cat "$ROOTFS/boot/dietpi/.install_stage")" = -1 ] \
    || die "DietPi no longer thinks it has never booted"
  require_enabled local-fs.target dietpi-fs_partition_resize.service
  require_enabled multi-user.target dietpi-firstboot.service kalinka-soundcard.service
  [ -x "$ROOTFS/usr/lib/sftp-server" ] || die "no /usr/lib/sftp-server, where Dropbear looks for SFTP"
  [ ! -e "$ROOTFS/etc/systemd/system/multi-user.target.wants/dietpi-ramlog.service" ] \
    || die "dietpi-ramlog is still enabled"
  [ -z "$(packages_landed "$WORK/packages.before" <(installed_packages) 'linux-image-*')" ] \
    || die "the build moved DietPi's kernel; it ships the one DietPi tested"
  if in_chroot dpkg-query -W -f='${Status}' openssh-server 2>/dev/null | grep -q ' installed$'; then
    die "openssh-server came in; DietPi's first boot does not renew its host keys"
  fi
  local removable
  removable="$(in_chroot apt-get -s autoremove | awk '/^Remv / { print $2 }' \
    | grep -Fxv -f "$WORK/packages.before" || true)"
  [ -z "$removable" ] || die "apt autoremove would take what this build installed: $removable"
}

# DietPi's first boot renews machine-id, SSH host keys and network; these are the build's.
base_seal() {
  rm -f "$ROOTFS"/etc/*- "$ROOTFS"/var/cache/debconf/*-old "$ROOTFS"/var/lib/dpkg/*-old
  rm -f "$ROOTFS/root/.bash_history" "$ROOTFS/root/.wget-hsts" "$ROOTFS/var/lib/dbus/machine-id"
}
