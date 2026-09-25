# shellcheck shell=bash
# Taking the build's traces out and publishing the image. Defines functions only.

seal_rootfs() {
  log "Stripping this build's identity out of the image"
  in_chroot apt-get clean
  rm -rf "$ROOTFS"/var/lib/apt/lists/* "$ROOTFS"/tmp/*
  # Emptied, not removed: whatever reads these files is rarely what recreates them.
  find "$ROOTFS/var/log" -type f -delete
  # pip's cache from bootstrap.sh, of wheels already installed; systemd recreates it.
  rm -rf "$ROOTFS/var/cache/kalinka"
  restore_dns
}

# Zeroed free space compresses to nothing; whatever the host left there does not.
zero_fill_free_space() {
  dd if=/dev/zero of="$ROOTFS/zero" bs=4M status=none || true
  rm -f "$ROOTFS/zero"
  sync
  # Asked of the filesystem, since du would descend into the bind mounts.
  log "Root filesystem: $(df -h --output=used "$ROOT_DEV" | tail -1 | tr -d " ") used"
}

# A PC's target and architecture are the same word, so it is said once.
image_name() {
  local version="$1" target="$2" arch="$3" suffix="$2"
  [ "$target" = "$arch" ] || suffix="$target-$arch"
  echo "kalinka-$version-$suffix.img"
}

publish_image() {
  log "Compressing"
  local version name
  version="$(in_chroot dpkg-query -W -f='${Version}' kalinka-server)"
  name="$(image_name "$version" "$TARGET" "$TARGET_ARCH")"
  mkdir -p "$OUT_DIR"
  detach_image
  xz "$XZ_LEVEL" --threads=0 --stdout "$IMAGE" > "$OUT_DIR/$name.xz"
  ( cd "$OUT_DIR" && sha256sum "$name.xz" > "$name.xz.sha256" )

  log "Built $OUT_DIR/$name.xz"
  ls -lh "$OUT_DIR/$name.xz"
}
