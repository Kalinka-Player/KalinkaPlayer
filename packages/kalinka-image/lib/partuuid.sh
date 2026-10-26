# shellcheck shell=bash
# How a Pi image finds its own partitions at boot. Defines only.

# Usage: verify_partuuid IMAGE   the finished image: the kernel and fstab find root and boot by its disk id, which an SD card, a USB drive and an NVMe drive all carry.
verify_partuuid() {
  local image="$1" id cmdline fstab
  local -a starts
  id="$(sfdisk --disk-id "$image")" || die "cannot read the disk id of $image"
  id="${id#0x}"
  mapfile -t starts < <(partx -g -o START "$image")
  [ "${#starts[@]}" -eq 2 ] || die "$image does not hold just a boot and a root partition"
  cmdline="$(mtype -i "$image@@$(( starts[0] * 512 ))" ::cmdline.txt)" || die 'cannot read cmdline.txt'
  [[ " $cmdline " == *" root=PARTUUID=$id-02 "* ]] || die "cmdline.txt does not find the root as PARTUUID=$id-02"
  fstab="$(debugfs -R 'cat /etc/fstab' "$image?offset=$(( starts[1] * 512 ))" 2>/dev/null)"
  [ "$(awk '$2 == "/" { print $1 }' <<< "$fstab")" = "PARTUUID=$id-02" ] \
    || die "fstab does not mount / from PARTUUID=$id-02"
  [ "$(awk '$2 == "/boot/firmware" { print $1 }' <<< "$fstab")" = "PARTUUID=$id-01" ] \
    || die "fstab does not mount /boot/firmware from PARTUUID=$id-01"
}
