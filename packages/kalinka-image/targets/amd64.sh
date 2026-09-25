# Generic x86-64 PC or virtual machine, Debian amd64.
#
# One image boots both ways: GRUB goes into the ESP under the removable-media
# path that any UEFI firmware will start without an NVRAM entry, and into the
# MBR gap for machines still booting BIOS. Both halves read the same
# /boot/grub/grub.cfg, so update-grub keeps them in step after a kernel
# upgrade and there is only ever one boot menu to reason about.

# shellcheck shell=bash disable=SC2034  # build-image.sh reads these after sourcing
TARGET_BASE=debootstrap
TARGET_ARCH=amd64
TARGET_PARTITION_LAYOUT='label: gpt
unit: sectors
start=2048, size=2048, type=21686148-6449-6E6F-744E-656564454649
start=4096, size=1048576, type=U
start=1052672, type=L'
TARGET_BOOT_PART=2
TARGET_ROOT_PART=3
TARGET_BOOT_MOUNT=/boot/efi
TARGET_BOOT_FSTAB_OPTS="umask=0077"
# Intel and Realtek cover all but the margins of x86 wireless, and the
# first-boot file offers Wi-Fi on this image as much as on the Pi one.
TARGET_PACKAGES="linux-image-amd64 grub2-common grub-efi-amd64-bin grub-pc-bin
                 firmware-iwlwifi firmware-realtek"

target_install_bootloader() {
  cat > "$ROOTFS/etc/default/grub" <<'GRUB'
GRUB_DEFAULT=0
GRUB_TIMEOUT=3
GRUB_DISTRIBUTOR="Kalinka Player"
GRUB_CMDLINE_LINUX_DEFAULT=""
GRUB_CMDLINE_LINUX=""
GRUB_DISABLE_OS_PROBER=true
GRUB_TERMINAL=console
GRUB
  in_chroot grub-install --target=x86_64-efi --efi-directory="$TARGET_BOOT_MOUNT" \
    --boot-directory=/boot --removable --no-nvram --recheck
  in_chroot grub-install --target=i386-pc --boot-directory=/boot --recheck "$LOOP"
  in_chroot update-grub

  local uuid
  uuid="$(blkid -s UUID -o value "$ROOT_DEV")"
  grep -q "$uuid" "$ROOTFS/boot/grub/grub.cfg" \
    || die "grub.cfg does not name the root filesystem by UUID — it would boot only from the loop device it was built on"
  [ -s "$ROOTFS$TARGET_BOOT_MOUNT/EFI/BOOT/BOOTX64.EFI" ] \
    || die "grub-install left no removable-media EFI binary"
}
