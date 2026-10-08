# Kalinka Player appliance images

Bootable images with the whole player already installed and enabled: the server, all four first-party plugins, the plugin SDK, the browser player and the renderer that drives the sound card. Power one on and it plays — there is no install step and no login needed to use it.

Five targets, all built from signed DietPi images:

| Target | Hardware | Base | Boots via |
|---|---|---|---|
| `rpi234` | Raspberry Pi 3, 4, 400, Zero 2 W, CM3 and CM4 | DietPi | the Pi's own firmware and the Raspberry Pi kernel |
| `rpi5` | Raspberry Pi 5, 500 and CM5 | DietPi | the Pi's own firmware and the Raspberry Pi kernel |
| `rpi234-display` | as `rpi234`, with the now-playing display on an attached screen | DietPi | as `rpi234` |
| `rpi5-display` | as `rpi5`, with the now-playing display on an attached screen | DietPi | as `rpi5` |
| `amd64` | x86-64 PC or virtual machine with UEFI | DietPi Native PC UEFI | GRUB and UEFI (Secure Boot disabled) |

A `-display` target sets `TARGET_DISPLAY=1` and is otherwise the same image. [`lib/display.sh`](lib/display.sh) installs `kalinka-kiosk` (the app drawn through flutter-pi, straight to KMS with no X or Wayland), switches on the KMS driver DietPi ships commented out in `config.txt`, drops DietPi's 16 MB GPU split so the firmware default applies, and writes `base_config.display.enabled: true` into `/etc/kalinka/kalinka_conf.cfg`. That file is written before the server is installed, so the server's postinst hands it to `kalusr`; a file the server cannot read is one its next settings save replaces. The build fails unless the kiosk package is installed, its units are enabled, the driver is on and the setting is readable by the server, and a headless build fails if the kiosk package got in.

