# Kalinka Player appliance images

Bootable images with the whole player already installed and enabled: the server, all four first-party plugins, the plugin SDK, the browser player and the renderer that drives the sound card. Power one on and it plays — there is no install step and no login needed to use it.

Five targets, all Debian 13. The four Raspberry Pi targets are built with Raspberry Pi's [rpi-image-gen](https://github.com/raspberrypi/rpi-image-gen) from Raspberry Pi's own packages, the PC image with debootstrap from Debian's:

| Target | Hardware | Base | Boots via |
|---|---|---|---|
| `rpi234` | Raspberry Pi 3, 4, 400, Zero 2 W, CM3 and CM4 | Debian 13, rpi-image-gen | the Pi's own firmware and the Raspberry Pi kernel |
| `rpi5` | Raspberry Pi 5, 500 and CM5 | Debian 13, rpi-image-gen | the Pi's own firmware and the Raspberry Pi kernel |
| `rpi234-display` | as `rpi234`, with the now-playing display on an attached screen | Debian 13, rpi-image-gen | as `rpi234` |
| `rpi5-display` | as `rpi5`, with the now-playing display on an attached screen | Debian 13, rpi-image-gen | as `rpi5` |
| `amd64` | x86-64 PC or virtual machine | Debian 13, debootstrap | GRUB, through UEFI (Secure Boot on or off) or a legacy BIOS |

A `-display` target sets `kalinka.display: y` in its rpi-image-gen configuration, which the hook reads as `TARGET_DISPLAY=1`, and is otherwise the same image. The installer then runs with `KALINKA_DISPLAY=1`: it installs `kalinka-kiosk` (the app drawn through flutter-pi, straight to KMS with no X or Wayland). [`lib/display.sh`](lib/display.sh) writes `base_config.display.enabled: true` into `/etc/kalinka/kalinka_conf.cfg`. That file is written before the server is installed, so the server's postinst hands it to `kalusr`; a file the server cannot read is one its next settings save replaces. The build fails unless the kiosk package is installed, its units are enabled, the driver is on and the setting is readable by the server, and a headless build fails if the kiosk package got in.

The Pi images take the Raspberry Pi kernel, from Raspberry Pi's own repository on top of Debian 13, because its drivers and overlays are what DAC HATs need; Debian's own kernel has neither.

