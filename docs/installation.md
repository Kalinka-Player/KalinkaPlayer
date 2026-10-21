# Installing Kalinka

Kalinka has three parts. They can all run on one machine or be spread across several, as long as they are on the same home network:

- **The server** keeps your music library and the play queue. You need one, running all the time.
- **An output** (the *renderer*) plays the sound. It runs on the machine your DAC, amplifier or speakers are plugged into. The server's machine gets one, and you can add more.
- **A remote** is what you control it from: the Kalinka app (Android, Windows, Linux) or any web browser.

The steps:

1. [Install the server](#install-the-server).
2. [Connect it to your network](#connect-your-box-to-the-network), if it is not on a cable.
3. [Open Kalinka](#open-kalinka) in the app or a browser, and follow the setup it shows.
4. [Put your music on it](#put-your-music-on-it).
5. [Add more outputs](#add-more-outputs), if you want music in more than one room.

## Install the server

| What you have | What to do |
|---|---|
| A Raspberry Pi 3, 4, 400, 5 or Zero 2 W | [A. Flash the Raspberry Pi image](#a-raspberry-pi-image) |
| A spare 64-bit PC, mini-PC or thin client | [B. Flash the PC image](#b-pc-image) |
| A home server that runs virtual machines | [C. Run the PC image as a virtual machine](#c-virtual-machine) |
| A computer already running Debian 13, Raspberry Pi OS (64-bit, Trixie), DietPi or Ubuntu 24.04 | [D. Install with one command](#d-install-with-one-command) |

If you are not sure, use an image. It is a complete system with Kalinka and the [supervisor](#the-supervisor) already installed, and needs no typing.

Kalinka needs a 64-bit system. Playback and the library run in 512 MB of memory; 1 GB is comfortable. Smart search needs 2 GB or more, so leave it off on a Pi 3 or Zero 2 W. See the [requirements](../README.md#-requirements).

### A. Raspberry Pi image

**You need:**

- a Raspberry Pi 3, 4, 400, 5 or Zero 2 W, or a Compute Module 3, 4 or 5, and its power supply;
- a microSD card or USB SSD of 8 GB or more. **Everything on it is erased.** Kalinka uses the whole card, so take a bigger one if you will keep music on it;
- a computer with a card reader;
- a DAC HAT or a USB DAC. The Pi's HDMI and, on a Pi 3 or 4, its headphone socket work too;
- a network cable, if you can. Wi-Fi can be set up after the first start.

#### 1. Download the image

Open the [Kalinka image releases](https://github.com/Kalinka-Player/KalinkaPlayer/releases?q=kalinka-image-v&expanded=true). In the newest one, under **Assets**, download the file that ends in:

- **`-rpi5-arm64.img.xz`** for a Raspberry Pi 5, 500 or CM5;
- **`-rpi234-arm64.img.xz`** for any other Pi.

With a screen on the Pi, take **`-rpi5-display-arm64.img.xz`** or **`-rpi234-display-arm64.img.xz`** instead. It is the same player, with a now-playing screen (cover, track, touch controls) switched on from the first start. You can turn it off in the app later.

The file is about 450 MB. Do not unpack it.

#### 2. Write it to the card

1. Install [Raspberry Pi Imager](https://www.raspberrypi.com/software/) on your computer and put the card in.
2. In Imager, choose your Pi model.
3. For the operating system, scroll to the bottom of the list, choose **Use custom**, and pick the file you downloaded.
4. Choose your card. Check its name and size: whatever you pick is erased.
5. If Imager offers to set a user name, Wi-Fi or SSH, skip it. The Kalinka image does not read those settings. It has [its own](#settings-on-the-card).
6. Write, and wait until Imager has finished verifying.

[balenaEtcher](https://etcher.balena.io/) works too: **Flash from file**, **Select target**, **Flash!**

Before you take the card out, you can [change settings on it](#settings-on-the-card): Wi-Fi, a login password, a fixed network address or a DAC HAT. None of it is needed. Wi-Fi and the DAC HAT can both be set up after the first start.

#### 3. Switch it on

Put the card in the Pi, connect your DAC and the network cable, and plug in the power. The first start takes a couple of minutes. If you set a sound card on the card, the Pi restarts once by itself.

Then [connect it to your network](#connect-your-box-to-the-network) if it has no cable, or go straight to [Open Kalinka](#open-kalinka).

### B. PC image

This turns a spare PC into a Kalinka player. You write the image to a USB stick or USB disk and start the PC from it. The PC's own disk is not touched.

**You need:** a 64-bit Intel or AMD PC that can start in UEFI mode, a USB stick or disk of 8 GB or more (**it is erased**; a small USB SSD lasts longer than a cheap stick), and a network cable if you can.

1. **Download** the file that ends in **`-amd64.img.xz`** from the [image releases](https://github.com/Kalinka-Player/KalinkaPlayer/releases?q=kalinka-image-v&expanded=true). Do not unpack it.
2. **Write it** to the USB stick with [balenaEtcher](https://etcher.balena.io/) or Raspberry Pi Imager (**Use custom**, as in [option A](#2-write-it-to-the-card)).
3. **Optional:** [change settings](#on-the-pc-image) for Wi-Fi, a login password or a fixed network address.
4. **Start the PC from the stick.** In the PC's BIOS or UEFI settings, turn off **Secure Boot**. Then switch the PC on and press the boot menu key straight away (usually F12, F11, F10, F8 or Esc) and choose the USB drive. To start from it every time, put USB first in the boot order.
5. The first start takes a few minutes and restarts the PC once. Then go on to [Open Kalinka](#open-kalinka).

<details>
<summary>Put Kalinka on the PC's internal disk instead</summary>

This erases the internal disk. Start the PC from any Linux live USB stick, download the image there, and write it to the internal disk. Replace `/dev/sdX` with that disk; `lsblk` lists the disks.

```bash
xzcat kalinka-*-amd64.img.xz | sudo dd of=/dev/sdX bs=4M conv=fsync status=progress
```

Remove the live stick and restart. Kalinka grows to fill the disk on its first start.

</details>

### C. Virtual machine

The PC image also runs as a virtual machine, which suits a home server that is on all the time anyway. Four things matter:

- **Bridged network, not NAT.** Your phone has to reach the VM directly. Use a **Bridged Adapter** in VirtualBox, a **bridge** in virt-manager, **vmbr0** in Proxmox, or an **External** switch in Hyper-V.
- **UEFI firmware, Secure Boot off.** The image does not start with legacy BIOS.
- **A bigger disk.** The image is 8 GiB. Make the virtual disk 16 GB or more before the first start, more if the music will live inside the VM.
- **2 CPU cores and 2 GB of memory**, or 4 GB for smart search.

A VM usually cannot reach your DAC. Let the VM be the server, and [add an output](#add-more-outputs) on a small box next to your amplifier. You can also pass a USB DAC through to the VM.

**1. Download and unpack** the file that ends in **`-amd64.img.xz`** from the [image releases](https://github.com/Kalinka-Player/KalinkaPlayer/releases?q=kalinka-image-v&expanded=true). On Linux or macOS run `xz -d kalinka-*-amd64.img.xz`; on Windows, use [7-Zip](https://www.7-zip.org/). You get a `.img` file.

**2. Import it.** Replace `kalinka.img` with the name of your file:

| Software | How |
|---|---|
| **virt-manager / QEMU** | `qemu-img convert -f raw -O qcow2 kalinka.img kalinka.qcow2`, then `qemu-img resize kalinka.qcow2 32G`. Create a VM with **Import existing disk image**, choose **Debian 13**, and select UEFI firmware before starting it. |
| **Proxmox VE** | Create a VM with no disk and UEFI (OVMF) firmware, then on the host run `qm disk import <vm-id> kalinka.img local-lvm`. Attach the imported disk, make it the boot disk, and resize it under **Hardware**. |
| **VirtualBox** | `VBoxManage convertfromraw kalinka.img kalinka.vdi --format VDI`, then `VBoxManage modifymedium disk kalinka.vdi --resize 32768`. Create a **Linux / Debian (64-bit)** VM with this disk, enable EFI, and set the network to **Bridged Adapter**. |
| **Hyper-V** | `qemu-img convert -f raw -O vhdx kalinka.img kalinka.vhdx`. Create a **Generation 2** VM with this disk and disable Secure Boot. |

**3. Start the VM**, give it a few minutes, and go on to [Open Kalinka](#open-kalinka).

<details>
<summary>Changing settings in a VM image</summary>

A VM rarely needs [the settings](#on-the-pc-image). The exception is a network without DHCP, where it needs [a fixed address](#a-network-without-dhcp) before anything can reach it. Edit `dietpi.txt` inside the image before you import it. It is on the image's third partition. On Linux, with `mtools` installed:

```bash
off=$(( $(partx -g -o START -n 3 kalinka.img) * 512 ))
mcopy -i kalinka.img@@$off ::/dietpi.txt .
```

Change the lines you need in that copy, as [on a Raspberry Pi](#on-a-raspberry-pi-image), then put it back:

```bash
mcopy -o -i kalinka.img@@$off dietpi.txt ::/dietpi.txt
```

</details>

### D. Install with one command

Use this on a computer that already runs one of these 64-bit systems:

- **Debian 13** (trixie)
- **Raspberry Pi OS (64-bit)** based on Debian 13 (trixie). The older Bookworm release gets the server but no sound output.
- **DietPi** on Debian 13
- **Ubuntu 24.04 LTS**

`cat /etc/os-release` tells you which one you have. Open a terminal on that computer, or log in to it over SSH, and run:

```bash
sudo apt install -y curl python3
curl -fsSL https://kalinkaplayer.com/install.sh | sudo bash
```

On a Debian system without `sudo`, log in as `root` and run the same commands without it.

This installs the server, its plugins, the browser player and an output for this computer's own sound card. Everything starts straight away and again after every restart. The first start downloads more components, so it needs the internet and takes a few minutes. Then go on to [Open Kalinka](#open-kalinka).

It does not install the [supervisor](#the-supervisor). [Add it](#add-the-supervisor-to-a-box) if you want the control page, Wi-Fi setup without a cable, or to set up a DAC HAT from the app.

If the computer runs a firewall, let your home network reach TCP port 8000 (and 8001 for the supervisor) and mDNS on UDP port 5353.

<details>
<summary>Starting from a blank Raspberry Pi</summary>

1. In [Raspberry Pi Imager](https://www.raspberrypi.com/software/), choose your Pi, then **Raspberry Pi OS (other)** → **Raspberry Pi OS Lite (64-bit)**, then your card.
2. When Imager offers settings, set a host name (for example `kalinka`), a user name and password, your Wi-Fi if you need it, and turn on **SSH**. Unlike with the Kalinka image, this is what you want here.
3. Write the card, start the Pi, and log in from your computer: `ssh <your-user>@kalinka.local`.
4. For a DAC HAT without an ID chip, set it up as its maker describes. This is usually one `dtoverlay=` line in `/boot/firmware/config.txt`, followed by a restart. With the [supervisor](#the-supervisor) installed, the app can do this instead.
5. Run the install command above.

</details>

<details>
<summary>A screen on this machine</summary>

On a Raspberry Pi with a screen, install the now-playing display with the rest:

```bash
curl -fsSL https://kalinkaplayer.com/install.sh | sudo KALINKA_DISPLAY=1 bash
```

Restart when the script says so, then switch on **Now-playing display on this machine** in the app's advanced settings.

</details>

<details>
<summary>Reading the script first</summary>

```bash
curl -fsSL https://kalinkaplayer.com/install.sh -o install.sh
less install.sh
sudo bash install.sh
```

Running it again later upgrades whatever is installed.

</details>

<details>
<summary>Fedora</summary>

Fedora 44 gets the server and the output, but not the browser player, the supervisor or automatic updates. Download `kalinka-server-…noarch.rpm` from the [latest server release](https://github.com/Kalinka-Player/KalinkaPlayer/releases/latest), then:

```bash
sudo dnf install ./kalinka-server-*.noarch.rpm
curl -fsSL https://kalinkaplayer.com/install-renderer.sh | sudo bash
```

To upgrade, install the newer `.rpm` the same way.

</details>

## Connect your box to the network

**With a network cable**, there is nothing to do: plug it into your router before switching the box on.

**For Wi-Fi**, a box with no network opens a temporary Wi-Fi network of its own, about a minute after it starts. A box that was connected before waits about five minutes after losing its network. Use any phone or laptop:

1. In its Wi-Fi settings, join **`Kalinka-Setup-XXXX`**. The four characters after the dash differ from box to box.
2. Enter the password **`kalinka-setup`**: lowercase, with a hyphen. If your device warns that there is no internet, choose to stay connected.
3. A setup page opens. If it does not, open **`http://192.168.4.1`** in the browser.
4. Choose your home Wi-Fi and enter its password.
5. The box leaves the setup network and joins yours. Reconnect your phone or laptop to your home Wi-Fi, then [open Kalinka](#open-kalinka). If the box could not join, the setup network comes back, so you can try again.

Keep these steps at hand: while your phone or laptop is on the setup network, it has no internet.

**With the Kalinka app** on a phone, you can set up the box over Bluetooth instead, and your phone stays on your home Wi-Fi. Start the app, choose to set up a box, and follow the steps.

Both ways need Wi-Fi in the box, and Bluetooth needs Bluetooth in it too. Every Pi listed above has both, except Compute Modules without wireless. On a PC, the Wi-Fi adapter must support access-point mode. The setup network uses 2.4 GHz.

You can also put the Wi-Fi name and password [on the card](#settings-on-the-card) before the first start.

## Open Kalinka

**With the Kalinka app.** Install it on a phone or computer on the same network; the [app's page](https://github.com/Kalinka-Player/KalinkaAI#-install) says which file to download for Android, Windows or Linux. The app finds the server by itself. A new server is called **My Kalinka Service**.

**With a web browser.** Open `http://kalinka.local:8000` for a box started from an image. A second image box on the same network becomes `kalinka-2.local`. Otherwise open `http://<address>:8000`, for example `http://192.168.1.50:8000`. Type the `http://` and the `:8000`. To find the address:

- the Kalinka app shows it under the server's name;
- your router's list of connected devices shows the box as **kalinka**;
- a screen plugged into a Raspberry Pi box shows it above the login prompt, unless it runs the now-playing display;
- on a computer where you ran the install command, `hostname -I` prints it.

The first time, Kalinka walks you through setup. Follow the steps it shows. On a Raspberry Pi, choosing a DAC HAT there restarts the Pi once.

## Put your music on it

Kalinka can read music from several places at once. Choose them during setup, and add more later in the app.

**On a NAS or another computer.** Nothing to copy. Add a **Network share**: the address of the NAS, the name of the share followed by any folder inside it (for example `music/Albums`), and the user name and password of an account on the NAS, or **Guest** for an open share.

- The NAS must offer SMB2 or later. A very old NAS, or a router sharing a USB disk, may offer only SMB1: turn on SMB2 or SMB3 in its settings.
- Kalinka looks for new and changed music on a share every 15 minutes.
- Recycle bins, snapshots, thumbnail folders and hidden folders are skipped.

**Copied onto the box.** Every installation has a music folder, `/srv/kalinka/music`, that Kalinka reads from the start. Copy into it with a program that speaks **SFTP**. On an image you need a login first: create one with **SSH access** on [the control page](#the-control-page), or set a password [on the card](#settings-on-the-card).

| On | Use |
|---|---|
| Windows | [WinSCP](https://winscp.net/): protocol **SFTP**, host = the box's address, your user name and password. Open `/srv/kalinka/music` and drag your albums in. |
| macOS | [Cyberduck](https://cyberduck.io/): **Open Connection** → **SFTP**, the same details. |
| Linux | In your file manager, open `sftp://<user>@<address>/srv/kalinka/music`. |
| Terminal | `scp -r ~/Music/* <user>@<address>:/srv/kalinka/music/` |

Albums appear once they have finished copying.

**In another folder or on a USB disk on the server's machine.** Add it as a **Folder on the server**. The server runs as the user `kalusr` and can only read files that every user may read. A folder in your home usually needs this once:

```bash
chmod o+X /home/<you>
chmod -R o+rX /home/<you>/Music
```

<details>
<summary>A USB disk on a box with no desktop</summary>

Kalinka does not mount USB disks by itself, so mount it once, permanently. `lsblk -f` lists the disks; note the **UUID** of yours. Then:

```bash
sudo mkdir -p /mnt/music
echo 'UUID=<your-uuid> /mnt/music auto nofail,ro 0 0' | sudo tee -a /etc/fstab
sudo mount -a
```

Add `/mnt/music` as a **Folder on the server**. `nofail` lets the box start when the disk is unplugged, and `ro` means Kalinka only ever reads it. For an NTFS disk, write `ntfs3` instead of `auto`.

</details>

The library then fills in by itself: Kalinka finds your files, tidies their details online and, if you turned it on, prepares smart search. On a Raspberry Pi with a large collection the first pass can take hours. Kalinka works normally in the meantime.

## Add more outputs

The server's own machine already has an output. You can add:

- **a Kalinka renderer on another machine**, for the best sound: bit-perfect and gapless. Any 64-bit Linux machine near an amplifier or DAC will do: a Raspberry Pi from the Zero 2 W up, an old laptop or a mini-PC;
- **a browser tab**, for quick listening with nothing to install. Open `http://<address>:8000` and pick the browser as the output. It is not gapless or bit-perfect.

**To set up a renderer on another machine:**

1. Give it 64-bit Debian 13, Raspberry Pi OS (64-bit, Trixie), Ubuntu 24.04 or Fedora 44. For a Pi, follow steps 1–4 of *Starting from a blank Raspberry Pi* under [option D](#d-install-with-one-command). Choose a host name that says where it is, such as `livingroom`: the output is named after it.
2. On that machine, run:

   ```bash
   curl -fsSL https://kalinkaplayer.com/install-renderer.sh | sudo bash
   ```

   On a Pi with a DAC HAT, install the [supervisor](#the-supervisor) with it, so the app can set the HAT up:

   ```bash
   curl -fsSL https://kalinkaplayer.com/install-renderer.sh | sudo env KALINKA_SUPERVISOR=1 bash
   ```

3. Within a few seconds the output appears in the app. Choose it, pick the sound device in its settings, and play a test sound.

Good to know:

- The renderer must be on the **same network** as the server. Guest Wi-Fi, Wi-Fi with "client isolation" and separate VLANs block it.
- Do not use a second Kalinka image as an output: an image always brings a server too, and a second server would appear in the app.
- There is no renderer for Windows or macOS. Use a browser tab there.

## The supervisor

The supervisor is a small service that looks after the box. It runs separately from Kalinka, so it keeps working when Kalinka has stopped or a plugin breaks it. It gives you:

- [the control page](#the-control-page), at `http://<address>:8001`;
- Wi-Fi setup over the temporary network or Bluetooth when the box is [not connected](#connect-your-box-to-the-network);
- from the app: restarting or switching off the box, setting its time zone, choosing a Raspberry Pi's DAC HAT, and restarting Kalinka when it stops answering.

The images include it. On a box you installed with a command, add it as below.

### Add the supervisor to a box

This works on any box that runs Debian 13, Raspberry Pi OS (64-bit), DietPi or Ubuntu 24.04, with the server, a renderer, or both. You do not need to flash anything. On that box, run:

```bash
curl -fsSL https://raw.githubusercontent.com/Kalinka-Player/KalinkaPlayer/main/scripts/install-supervisor.sh | sudo bash
```

On the server's own machine you can do it from the app instead: turn on **Manage this box** in the advanced settings and apply. Kalinka restarts while the supervisor is installed; the computer itself does not.

Once installed, it is upgraded together with Kalinka. To remove it, turn **Manage this box** off again, or run `sudo apt remove kalinka-supervisor`.

- Wi-Fi setup without a cable needs a Wi-Fi adapter in the box, and works on DietPi and on systems where NetworkManager runs the network, such as Raspberry Pi OS. Elsewhere, you still get the control page and everything else.
- On a Pi running Raspberry Pi OS, turn I²C on (`sudo raspi-config nonint do_i2c 0`) so the supervisor can recognise a DAC HAT by its chip. Without it, you pick the HAT from the full list.
- It is not available on Fedora.

### The control page

Open `http://<address>:8001` in a browser. It shows:

- whether Kalinka is running, the installed versions, and how much memory and processor time Kalinka uses;
- buttons to restart Kalinka, restart or power off the box, and reinstall Kalinka from kalinkaplayer.com. Reinstalling keeps your settings and music;
- the installed plugins, each with a **Remove** button, so a plugin that stops Kalinka from starting can be taken away;
- on a Raspberry Pi, the DAC HAT it found and the one it is set up for;
- the time zone;
- **SSH access**: enter a password of 12 characters or more to create the login `kalinka-admin`, with administrator rights. Connect with `ssh kalinka-admin@<address>` and use `sudo` for administrator commands.

The page answers anyone on your home network, over plain HTTP, and nobody outside it. Anyone who can open it can set the administrator password, so use it on a network you trust. To turn it off, add `KALINKA_CONTROL_API=0` to `/etc/default/kalinka-supervisor` and run `sudo systemctl restart kalinka-supervisor`. [The supervisor's control page and API](supervisor-control.md) describes it in full.

## Settings on the card

The images start with **no login password and no Wi-Fi**. You can set these, and a few more things, on the card before the first start. You do not need to: Wi-Fi can be [set up afterwards](#connect-your-box-to-the-network), a login created on [the control page](#the-control-page), and the DAC HAT chosen in the app.

After writing the card, unplug it and plug it back in. A drive called **KALINKA-BT** appears.

> **Windows may say the disk needs formatting. Click Cancel.** That message is about the part of the card Windows cannot read, and formatting would erase it. macOS may say a disk is not readable: click **Ignore**.

### On a Raspberry Pi image

Open **`dietpi.txt`** on the KALINKA-BT drive in a text editor (Notepad is fine) and change only the lines you need:

| To get | Change this line | For example |
|---|---|---|
| A login, as `root` or `dietpi` | `AUTO_SETUP_GLOBAL_PASSWORD=` | `AUTO_SETUP_GLOBAL_PASSWORD=choose-a-password` |
| Wi-Fi | `AUTO_SETUP_NET_WIFI_ENABLED=0` | `AUTO_SETUP_NET_WIFI_ENABLED=1` |
| Wi-Fi channels for your country | `AUTO_SETUP_NET_WIFI_COUNTRY_CODE=GB` | `AUTO_SETUP_NET_WIFI_COUNTRY_CODE=US` |
| A DAC HAT without an ID chip, HDMI or the headphone socket | `CONFIG_SOUNDCARD=none` | `CONFIG_SOUNDCARD=hifiberry-digi` (see below) |

For Wi-Fi, also open **`dietpi-wifi.txt`** and put your network's name and password between the quotes:

```sh
aWIFI_SSID[0]='Your network name'
aWIFI_KEY[0]='your Wi-Fi password'
```

Save, eject the card safely, and start the Pi. It reads the files on its first start and then **removes them from the card**, so the password does not stay there. To change them later, log in and run `sudo dietpi-config`, or write the card again.

Leave the other lines as they are.

**Sound card names.** A HAT with an ID chip needs nothing. For anything else:

| Plays through | `CONFIG_SOUNDCARD=` |
|---|---|
| HiFiBerry DAC+ / DAC+ Pro | `hifiberry-dacplus-std` / `hifiberry-dacplus-pro` |
| HiFiBerry Digi+ / Digi+ Pro | `hifiberry-digi` / `hifiberry-digi-pro` |
| IQaudio DAC+ | `iqaudio-dacplus` |
| Allo Boss DAC | `allo-boss-dac-pcm512x-audio` |
| JustBoom DAC | `justboom-dac` |
| The Pi's headphone socket (Pi 3 and 4) | `rpi-bcm2835-3.5mm` |
| The Pi's HDMI | `rpi-bcm2835-hdmi` |

The first start restarts the Pi once to switch the card on. A USB DAC needs no setting.

**Logging in.** With a password set, log in as `root` or `dietpi`, for example `ssh root@kalinka.local`. The first login starts DietPi's own setup, which updates the system and may restart it. Kalinka keeps working through it.

<details>
<summary>An SSH key instead of a password</summary>

In `dietpi.txt`, remove the `#` in front of `AUTO_SETUP_SSH_PUBKEY=` and put the whole line from your `.pub` file after the `=`.

</details>

#### A network without DHCP

Most routers give each device its address. On a network without one, such as a switch with no router, a box on a cable gets no address and nothing can reach it. Give it a fixed address before its first start. For a network that uses `192.168.1.1` to `192.168.1.254`, change these four lines in `dietpi.txt`:

```sh
AUTO_SETUP_NET_USESTATIC=1
AUTO_SETUP_NET_STATIC_IP=192.168.1.50/24
AUTO_SETUP_NET_STATIC_GATEWAY=192.168.1.1
AUTO_SETUP_NET_STATIC_DNS=192.168.1.1
```

- **The address** must be one no other device uses. Keep the `/24`, which most home networks use.
- **The gateway** is the router to the internet. Without one, do not leave the line empty: give an unused address in the same range.
- **DNS** is usually the router again. Without the internet, add network shares by their address, such as `192.168.1.20`, not by name.

Your phone or computer needs an address in the same range, for example `192.168.1.51` with the netmask `255.255.255.0`. Then open `http://192.168.1.50:8000`.

To go back to DHCP, log in, run `sudo dietpi-config`, open **Network Options: Adapters** → **Ethernet**, and switch it to **DHCP**. Or write the card again.

### On the PC image

The PC image has the same `dietpi.txt` and `dietpi-wifi.txt`, on a small drive called **DIETPISETUP** instead of KALINKA-BT. Change them as [on a Raspberry Pi](#on-a-raspberry-pi-image), including the lines for [a network without DHCP](#a-network-without-dhcp). The sound card lines are for a Pi only. The drive is removed on the first start.

If the drive does not appear, open your disk utility and mount the partition labelled `DIETPISETUP` (partition 3). Leave the other partitions alone.

## Keeping it up to date

Kalinka checks for a new release every hour, and the app offers to install it. You can also let it update by itself: turn on automatic updates in the app, and it installs them between 3 and 6 in the morning, box time, while nothing is playing. Everything on the box updates together: the server, its plugins, the browser player, the supervisor and the renderer. Renderers on other machines go first, while they are connected to the server.

A renderer that falls behind shows an upgrade button in the app. To have one update itself every night, even when the server is off, run this on its machine:

```bash
sudo systemctl enable --now kalinka-renderer-upgrade.timer
```

Kalinka does not update the operating system. On an image, log in and run `sudo dietpi-update`, then `sudo apt update && sudo apt full-upgrade`.

## Troubleshooting

**The app finds no server.** The phone and the box must be on the same network. Guest Wi-Fi, Wi-Fi with "client isolation" and a VM on NAT all hide the server. Enter the address by hand in the app, as `<address>:8000`: if that works, only discovery is blocked.

**The browser cannot connect.** Wait a few minutes after switching the box on. Type `http://`, not `https://`, and `:8000` at the end.

**The Pi does not start.** Check that you wrote the right image: `rpi5` for a Pi 5, `rpi234` for the others. Write the card again, try another card, and use the official power supply.

**The setup Wi-Fi does not appear.** The box only opens it when it has no network. If a cable is plugged in, unplug it, or open Kalinka over the cable instead. Give it a few minutes after switching on.

**Wi-Fi or the login from the card does not work.** Check that `AUTO_SETUP_NET_WIFI_ENABLED=1` is set and that the name and password in `dietpi-wifi.txt` are between the quotes. The box removes the files after its first start, so write the card again to try once more. Or use the [setup Wi-Fi](#connect-your-box-to-the-network) and [the control page](#the-control-page) instead.

**No outputs in the list, or no sound.** On the box, `systemctl status kalinka-renderer` should say `active (running)`. If the unit is not found, no output is installed there: run the renderer command from [Add more outputs](#add-more-outputs). If there is no sound, pick another sound device in the output's settings: HDMI, the headphone socket and a USB DAC are separate devices. On a Pi, HDMI, the headphone socket and a HAT without an ID chip stay silent until you choose them as the sound card, in the app or [on the card](#settings-on-the-card). Until then, you can listen in the browser.

**An output on another machine does not appear.** On that machine, run `sudo journalctl -u kalinka-renderer -f`. A line with `[Discovery] Found` means it has seen the server. Without one, the network is blocking discovery: put both on the same network, or point the renderer at the server directly:

```bash
sudo systemctl edit kalinka-renderer
```

In the editor, add these lines with your server's address, save, and run `sudo systemctl restart kalinka-renderer`:

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/kalinka-renderer --server 192.168.1.50:8000
```

**The library stays empty.** The folder is wrong, or the `kalusr` user cannot read it; see [Put your music on it](#put-your-music-on-it). For a network share, the share's status in the app says what the NAS answered: a wrong password, a share it does not have, or only SMB1.

**Kalinka does not start.** Open [the control page](#the-control-page): it shows what failed, can remove a broken plugin, and can reinstall Kalinka.

**Logs.** While the server runs, the app can download its logs as a ZIP. Passwords and keys are removed, but file names and network addresses remain, so look through it before posting it. When the server does not start, save the logs on the box:

```bash
sudo journalctl -u kalinka -u kalinka-upgrade -u kalinka-restart --since "1 day ago" > kalinka.log
```

For a renderer, use `-u kalinka-renderer -u kalinka-renderer-upgrade`.

**Asking for help.** Questions are welcome in [the testing thread](https://github.com/Kalinka-Player/KalinkaPlayer/discussions/133). Something you can reproduce is easier to act on as an [issue](https://github.com/Kalinka-Player/KalinkaPlayer/issues).