The Pi images are built on [DietPi](https://dietpi.com) because it carries the Raspberry Pi kernel, whose drivers and overlays are what DAC HATs need; Debian's own kernel has neither.

Published images live on the [`kalinka-image-v*` releases](https://github.com/Kalinka-Player/KalinkaPlayer/releases?q=kalinka-image-v&expanded=true).

## Using an image

Flashing an image, its first-boot settings and everything else a user needs are in the [installation guide](../../docs/installation.md#install-the-server).

## Building an image

Every build needs root, for loop devices and mounts:

```sh
sudo make image-rpi234                        # the latest published release
sudo make image-rpi5
sudo make image-rpi5-display                  # with the now-playing display
sudo make image-amd64 KALINKA_VERSION=4.3.2   # a specific one
```

Building for an architecture other than the host's needs `qemu-user-static` registered with `binfmt_misc`; the script says so if it is missing. CI avoids the question by building each image on a runner of its own architecture.

`IMAGE_SIZE`, `OUT_DIR` and `XZ_LEVEL` override the defaults. CI uses 8 GiB; scratch images live in `/var/tmp`, overridable with `IMAGE_WORK_DIR`. That filesystem needs enough free disk space for the uncompressed image. Each build uses a private mount namespace. Set `IMAGE_KEEP_FAILED=1` to retain a failed build’s scratch directory for diagnosis. The DietPi downloads are kept in `cache/` (`DIETPI_CACHE`) and fetched again only when dietpi.com has a newer image. dietpi.com replaces its images in place, so the build logs the sha256 of the one it used, and `DIETPI_SHA256` makes it insist on a particular one. Images land in `out/`.

```sh
make image-test    # the test suite, in a throwaway Debian container
```

## How it is put together

[`build-image.sh`](build-image.sh) runs the steps in a fixed order and knows nothing about any one system. A target in [`targets/`](targets) names the hardware and a base; the base, `lib/base-<name>.sh`, supplies how that system is created, configured, finished and sealed. The rest is shared in [`lib/`](lib): the loop device and mounts, the chroot aids, installing and checking Kalinka, and sealing and compressing.

Kalinka itself is installed by running the repository's own [`scripts/install-release.sh`](../../scripts/install-release.sh) inside the chroot, which is what keeps the image honest: it installs exactly what `curl … | sudo bash` installs on anyone else's machine, picks up the browser player from the app repo and the renderer package for this distro and architecture, and needs no second copy of that logic here. The build then fails unless the server's venv builds, `fpcalc` fingerprints a test tone, and both the server and the renderer are enabled. A `-display` build passes `KALINKA_DISPLAY=1`, which adds the kiosk package from the same app release.

No install in the build takes recommends. `install-release.sh` passes `--no-install-recommends` itself, and the build turns them off for its own installs, so what the image needs of them is named instead: `fpcalc` for AcoustID fingerprinting and the toolchain below by `install-release.sh`, `wpasupplicant`, BlueZ and the supervisor for nearby setup. The x86 image also includes `libpam-systemd` and unmasks `systemd-logind` for login sessions and the VM/PC power button.

The reason is graphics. `fpcalc` links ffmpeg; ffmpeg's `libavutil` hard-depends `libva2` and `libvdpau1`; each of those Recommends a video-acceleration driver, which pulls Mesa and a 118 MB `libLLVM` onto a machine with no display. Refusing packages does not keep that out: both Recommends read `<name>-all | <virtual>`, and the Mesa drivers, the Intel ones and NVIDIA's all *Provide* that virtual, so apt only moves on to the next provider. With recommends off none of them has a way in, and neither do the cellular modem stack behind NetworkManager and X forwarding behind sshd. `libva2` and `libvdpau1` themselves stay: ffmpeg needs them, they are small, and VA-API with no driver behind it is what any headless machine does anyway. The setting is lifted before the image is sealed — it was about keeping this build lean, not about what you may install later.

The `-display` images carry Mesa and `libLLVM` on purpose: the kiosk package hard-depends on Mesa's DRI and GLES drivers to draw at all, so recommends being off does not keep them out, and should not. That weight is why the headless images stay the default.

The C/C++ toolchain does stay, at about 300 MB: `install-release.sh` asks for it by name, because `kalinka.service` expects to be able to build an optional package from source when Smart Search wants one that has no prebuilt wheel for this architecture.

Two things happen in the chroot that would not happen on a real machine. `policy-rc.d` stops packages from starting services that have no systemd to start them, and [`build-aids/systemctl`](build-aids/systemctl) stands in for `systemctl`, passing `enable` and `disable` through to this root and swallowing the rest — so each package stays the authority on what runs at boot instead of the image builder keeping a second copy of that list. Both are removed before the image is sealed.

The server's venv is built during the image build rather than on first start, so a machine that never reaches PyPI still comes up playing, and a Pi saves several minutes on its first boot.

### The DietPi base

All images support **Set up a box** through the installed Kalinka app.
Select the box over Bluetooth, choose a nearby WPA2 network and enter its
password. The box reports join progress; failures offer Retry, and Back
returns to network selection. It advertises setup when it has no LAN connection and
returns to normal discovery after joining. New boxes continue into the
existing first-run wizard. See
[BLE provisioning](../../docs/ble-provisioning.md) for the service, protocol,
supported networks, and the laptop test launcher
`../kalinka-supervisor/run-test.sh`. All three targets install a package-managed static Go
`kalinka-supervisor.service` using DietPi’s ifupdown/wpa_supplicant backend.
The same package also supports NetworkManager on other distributions. It has no Python runtime dependency.
The service runs on every box, radios or not, and also serves the
[control page](../../docs/supervisor-control.md) on port 8001: the box's state
and versions, and restart, reboot, power off and reinstall.

[`lib/base-dietpi.sh`](lib/base-dietpi.sh) starts from DietPi's published image instead. It checks the image's signature against the key in [`keys/dietpi.asc`](keys/dietpi.asc) and insists the signing key is the one pinned in `DIETPI_SIGNER`. It then grows the image to `IMAGE_SIZE` without changing the disk id, which is how the Pi's `cmdline.txt` finds the root partition, and names the Pi FAT partition KALINKA-BT. On x86, the EFI partition stays intact and the trailing `DIETPISETUP` FAT partition moves to the end of the enlarged image. DietPi imports its settings, deletes that temporary partition and expands root on first boot.

The packages are brought up to date, but the kernel stays the one DietPi shipped and tested. APT would otherwise move it as a side effect: the toolchain brings `linux-libc-dev`, which is built from the same source as the kernel, and APT upgrades a source's packages together. The build turns that off for its own installs, and holds the kernel packages while it upgrades, since a rebuilt kernel keeps its package name; it fails if the kernel moved anyway. The hold is lifted before the image is sealed, so DietPi's own updates move the kernel later.

DietPi does its own first boot, so the build leaves identity, growth, Wi-Fi and accounts to it and only sets what Kalinka needs in `dietpi.txt`:

- no password (every account is locked until `AUTO_SETUP_GLOBAL_PASSWORD` or an SSH key is set on the card);
- the host name `kalinka`;
- no automated first run, since that would log root in on the console;
- the survey off;
- logs kept on disk, because the log export reads the journal, which DietPi otherwise keeps in RAM.

The copy on the root filesystem is dated 1970, as DietPi ships it, so the first boot always imports the one people edit on the card.

DietPi's own first run — `dietpi-update` and an APT upgrade — starts only when somebody logs in, and Kalinka does not need it. Nothing the build installs came in as a recommends, so that run's autoremove has nothing of Kalinka's to take, and the build fails if it would. Dropbear, DietPi's SSH server, has no SFTP server of its own, so the build adds OpenSSH's `sftp-server` for copying music onto the player.

To refresh the key when DietPi rotates it, fetch it by fingerprint (`https://keyserver.ubuntu.com/pks/lookup?op=get&options=mr&search=0x974105F494304547F1A9E5E00442B9ADE65643FE`), check that `gpg --show-keys` lists the fingerprint and the signing subkey, and commit it.

### Sound cards on the Pi images

ALSA is installed, and marked installed for DietPi's own tools, so choosing a sound card never needs a network. A DAC HAT with an ID EEPROM needs nothing: the Pi firmware loads its overlay at boot, and with onboard audio off in DietPi's `config.txt` it becomes the default card. For a HAT without one, `CONFIG_SOUNDCARD=` in `dietpi.txt` names it, and `kalinka-soundcard.service` hands it to DietPi's `dietpi-set_hardware` before the renderer starts, restarting once if the boot configuration changed. `none` is not handed on, since DietPi's handling of it removes ALSA, but it does make the hook forget the last card, so naming that card again applies it again. The values that download something — `allo-piano*` firmware, the `-eq` variants — need a network the first time, so the hook waits for one. The Pi 5 has no 3.5 mm output.

## Tests

[`tests/`](tests) covers the parts that are cheap to get wrong and expensive to discover on real hardware:

- **`test_targets.sh`** checks that every target names a base that exists and sets what that base needs, applies each Debian target's partition table, and checks that `TARGET_BOOT_PART` really is the FAT partition, that `TARGET_ROOT_PART` really is the Linux one and is last (nothing after it could grow), and that the target fills in the whole contract. Needs no privileges.
- **`test_build_lib.sh`** checks the published file names, that the image's own `resolv.conf` — file, symlink or none — comes back exactly as it was, that an overlay leaves the image's own directory modes alone, and that unit links are read as links rather than resolved on the build host.
- **`test_dietpi_conf.sh`** checks reading and writing `dietpi.txt` the way DietPi does, and every change the build makes to DietPi's files, on a copy shaped like the real ones.
- **`test_display_image.sh`** checks the display image's edits on a `config.txt` shaped like DietPi's: the KMS line switched on once, the GPU split gone, the setting written and kept private, existing settings kept, nothing done for a headless target, and that each `-display` target builds on the same DietPi image as its headless one.
- **`test_dietpi_image.sh`** grows a partition table without losing the disk id, refuses anything but a two-partition MBR image, accepts a signature only from the pinned key — not from a second key in the same keyring — and holds exactly the installed kernel packages, by name and version.
- **`test_growroot.sh`** runs the grow step against faked SD, SATA and NVMe device names, and against media the image already fills.
- **`test_systemctl_shim.sh`** pins down which verbs reach the real `systemctl` and which are swallowed.
- **`test_firstboot.sh`** runs the Debian image's first boot for real — a real `useradd`, a real `ssh-keygen` — and checks the account, the key, the Wi-Fi profile and its permissions, that two machines get different host keys, that a file saved on Windows or holding a setting that fails is still applied as far as it can be, and that the configuration file does not survive being read.
- **`test_soundcard.sh`** runs the sound-card hook against a stand-in for DietPi's tool: applied once, one restart only when the boot configuration changed, nothing for `none` beyond forgetting the last card, and ordered after the network.

The last three edit `/etc`, so they skip themselves unless `KALINKA_IMAGE_TEST_DISPOSABLE=1` says the system is throwaway. `make image-test` supplies that by running them in a container.

[`tests/vm/`](tests/vm) boots a copy of the PC image under UEFI with Secure Boot disabled. The test waits for Core's HTTP endpoint, then powers the guest off through the supervisor's control API, falling back to the power button. Offline, it checks the expanded filesystem, DietPi first-boot completion, networking packages, the supervisor installation, and the supervisor's journal: the control API listened, setup waited for a radio, and the power-off was accepted. `boot.sh` needs KVM, QEMU and OVMF; set `OVMF_CODE` and `OVMF_VARS` to your host's plain UEFI firmware. `inspect.sh` needs root. The old debootstrap first-boot tests remain for the retained library, which no current target uses.

`test_dietpi_uefi.sh` verifies that expanding the GPT image preserves the setup partition's bytes and every partition identifier. `test_supervisor_image.sh` checks radio settings, service ordering, the always-on unit and its package hooks.

### Building the supervisor

Install Go 1.25 or newer on the build host (or pass `GO=/absolute/path/to/go`
to the image build). Without `SUPERVISOR_DEB=/absolute/path/to/package.deb`, the build compiles the supervisor for the target with
`CGO_ENABLED=0` before creating the image. No Go compiler or Go modules are
installed inside the image. CI installs Go and runs `make supervisor-test`.
See [the supervisor documentation](../../docs/supervisor.md) for local builds,
a separate Debian package, service hardening and future recovery capabilities.

## CI and E2E artifacts

- `supervisor-packages.yml` tests Go and the installer, then cross-compiles `amd64` and `arm64` Debian packages and checksums.
- `supervisor-release.yml` publishes those packages for a `kalinka-supervisor-vX.Y.Z` tag. It does not change the app bundle's Latest badge.
- `image-build.yml` is the manual **DietPi E2E images** workflow. It builds packages first, then the five images on native runners. Download the `image-amd64`, `image-rpi234`, `image-rpi5`, `image-rpi234-display` and `image-rpi5-display` artifacts. Each contains the compressed disk, checksum and version manifest; artifacts are retained for 14 days.
- `image-release.yml` reuses the same build before publishing an image release. `image-pr.yml` builds and boots x86 on relevant pull requests.

Core and renderer come from published releases; supervisor comes from the selected checkout. An empty supervisor version is counted from the last `kalinka-supervisor-v*` tag, as `0.2.1~dev3+g1a2b3c4` three commits past `0.2.0`: above that release and below the next one. A checkout without that tag, such as a shallow clone, needs `SUPERVISOR_VERSION` set. Nothing in the E2E workflow publishes a release.

For a provisioning test, import the x86 disk into a UEFI VM, disable Secure Boot, and pass through dedicated USB Wi-Fi and Bluetooth adapters. Disconnect the VM's virtual Ethernet so it is actually offline. The phone should discover the box, configure Wi-Fi and reach Core on the resulting address. NAT Ethernet tests Core boot, but cannot test a real Wi-Fi join or LAN mDNS. Pi radio firmware still requires a Pi test.
