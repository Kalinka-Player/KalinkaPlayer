# Kalinka Player appliance images

Bootable images of a minimal Debian 13 with the whole player already installed and enabled: the server, all four first-party plugins, the plugin SDK, the browser player and the renderer that drives the sound card. Power one on and it plays — there is no install step and no login needed to use it.

Two targets, built from the same script:

| Target | Hardware | Boots via |
|---|---|---|
| `rpi4` | Raspberry Pi 4, Pi 400, CM4 — SD card or USB disk | the Pi's own firmware, from a FAT partition |
| `amd64` | any x86-64 PC or virtual machine | GRUB, UEFI or BIOS, from one image |

Published images live on the [`kalinka-image-v*` releases](https://github.com/Kalinka-Player/KalinkaPlayer/releases?q=kalinka-image-v&expanded=true).

## Using an image

Flashing an image, the first-boot settings file and everything else a user needs are in the [installation guide](../../docs/installation.md#install-the-server).

Two things about a running image matter when working on it. The root filesystem grows to fill the media on every boot (`growroot.sh`), so moving to a bigger card later is a reboot, not a re-flash. And the image ships no login account, no password and no SSH host keys: `firstboot.sh` generates the host keys on the first boot, and applies `kalinka-firstboot.conf` from the boot partition whenever one is there, then shreds it. [`boot/kalinka-firstboot.conf.example`](boot/kalinka-firstboot.conf.example) documents every setting.

## Building an image

Both builds need root, for loop devices and mounts:

```sh
sudo make image-rpi4                          # the latest published release
sudo make image-amd64 KALINKA_VERSION=4.3.2   # a specific one
```

Building for an architecture other than the host's needs `qemu-user-static` registered with `binfmt_misc`; the script says so if it is missing. CI avoids the question by building each image on a runner of its own architecture.

`SUITE`, `MIRROR`, `IMAGE_SIZE`, `OUT_DIR` and `XZ_LEVEL` override the defaults. Images land in `out/`.

```sh
make image-test    # the test suite, in a throwaway Debian container
```

## How it is put together

[`build-image.sh`](build-image.sh) is the whole flow — partition, debootstrap, install, verify, compress — and knows nothing about bootloaders. Each file in [`targets/`](targets) supplies the partition table, the kernel and firmware packages, and a `target_install_bootloader` that makes the image bootable *and proves it did*: the Pi target fails the build if `cmdline.txt` does not name the root filesystem, the amd64 target fails it if GRUB wrote the loop device it was built on into its config instead of a UUID. An image that builds and does not boot is the expensive failure here, so the checks are in the build rather than in a later boot test.

Kalinka itself is installed by running the repository's own [`scripts/install-release.sh`](../../scripts/install-release.sh) inside the chroot, which is what keeps the image honest: it installs exactly what `curl … | sudo bash` installs on anyone else's machine, picks up the browser player from the app repo and the renderer package for this distro and architecture, and needs no second copy of that logic here.

Recommends are left on, which is how `fpcalc` arrives for AcoustID fingerprinting — the build fails if it did not. The cost of that is a set of packages that come in behind it and have no job here, so `EXCLUDED_PACKAGES` refuses them with an apt pin, and the build fails if any of them lands anyway.

The expensive one is graphics, and it is worth spelling out because the obvious fix does not work. `fpcalc` links ffmpeg; ffmpeg's `libavutil` hard-depends `libva2` and `libvdpau1`; each of those Recommends a video-acceleration driver, which pulls Mesa and a 118 MB `libLLVM` onto a machine with no display. Refusing the two metapackages they name accomplishes almost nothing: both Recommends read `<name>-all | <virtual>`, and the same Mesa packages *Provide* that virtual, so apt takes the second alternative and installs them anyway. Every provider has to be named — `mesa-va-drivers`, `libvdpau-va-gl1` and the Intel ones that exist only on amd64 — and what they carry is named too, so a path opening elsewhere in the dependency graph fails the build rather than quietly adding 200 MB back.

`libva2` and `libvdpau1` themselves stay: ffmpeg needs them, they are small, and VA-API with no driver behind it is what any headless machine does anyway. Same story on a smaller scale for the cellular modem stack behind NetworkManager and X forwarding behind sshd. The pin is lifted before the image is sealed — it was about keeping this build lean, not about what you may install later.

The C/C++ toolchain does stay, at about 300 MB: `python3-pip` recommends it, and `kalinka.service` expects to be able to build an optional package from source when Smart Search wants one that has no prebuilt wheel for this architecture.

Two things happen in the chroot that would not happen on a real machine. `policy-rc.d` stops packages from starting services that have no systemd to start them, and [`build-aids/systemctl`](build-aids/systemctl) stands in for `systemctl`, passing `enable` and `disable` through to this root and swallowing the rest — so each package stays the authority on what runs at boot instead of the image builder keeping a second copy of that list. Both are removed before the image is sealed, along with the SSH host keys, the machine-id and the apt lists.

The server's venv is built during the image build rather than on first start, so a machine that never reaches PyPI still comes up playing, and a Pi saves several minutes on its first boot.

## Tests

[`tests/`](tests) covers the parts that are cheap to get wrong and expensive to discover on real hardware:

- **`test_targets.sh`** applies each target's partition table and checks that `TARGET_BOOT_PART` really is the FAT partition, that `TARGET_ROOT_PART` really is the Linux one and is last (nothing after it could grow), and that every target fills in the whole contract. Needs no privileges.
- **`test_growroot.sh`** runs the grow step against faked SD, SATA and NVMe device names, and against media the image already fills.
- **`test_systemctl_shim.sh`** pins down which verbs reach the real `systemctl` and which are swallowed.
- **`test_firstboot.sh`** runs the first boot for real — a real `useradd`, a real `ssh-keygen` — and checks the account, the key, the Wi-Fi profile and its permissions, that two machines get different host keys, and that the configuration file does not survive being read.

The last two edit `/etc`, so they skip themselves unless `KALINKA_IMAGE_TEST_DISPOSABLE=1` says the system is throwaway. `make image-test` supplies that by running them in a container.
