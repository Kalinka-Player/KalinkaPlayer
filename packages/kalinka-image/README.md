# Kalinka Player appliance images

Bootable images with the whole player already installed and enabled: the server, all four first-party plugins, the plugin SDK, the browser player and the renderer that drives the sound card. Power one on and it plays — there is no install step and no login needed to use it.

Three targets, built by the same script on one of two bases:

| Target | Hardware | Base | Boots via |
|---|---|---|---|
| `rpi234` | Raspberry Pi 3, 4, 400, Zero 2 W, CM3 and CM4 | DietPi | the Pi's own firmware and the Raspberry Pi kernel |
| `rpi5` | Raspberry Pi 5, 500 and CM5 | DietPi | the Pi's own firmware and the Raspberry Pi kernel |
| `amd64` | any x86-64 PC or virtual machine | Debian, from debootstrap | GRUB, UEFI or BIOS, from one image |

The Pi images are built on [DietPi](https://dietpi.com) because it carries the Raspberry Pi kernel, whose drivers and overlays are what DAC HATs need; Debian's own kernel has neither.

Published images live on the [`kalinka-image-v*` releases](https://github.com/Kalinka-Player/KalinkaPlayer/releases?q=kalinka-image-v&expanded=true).

## Using an image

Flashing an image, its first-boot settings and everything else a user needs are in the [installation guide](../../docs/installation.md#install-the-server).

## Building an image

Every build needs root, for loop devices and mounts:

```sh
sudo make image-rpi234                        # the latest published release
sudo make image-rpi5
sudo make image-amd64 KALINKA_VERSION=4.3.2   # a specific one
```

Building for an architecture other than the host's needs `qemu-user-static` registered with `binfmt_misc`; the script says so if it is missing. CI avoids the question by building each image on a runner of its own architecture.

`IMAGE_SIZE`, `OUT_DIR` and `XZ_LEVEL` override the defaults for every target, `SUITE` and `MIRROR` for the Debian one. The DietPi downloads are kept in `cache/` (`DIETPI_CACHE`) and fetched again only when dietpi.com has a newer image. dietpi.com replaces its images in place, so the build logs the sha256 of the one it used, and `DIETPI_SHA256` makes it insist on a particular one. Images land in `out/`.

```sh
make image-test    # the test suite, in a throwaway Debian container
```

## How it is put together

[`build-image.sh`](build-image.sh) runs the steps in a fixed order and knows nothing about any one system. A target in [`targets/`](targets) names the hardware and a base; the base, `lib/base-<name>.sh`, supplies how that system is created, configured, finished and sealed. The rest is shared in [`lib/`](lib): the loop device and mounts, the chroot aids, installing and checking Kalinka, and sealing and compressing.

Kalinka itself is installed by running the repository's own [`scripts/install-release.sh`](../../scripts/install-release.sh) inside the chroot, which is what keeps the image honest: it installs exactly what `curl … | sudo bash` installs on anyone else's machine, picks up the browser player from the app repo and the renderer package for this distro and architecture, and needs no second copy of that logic here. The build then fails unless the server's venv builds, `fpcalc` fingerprints a test tone, and both the server and the renderer are enabled.

Recommends are on for the install, which is how `fpcalc` arrives for AcoustID fingerprinting. The cost of that is a set of packages that come in behind it and have no job here, so `EXCLUDED_PACKAGES` refuses them with an apt pin, and the build fails if this build brought any of them in. A base system may ship some of them itself — DietPi has `bash-completion` — and those are left alone.

The expensive one is graphics, and it is worth spelling out because the obvious fix does not work. `fpcalc` links ffmpeg; ffmpeg's `libavutil` hard-depends `libva2` and `libvdpau1`; each of those Recommends a video-acceleration driver, which pulls Mesa and a 118 MB `libLLVM` onto a machine with no display. Refusing the two metapackages they name accomplishes almost nothing: both Recommends read `<name>-all | <virtual>`, and the same Mesa packages *Provide* that virtual, so apt takes the second alternative and installs them anyway. Every provider has to be named — `mesa-va-drivers`, `libvdpau-va-gl1` and the Intel ones that exist only on amd64 — and what they carry is named too, so a path opening elsewhere in the dependency graph fails the build rather than quietly adding 200 MB back. DietPi enables Debian's `non-free`, where NVIDIA's Tesla driver also provides `vdpau-driver` and brings DKMS, CUDA and a kernel upgrade with it, so the list takes globs and refuses `nvidia-*` as a family.

`libva2` and `libvdpau1` themselves stay: ffmpeg needs them, they are small, and VA-API with no driver behind it is what any headless machine does anyway. Same story on a smaller scale for the cellular modem stack behind NetworkManager and X forwarding behind sshd. The pin is lifted before the image is sealed — it was about keeping this build lean, not about what you may install later.

The C/C++ toolchain does stay, at about 300 MB: `python3-pip` recommends it, and `kalinka.service` expects to be able to build an optional package from source when Smart Search wants one that has no prebuilt wheel for this architecture.

Two things happen in the chroot that would not happen on a real machine. `policy-rc.d` stops packages from starting services that have no systemd to start them, and [`build-aids/systemctl`](build-aids/systemctl) stands in for `systemctl`, passing `enable` and `disable` through to this root and swallowing the rest — so each package stays the authority on what runs at boot instead of the image builder keeping a second copy of that list. Both are removed before the image is sealed.

The server's venv is built during the image build rather than on first start, so a machine that never reaches PyPI still comes up playing, and a Pi saves several minutes on its first boot.

### The Debian base

[`lib/base-debootstrap.sh`](lib/base-debootstrap.sh) builds the system from nothing. The target supplies the partition table, the kernel and firmware packages, and a `target_install_bootloader` that makes the image bootable *and proves it did*: the amd64 target fails the build if GRUB wrote the loop device it was built on into its config instead of a UUID. An image that builds and does not boot is the expensive failure here, so the check is in the build rather than in a later boot test.

A minimal Debian has no first-boot provisioning of its own, so [`overlays/debootstrap/`](overlays/debootstrap) brings it: `growroot.sh` grows the root filesystem into the media on every boot, and `firstboot.sh` gives the machine host keys and a machine-id of its own, then applies `kalinka-firstboot.conf` from the boot partition whenever one is there. A setting that cannot be applied is logged and the rest still are, and the file is shredded either way, since FAT keeps no permissions to hide a Wi-Fi password behind.

### The DietPi base

[`lib/base-dietpi.sh`](lib/base-dietpi.sh) starts from DietPi's published image instead. It checks the image's signature against the key in [`keys/dietpi.asc`](keys/dietpi.asc) and insists the signing key is the one pinned in `DIETPI_SIGNER`. It then grows the image to `IMAGE_SIZE` without changing the disk id, which is how the Pi's `cmdline.txt` finds the root partition, and names the FAT partition KALINKA-BT.

The packages are brought up to date, but the kernel stays the one DietPi shipped and tested. APT would otherwise move it as a side effect: the toolchain brings `linux-libc-dev`, which is built from the same source as the kernel, and APT upgrades a source's packages together. The build turns that off for its own installs, and holds the kernel packages while it upgrades, since a rebuilt kernel keeps its package name; it fails if the kernel moved anyway. The hold is lifted before the image is sealed, so DietPi's own updates move the kernel later.

DietPi does its own first boot, so the build leaves identity, growth, Wi-Fi and accounts to it and only sets what Kalinka needs in `dietpi.txt`:

- no password (every account is locked until `AUTO_SETUP_GLOBAL_PASSWORD` or an SSH key is set on the card);
- the host name `kalinka`;
- no automated first run, since that would log root in on the console;
- the survey off;
- logs kept on disk, because the log export reads the journal, which DietPi otherwise keeps in RAM.

The copy on the root filesystem is dated 1970, as DietPi ships it, so the first boot always imports the one people edit on the card.

DietPi's own first run — `dietpi-update` and an APT upgrade — starts only when somebody logs in, and Kalinka does not need it. `98kalinka-image` in `apt.conf.d` stops that run's autoremove from taking `fpcalc` and the toolchain, which came in as recommends. Dropbear, DietPi's SSH server, has no SFTP server of its own, so the build adds OpenSSH's `sftp-server` for copying music onto the player.

To refresh the key when DietPi rotates it, fetch it by fingerprint (`https://keyserver.ubuntu.com/pks/lookup?op=get&options=mr&search=0x974105F494304547F1A9E5E00442B9ADE65643FE`), check that `gpg --show-keys` lists the fingerprint and the signing subkey, and commit it.

### Sound cards on the Pi images

ALSA is installed, and marked installed for DietPi's own tools, so choosing a sound card never needs a network. A DAC HAT with an ID EEPROM needs nothing: the Pi firmware loads its overlay at boot, and with onboard audio off in DietPi's `config.txt` it becomes the default card. For a HAT without one, `CONFIG_SOUNDCARD=` in `dietpi.txt` names it, and `kalinka-soundcard.service` hands it to DietPi's `dietpi-set_hardware` before the renderer starts, restarting once if the boot configuration changed. `none` is not handed on, since DietPi's handling of it removes ALSA, but it does make the hook forget the last card, so naming that card again applies it again. The values that download something — `allo-piano*` firmware, the `-eq` variants — need a network the first time, so the hook waits for one. The Pi 5 has no 3.5 mm output.

## Tests

[`tests/`](tests) covers the parts that are cheap to get wrong and expensive to discover on real hardware:

- **`test_targets.sh`** checks that every target names a base that exists and sets what that base needs, applies each Debian target's partition table, and checks that `TARGET_BOOT_PART` really is the FAT partition, that `TARGET_ROOT_PART` really is the Linux one and is last (nothing after it could grow), and that the target fills in the whole contract. Needs no privileges.
- **`test_build_lib.sh`** checks the published file names, that the image's own `resolv.conf` — file, symlink or none — comes back exactly as it was, that an overlay leaves the image's own directory modes alone, that unit links are read as links rather than resolved on the build host, and that only packages this build brought in count against it.
- **`test_dietpi_conf.sh`** checks reading and writing `dietpi.txt` the way DietPi does, and every change the build makes to DietPi's files, on a copy shaped like the real ones.
- **`test_dietpi_image.sh`** grows a partition table without losing the disk id, refuses anything but a two-partition MBR image, accepts a signature only from the pinned key — not from a second key in the same keyring — and holds exactly the installed kernel packages, by name and version.
- **`test_growroot.sh`** runs the grow step against faked SD, SATA and NVMe device names, and against media the image already fills.
- **`test_systemctl_shim.sh`** pins down which verbs reach the real `systemctl` and which are swallowed.
- **`test_firstboot.sh`** runs the Debian image's first boot for real — a real `useradd`, a real `ssh-keygen` — and checks the account, the key, the Wi-Fi profile and its permissions, that two machines get different host keys, that a file saved on Windows or holding a setting that fails is still applied as far as it can be, and that the configuration file does not survive being read.
- **`test_soundcard.sh`** runs the sound-card hook against a stand-in for DietPi's tool: applied once, one restart only when the boot configuration changed, nothing for `none` beyond forgetting the last card, and ordered after the network.

The last three edit `/etc`, so they skip themselves unless `KALINKA_IMAGE_TEST_DISPOSABLE=1` says the system is throwaway. `make image-test` supplies that by running them in a container.