Published images live on the [`kalinka-image-v*` releases](https://github.com/Kalinka-Player/KalinkaPlayer/releases?q=kalinka-image-v&expanded=true).

## Using an image

Flashing an image and everything else a user needs are in the [installation guide](../../docs/installation.md#install-the-server).

For first-time Wi-Fi setup from a laptop, join **`Kalinka-Setup-XXXX`** with password **`kalinka-setup`**, stay connected without internet, and open **`http://192.168.4.1`**. The [connection steps](../../docs/installation.md#connect-your-streamer-to-the-network) are in the installation guide. Bluetooth setup remains available through **Set up a streamer** in the native app; Ethernet works without either radio setup flow.

## Building an image

The PC image's build needs root, for loop devices and mounts, and `debootstrap`. A Pi build needs podman instead, and runs as you:

```sh
make image-rpi234                             # the latest published release
make image-rpi5
make image-rpi5-display                       # with the now-playing display
sudo make image-amd64 KALINKA_VERSION=4.3.2   # a specific one
```

Building for an architecture other than the host's needs `qemu-user-static` registered with `binfmt_misc`; the script says so if it is missing. CI avoids the question by building each image on a runner of its own architecture.

`IMAGE_SIZE`, `OUT_DIR` and `XZ_LEVEL` override the defaults. CI uses 8 GiB; scratch images live in `/var/tmp`, overridable with `IMAGE_WORK_DIR`. That filesystem needs enough free disk space for the uncompressed image. Each build uses a private mount namespace. Set `IMAGE_KEEP_FAILED=1` to retain a failed build’s scratch directory for diagnosis. `SUITE` and `MIRROR` pick the Debian release and mirror the PC image is built from. Images land in `out/`.

```sh
make image-test    # the test suite, in a throwaway Debian container
```

## How it is put together

[`build-image.sh`](build-image.sh) runs the steps in a fixed order and knows nothing about any one system. A target in [`targets/`](targets) names the hardware and a base; the base, `lib/base-<name>.sh`, supplies how that system is created, configured, finished and sealed. The rest is shared in [`lib/`](lib): the loop device and mounts, the chroot aids, installing and checking Kalinka, and sealing and compressing.

Kalinka itself is installed by running the repository's own [`scripts/install-release.sh`](../../scripts/install-release.sh) inside the chroot, which is what keeps the image honest: it installs exactly what `curl … | sudo bash` installs on anyone else's machine, picks up the browser player from the app repo and the renderer package for this distro and architecture, and needs no second copy of that logic here. The build then fails unless the server's venv builds, `fpcalc` fingerprints a test tone, both the server and the renderer are enabled, and no C compiler or Python development files came in. A `-display` build passes `KALINKA_DISPLAY=1`, which adds the kiosk package from the latest app release that has one, as it does the browser player.

No install in the build takes recommends. `install-release.sh` passes `--no-install-recommends` itself, and the build turns them off for its own installs, so what the image needs of them is named instead: `fpcalc` for AcoustID fingerprinting by `install-release.sh`, `wpasupplicant`, BlueZ and the supervisor for nearby setup. The x86 image also includes `libpam-systemd` for login sessions and the VM/PC power button.

The reason is graphics. `fpcalc` links ffmpeg; ffmpeg's `libavutil` hard-depends `libva2` and `libvdpau1`; each of those Recommends a video-acceleration driver, which pulls Mesa and a 118 MB `libLLVM` onto a machine with no display. Refusing packages does not keep that out: both Recommends read `<name>-all | <virtual>`, and the Mesa drivers, the Intel ones and NVIDIA's all *Provide* that virtual, so apt only moves on to the next provider. With recommends off none of them has a way in, and neither do the cellular modem stack behind NetworkManager and X forwarding behind sshd. `libva2` and `libvdpau1` themselves stay: ffmpeg needs them, they are small, and VA-API with no driver behind it is what any headless machine does anyway. The setting is lifted before the image is sealed — it was about keeping this build lean, not about what you may install later.

The `-display` images carry Mesa and `libLLVM` on purpose: the kiosk package hard-depends on Mesa's DRI and GLES drivers to draw at all, so recommends being off does not keep them out, and should not. That weight is why the headless images stay the default.

The images carry no C compiler and no Python development files, which would cost about 300 MB. Nothing on the streamer builds from source: the server's venv and the optional packages Smart Search and other features ask for install from prebuilt wheels only, and `make test` checks, when PyPI and piwheels can be reached, that every optional package has a wheel for each architecture the images run on. A package with no wheel for a streamer fails there with its reason, rather than compiling for minutes on a Pi.

Two things happen in the chroot that would not happen on a real machine. `policy-rc.d` stops packages from starting services that have no systemd to start them, and [`build-aids/systemctl`](build-aids/systemctl) stands in for `systemctl`, passing `enable` and `disable` through to this root and swallowing the rest — so each package stays the authority on what runs at boot instead of the image builder keeping a second copy of that list. Both are removed before the image is sealed.

The server's venv is built during the image build rather than on first start, so a machine that never reaches PyPI still comes up playing, and a Pi saves several minutes on its first boot.

### The rpi-image-gen base

[`rpi-image-gen/build.sh`](rpi-image-gen/build.sh) builds the four Pi targets with rpi-image-gen, at the release it pins, inside a Debian container, so any Linux host with podman and arm64 binfmt support can run it. rpi-image-gen assembles Debian trixie from Raspberry Pi's kernel, firmware and packages, with NetworkManager for wired and Wi-Fi and BlueZ for nearby setup ([`layer/kalinka-base.yaml`](rpi-image-gen/layer/kalinka-base.yaml)). [`kalinka-hook.sh`](rpi-image-gen/kalinka-hook.sh) then runs the same steps as the PC build: Kalinka, the supervisor and, for `-display`, the kiosk, each checked as there. rpi-image-gen writes `config.txt` and `cmdline.txt` from the layers' fragments only after that, so the I2C bus and the absent serial console go in as fragments, and late hooks check the display image's KMS driver, that no overlay switches off the setup radios, and that the I2C bus is on. On every boot `kalinka-growroot.service` grows the root filesystem to fill the card. The kernel and `/etc/fstab` find the root and boot partitions by PARTUUID, from the disk id, as Raspberry Pi OS does, so a streamer starts from an SD card, a USB drive or an NVMe drive alike: rpi-image-gen's own default finds them only on a card or an NVMe drive, and a streamer started from USB waits for its root until the initramfs gives up. A last hook, through `verify_partuuid` in [`lib/partuuid.sh`](lib/partuuid.sh), reads the finished image and fails the build unless its command line and fstab name both partitions that way.

The targets share [`config/common.yaml`](rpi-image-gen/config/common.yaml) and differ in the board layer. `rpi5` takes rpi-image-gen's Pi 5 layer. `rpi234` takes its Pi 4 layer, which is rpi-image-gen's generic 64-bit one: Raspberry Pi's v8 kernel, with the boot firmware and wireless firmware for every board, so one image starts every Pi from the 3 on except the 5.

These images read nothing from the card: Wi-Fi, a login and the sound card are set up after the first start. The supervisor runs its `nm` backend and applies the country setup chooses. Each streamer makes its own SSH host keys on its first start, through rpi-image-gen's `openssh-server` layer, and the build fails if the image carries any. `KALINKA_VERSION`, `SUPERVISOR_DEB`, `SUPERVISOR_VERSION`, `OUT_DIR` and `XZ_LEVEL` work as for the PC build. rpi-image-gen prints every setting it is given, so `GITHUB_TOKEN` reaches the install as a file. The image is named and its manifest written as the PC build's, with `rpi_image_gen` in place of `debian_version`. `SPIKE_SSH_PUBKEY` adds a `kalinka-dev` login with that key, for checking an image on a board, and names the image `-spike`.

From early in the boot, a screen shows the Kalinka logo. The image is [`splash/kalinka.tga`](rpi-image-gen/splash/kalinka.tga), which rpi-image-gen's `rpi-splash-screen` layer puts in the initramfs for the Raspberry Pi kernel's `fullscreen_logo`. [`splash/make-splash.sh`](rpi-image-gen/splash/make-splash.sh) draws it again from `docs/images/kalinka_logo.svg`. The kernel takes a 1920×1080, uncompressed 24-bit TGA of at most 224 colours, which `tests/test_splash.sh` checks. It crops a larger image to the screen instead of scaling it, and fills the rest of a larger screen with the image's top-left pixel. So the logo is drawn 680 px wide, narrower than the smallest official Pi screen, on the app's background colour. The kernel keeps the logo on all but the screen's bottom text line, and whatever the console prints scrolls there. Raspberry Pi OS sends that output to the serial console. Our images have none, so they add back the `quiet` that the splash layer removes. A late hook, through `verify_splash` in [`lib/splash.sh`](lib/splash.sh), fails the build if the logo, its kernel arguments or `quiet` are missing, or if an initramfs lacks the logo.

### The Debian base

[`lib/base-debootstrap.sh`](lib/base-debootstrap.sh) builds the PC image from nothing: Debian 13 from Debian's own mirror, with NetworkManager for wired and Wi-Fi and BlueZ for nearby setup. The target supplies the partition table, the kernel and firmware packages, and a `target_install_bootloader` that makes the image bootable *and proves it did*: the amd64 target fails the build if GRUB wrote the loop device it was built on into its config instead of a UUID, or if no shim stands in front of GRUB for Secure Boot. An image that builds and does not boot is the expensive failure here, so the check is in the build rather than in a later boot test. One image starts both ways: GRUB sits in the EFI partition under the removable-media path any UEFI firmware starts, and in the gap before the first partition for a legacy BIOS.

Like the Pi images, it reads nothing from the disk: Wi-Fi, a login and the time zone are set up after the first start. [`overlays/debootstrap/`](overlays/debootstrap) brings the little first-boot work a minimal Debian lacks: `kalinka-growroot.service` grows the root filesystem into the disk on every boot, and `kalinka-hostkeys.service` makes the machine's own SSH host keys once; systemd makes its machine-id. The build fails unless both units are enabled, SSH is off until the supervisor turns it on for a password someone sets, and root has no password.

All images support **Set up a streamer** through the installed Kalinka app.
Choose Bluetooth or the temporary Wi-Fi hotspot, select a nearby WPA2 network
and enter its password. Joining `Kalinka-Setup-XXXX` (password `kalinka-setup`)
opens an activation page into the standard Kalinka web app; use
`http://192.168.4.1` if it does not open automatically. The wizard always includes
a Network step and can keep an existing Ethernet or Wi-Fi connection. The streamer reports join progress; failures offer Retry, and Back
returns to network selection. It advertises setup when it has no LAN connection and
returns to normal discovery after joining. New streamers continue into the
existing first-run wizard. See
[BLE provisioning](../../docs/ble-provisioning.md) for the service, protocol,
supported networks, and the laptop test launcher
`../kalinka-supervisor/run-test.sh`. Every target installs a package-managed static Go
`kalinka-supervisor.service`, which runs its NetworkManager backend on every image. It has no Python runtime dependency.
The service runs on every streamer, radios or not, and also serves the
[control page](../../docs/supervisor-control.md) on port 8001: the streamer's state
and versions, and restart, reboot, power off and reinstall.

### Sound cards on the Pi images

ALSA is installed, so choosing a sound card never needs a network. A DAC HAT with a compliant ID EEPROM needs nothing: the Pi firmware loads its overlay at boot.

Any other HAT is chosen in the app or on the supervisor's control page, after the first start. The supervisor runs its helper, `soundcard.sh`, which ships in the supervisor package, outside its own sandbox. On these images the helper edits `config.txt` itself: it writes the HAT's overlay and turns the Pi's own audio off, or back on for the headphone socket and HDMI.

On a Pi that runs DietPi, with Kalinka installed on it by hand, `CONFIG_SOUNDCARD=` in `dietpi.txt` can also name a card before the first start, and `kalinka-soundcard.service` applies it before the renderer starts, restarting once if the boot configuration changed. There the helper hands the cards DietPi knows to DietPi's `dietpi-set_hardware`, and sets up the others, such as `i2s-dac`, by editing `config.txt` itself. `none` is not handed on, since DietPi's handling of it removes ALSA, but it does make the helper forget the last card, so naming that card again applies it again. The values that download something — `allo-piano*` firmware, the `-eq` variants — need a network the first time, so the boot unit waits for one; the app does not offer them. The Pi 5 has no 3.5 mm output.

The build switches the HAT's I2C bus on (`dtparam=i2c_arm=on`) and loads `i2c-dev` at boot, so the supervisor can read a HAT's chips and suggest boards that carry no ID chip. A device that uses GPIO 2 or 3 for something else, such as a power button on GPIO 3, then needs that line removed.

## Tests

[`tests/`](tests) covers the parts that are cheap to get wrong and expensive to discover on real hardware:

- **`test_targets.sh`** checks that every target names a base that exists and sets what that base needs, applies each Debian target's partition table, and checks that `TARGET_BOOT_PART` really is the FAT partition, that `TARGET_ROOT_PART` really is the Linux one and is last (nothing after it could grow), and that the target fills in the whole contract. Needs no privileges.
- **`test_build_lib.sh`** checks the published file names, that the image's own `resolv.conf` — file, symlink or none — comes back exactly as it was, that an overlay leaves the image's own directory modes alone, and that unit links are read as links rather than resolved on the build host.
- **`test_display_image.sh`** checks the setting that switches the display on, written and kept private, existing settings kept, nothing done for a headless target, and the check that the display image's `config.txt` loads the KMS driver.
- **`test_splash.sh`** checks that the Pi images' splash is what the kernel's `fullscreen_logo` reads: a 1920×1080 uncompressed 24-bit TGA of at most 224 colours whose top-left pixel is the app's background. It also runs the build's splash check against images that lack the logo, one of its kernel arguments or `quiet`, an initramfs, or the logo inside one.
- **`test_partuuid.sh`** builds small images laid out as genimage lays them out and runs the build's partition check on them: one that finds root and boot by its disk id passes, and one whose command line finds the root by slot or by another disk's id, or whose fstab mounts root or boot by slot, fails.
- **`test_growroot.sh`** runs the grow step against faked SD, SATA and NVMe device names, and against media the image already fills.
- **`test_systemctl_shim.sh`** pins down which verbs reach the real `systemctl` and which are swallowed.
- **`test_soundcard.sh`**, for the sound-card helper, and **`test_dietpi_conf.sh`**, for reading `dietpi.txt` as DietPi does, are in the supervisor package; `make supervisor-test` runs them.

`test_systemctl_shim.sh` edits `/etc`, so it skips itself unless `KALINKA_IMAGE_TEST_DISPOSABLE=1` says the system is throwaway. `make image-test` supplies that by running it in a container.

The test container uses the official Debian `trixie` image from the ECR mirror,
falling back to Docker Hub if the mirror cannot supply it. Cached images are
reused. `IMAGE_TEST_BASE` can select a different image, which is used without
substitution. Download failures can trigger fallback; test failures cannot.
`test_pull_base.sh` covers registry failures, cached images and explicit overrides.

[`tests/vm/`](tests/vm) boots a copy of the PC image under UEFI with Secure Boot on, and again through a legacy BIOS. The test waits for Core's HTTP endpoint, then sets the guest's time zone through the supervisor's control API, checks that `/v1/status` reports it, and powers the guest off through the same API, falling back to the power button. Offline, it checks the expanded filesystem, the first-boot units, that the kernel saw Secure Boot on and that shim is what the firmware starts, networking packages, the machine's own SSH host keys with SSH still off, the supervisor installation, the supervisor's journal (the control API listened, setup waited for a radio, and the time zone change and the power-off were accepted), and that `/etc/localtime` names the zone it set. `boot.sh` needs KVM, QEMU and OVMF; set `OVMF_CODE` and `OVMF_VARS` to your host's firmware, its Secure Boot build and a variable store with Microsoft's keys enrolled for `uefi-secure-boot`. `inspect.sh` needs root and `sbverify`.

`test_supervisor_image.sh` checks radio settings, the Pi images' `config.txt` check (setup radios on, the HAT's I2C bus on), what setting the time zone needs, service ordering, the always-on unit, the sound-card helper in the package, and its package hooks.

### Building the supervisor

Install Go 1.25 or newer on the build host (or pass `GO=/absolute/path/to/go`
to the image build). Without `SUPERVISOR_DEB=/absolute/path/to/package.deb`, the build compiles the supervisor for the target with
`CGO_ENABLED=0` before creating the image. No Go compiler or Go modules are
installed inside the image. CI installs Go and runs `make supervisor-test`.
The supervisor hands the time zone chosen in the app to systemd-timedated,
which D-Bus starts on demand, so the build fails unless the image has
timedated, its D-Bus activation file, not masked, and tzdata's `zone.tab`.
See [the supervisor documentation](../../docs/supervisor.md) for local builds,
a separate Debian package, service hardening and future recovery capabilities.

## CI and E2E artifacts

- `supervisor-packages.yml` tests Go and the installer, then cross-compiles `amd64` and `arm64` Debian packages and checksums.
- `supervisor-release.yml` publishes those packages for a `kalinka-supervisor-vX.Y.Z` tag. It does not change the app bundle's Latest badge.
- `image-build.yml` is the manual **Appliance images** workflow. It builds packages first, then the five images on native runners. Download the `image-amd64`, `image-rpi234`, `image-rpi5`, `image-rpi234-display` and `image-rpi5-display` artifacts. Each contains the compressed disk, checksum and version manifest; artifacts are retained for 14 days.
- `image-release.yml` reuses the same build before publishing an image release. `image-pr.yml` builds and boots x86 on relevant pull requests.

Core and renderer come from published releases; supervisor comes from the selected checkout. An empty supervisor version is counted from the last `kalinka-supervisor-v*` tag, as `0.2.1~dev3+g1a2b3c4` three commits past `0.2.0`: above that release and below the next one. A checkout without that tag, such as a shallow clone, needs `SUPERVISOR_VERSION` set. Nothing in the E2E workflow publishes a release.

For a provisioning test, import the x86 disk into a VM and pass through dedicated USB Wi-Fi and Bluetooth adapters. Disconnect the VM's virtual Ethernet so it is actually offline. The phone should discover the streamer, configure Wi-Fi and reach Core on the resulting address. NAT Ethernet tests Core boot, but cannot test a real Wi-Fi join or LAN mDNS. Pi radio firmware still requires a Pi test.
