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

# Usage: write_manifest PATH TARGET ARCH DISPLAY CORE SUPERVISOR RENDERER [KEY=VALUE...]
# The KEY=VALUE pairs name the base the image was built on.
write_manifest() {
  python3 - "$@" <<'PYMANIFEST'
import datetime, json, pathlib, sys
path, target, arch, display, core, supervisor, renderer, *base = sys.argv[1:]
data = dict(target=target, architecture=arch, display=display == "1", core_version=core,
            supervisor_version=supervisor, renderer_version=renderer)
data.update(pair.split("=", 1) for pair in base)
data["built_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
pathlib.Path(path).write_text(json.dumps(data, indent=2) + "\n")
PYMANIFEST
}

# Usage: compress_image IMAGE NAME   NAME.xz and its checksum land in OUT_DIR.
compress_image() {
  xz "${XZ_LEVEL:--6}" --threads=0 --stdout "$1" > "$OUT_DIR/$2.xz"
  ( cd "$OUT_DIR" && sha256sum "$2.xz" > "$2.xz.sha256" )
}

publish_image() {
  log "Compressing"
  local version name supervisor renderer
  version="$(in_chroot dpkg-query -W -f='${Version}' kalinka-server)"
  name="$(image_name "$version" "$TARGET" "$TARGET_ARCH")"
  mkdir -p "$OUT_DIR"
  supervisor="$(in_chroot dpkg-query -W -f='${Version}' kalinka-supervisor)"
  renderer="$(in_chroot dpkg-query -W -f='${Version}' kalinka-renderer)"
  write_manifest "$OUT_DIR/$name.manifest.json" "$TARGET" "$TARGET_ARCH" "${TARGET_DISPLAY:-0}" \
    "$version" "$supervisor" "$renderer" "dietpi_image=$DIETPI_IMAGE" "dietpi_version=$DIETPI_VERSION" \
    "dietpi_sha256=$(sha256sum "$DIETPI_CACHE/$DIETPI_IMAGE.img.xz" | cut -d' ' -f1)"
  detach_image
  compress_image "$IMAGE" "$name"

  log "Built $OUT_DIR/$name.xz"
  ls -lh "$OUT_DIR/$name.xz"
}
