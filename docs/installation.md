# Installing Kalinka

This guide gets Kalinka running on your own hardware: a Raspberry Pi, a spare PC, a virtual machine, or a Linux machine you already use. It takes five steps:

1. **[Install the server](#install-the-server)**, in one of four ways.
2. **[Get a remote](#get-a-remote)**: the Kalinka app, or any web browser.
3. **[Run the setup wizard](#run-the-setup-wizard)**, which asks where your music is and where the sound should come out.
4. **[Put your music on it](#put-your-music-on-it).**
5. **[Add more outputs](#add-more-outputs)**, if you want music in more than one room or in a browser tab.

After that, Kalinka [keeps itself up to date](#keeping-it-up-to-date). If something goes wrong, see [Troubleshooting](#troubleshooting).

## How Kalinka fits together

Kalinka has three parts, and they can all run on one machine or be spread across several:

- **The server** keeps your music library, runs search and holds the play queue. You need one, and it runs all the time.
- **An output** makes the sound. It runs on the machine that your DAC, amplifier or speakers are plugged into. Kalinka calls this program a *renderer*. The server's own machine gets one automatically, and you can add more.
- **A remote** is what you hold: the Kalinka app (Android, Windows, Linux) or any web browser. A browser tab can also be an output itself.

```mermaid
flowchart LR
    remote["📱 App or browser"] -- controls --> server["🖥️ Kalinka server"]
    server -- plays through --> out1["🔊 Output on the same machine"]
    server -- plays through --> out2["🔊 Output in another room"]
```

All of them must be on the same home network. They find each other by themselves.

## Install the server

| What you have | What to do | What it takes |
|---|---|---|
| A Raspberry Pi 3, 4, 400, 5 or Zero 2 W | [A. Flash the Raspberry Pi image](#a-raspberry-pi-image) | A memory card and 15 minutes |
| A spare PC, mini-PC or thin client (64-bit Intel or AMD) | [B. Flash the PC image](#b-pc-image) | A USB stick or USB disk |
| A home server that runs virtual machines (Proxmox, virt-manager, VirtualBox, Hyper-V) | [C. Run the PC image as a virtual machine](#c-virtual-machine) | Importing a disk image |
| A computer already running Debian 13, Raspberry Pi OS (64-bit), DietPi or Ubuntu 24.04 | [D. Install with one command](#d-install-with-one-command) | A terminal |

**Not sure?** If you have a Raspberry Pi, use the image (A): it has the fewest steps.

**Using a DAC HAT?** The Raspberry Pi images have the drivers for DAC HATs (HiFiBerry, IQaudio, Allo, JustBoom and similar boards), with nothing to install. A HAT with an ID chip on it is set up by itself. For one without, you add one line to the card before the first start; see [Settings on the card](#settings-on-the-card).

Kalinka needs a 64-bit system. Playback and the library run in 512 MB of memory, and 1 GB is comfortable. AI search needs more: 2 GB should work, and 4 GB is what has been tested, so leave it off on a Pi 3 or Zero 2 W. See the [requirements](../README.md#-requirements) for details.

### A. Raspberry Pi image

The image is a complete system with Kalinka already installed. You write it to a memory card, put the card in the Pi and switch it on.

**You need:**

- a Raspberry Pi 3, 4, 400, 5 or Zero 2 W, or a Compute Module 3, 4 or 5, and its power supply;
- a microSD card of 8 GB or more, or a USB SSD. **Everything on it will be erased.** Kalinka uses all the space on the card, so buy a larger one if you plan to keep music on it;
- a network cable, if you can: it is the simplest way to connect. Wi-Fi works too, but needs [one setting on the card](#settings-on-the-card);
- a computer to write the card with;
- something to play through: a DAC HAT or a USB DAC. The Pi's own headphone socket and HDMI work too on a Pi 3 or 4, once you [turn them on](#settings-on-the-card).

#### 1. Download the image

Open the [Kalinka image releases](https://github.com/Kalinka-Player/KalinkaPlayer/releases?q=kalinka-image-v&expanded=true). In the newest release, under **Assets**, download:

- **`-rpi5-arm64.img.xz`** for a Raspberry Pi 5, 500 or CM5;
- **`-rpi234-arm64.img.xz`** for any other Pi: 3, 4, 400, Zero 2 W, CM3 or CM4.

It is about 500 MB. Do not unpack it: the writing tools read it as it is.

#### 2. Write it to the card

Install [Raspberry Pi Imager](https://www.raspberrypi.com/software/) (Windows, macOS, Linux), put the card in your computer, and open Imager.

1. **Device:** choose your Pi, then **Next**.

   <img src="images/install/imager-device.png" width="560" alt="Raspberry Pi Imager with Raspberry Pi 4 selected">

2. **OS:** scroll to the very bottom of the list, choose **Use custom**, and pick the `.img.xz` file you downloaded.

   <img src="images/install/imager-use-custom.png" width="560" alt="The Use custom entry at the bottom of Imager's operating system list">

3. **Storage:** choose your memory card. Check the name and size carefully, because whatever you pick is erased.
4. **Customisation:** if Imager offers to set a user name, Wi-Fi or SSH, **skip it**. Those settings are for Raspberry Pi OS, and the Kalinka image does not use them. It has [its own settings on the card](#settings-on-the-card) for this.
5. **Write**, and wait until Imager has finished checking the card.

[balenaEtcher](https://etcher.balena.io/) works just as well: **Flash from file**, **Select target**, **Flash!**

#### 3. Wi-Fi, a login or a DAC HAT? Change a setting on the card

Skip this step if the Pi will use a network cable, you have no need to log in to it, and it plays through a USB DAC or a HAT with an ID chip.

Otherwise, [change the settings on the card](#settings-on-the-card) for any of these:

- the Pi on **Wi-Fi**;
- a **login** to the Pi, so you can copy music onto it over the network or use SSH;
- a **DAC HAT without an ID chip**, or the Pi's own headphone socket or HDMI.

#### 4. Switch it on

Put the card in the Pi, connect the network cable and your DAC, and plug in the power. Give it a couple of minutes to start. If you named a sound card on the card, it restarts once on its own to switch it on. Then go on to [Get a remote](#get-a-remote).

### B. PC image

This turns a spare 64-bit PC into a Kalinka player. The simplest way is to write the image to a USB stick or USB disk and start the PC from it. The PC's own disk is not touched. A small USB SSD lasts longer than a cheap stick.

**You need:** a 64-bit PC (almost any Intel or AMD PC from the last 15 years), a USB stick or disk of 8 GB or more (**it will be erased**), and a network cable if you can.

1. **Download:** from the [image releases](https://github.com/Kalinka-Player/KalinkaPlayer/releases?q=kalinka-image-v&expanded=true), download the file that ends in **`-amd64.img.xz`** (about 500 MB).
2. **Write it** to the USB stick with [balenaEtcher](https://etcher.balena.io/) (**Flash from file**, **Select target**, **Flash!**) or with Raspberry Pi Imager (**Use custom**, as in [option A](#2-write-it-to-the-card)).
3. **Optional:** add the [settings file](#on-the-pc-image) for Wi-Fi or a login.
4. **Start the PC from the stick.** Plug it in, switch the PC on, and press the boot menu key straight away. This is usually F12, F11, F10, F8 or Esc, depending on the maker. Choose the USB drive from the menu. To start from it every time, move USB to the top of the boot order in the PC's BIOS or UEFI settings. Secure Boot can stay on.
5. Go on to [Get a remote](#get-a-remote).

<details>
<summary>Advanced: put Kalinka on the PC's internal disk instead</summary>

This erases the internal disk. Start the PC from any Linux live USB stick (Debian or Ubuntu live, for example), download the image there, and write it to the internal disk. Replace `/dev/sdX` with that disk; `lsblk` lists the disks.

```bash
xzcat kalinka-*-amd64.img.xz | sudo dd of=/dev/sdX bs=4M conv=fsync status=progress
```

Remove the live stick and restart. Kalinka grows to fill the whole disk on its first start.

</details>

### C. Virtual machine

The PC image also runs as a virtual machine. This suits a home server that is on all the time anyway. Read these four points first, because they are where VMs usually go wrong:

- **Use a bridged network, not NAT.** Your phone has to reach the VM directly, and the app finds the server by listening on the local network. Choose **Bridged Adapter** in VirtualBox, a **bridge** device in virt-manager, **vmbr0** in Proxmox (the default), or an **External** switch in Hyper-V. If the VM's screen shows an address starting with `10.0.2.` (VirtualBox, QEMU) or `192.168.122.` (virt-manager), or the VM uses Hyper-V's **Default Switch**, it is on NAT.
- **Sound needs a plan.** A VM usually cannot reach your DAC. The usual setup is to let the VM be the server and [add an output](#add-more-outputs) on a small box next to your amplifier. You can also pass a USB DAC through to the VM. The VM's virtual sound card does appear as an output, but it plays through the host's sound system, so it is not bit-perfect.
- **Make the disk bigger before the first start.** The image is only 4 GB. Enlarge the virtual disk to 16 GB or more, or more again if you will keep music inside the VM. Kalinka grows into the extra space every time it starts, so you can enlarge the disk again later.
- **Give it 2 CPU cores and 2 GB of memory,** or 4 GB for AI search. UEFI and BIOS both work, and so does Secure Boot.

**1. Download and unpack** the file that ends in **`-amd64.img.xz`** from the [image releases](https://github.com/Kalinka-Player/KalinkaPlayer/releases?q=kalinka-image-v&expanded=true). On Linux or macOS run `xz -d kalinka-*-amd64.img.xz`; on Windows, [7-Zip](https://www.7-zip.org/) unpacks it. You get a `.img` file, which is a raw disk image.

**2. Import it** into your virtualisation software. Replace `kalinka.img` with the name of your file:

| Software | How |
|---|---|
| **virt-manager / QEMU** | `qemu-img convert -f raw -O qcow2 kalinka.img kalinka.qcow2` and then `qemu-img resize kalinka.qcow2 32G`. Create a VM with **Import existing disk image** and choose **Debian 13**. |
| **Proxmox VE** | Create a VM with no disk, then on the host run `qm disk import <vm-id> kalinka.img local-lvm`. Attach the imported disk, make it the boot disk, and resize it under **Hardware**. |
| **VirtualBox** | `VBoxManage convertfromraw kalinka.img kalinka.vdi --format VDI` and then `VBoxManage modifymedium disk kalinka.vdi --resize 32768`. Create a **Linux / Debian (64-bit)** VM that uses this disk, and set its network to **Bridged Adapter**. |
| **Hyper-V** | `qemu-img convert -f raw -O vhdx kalinka.img kalinka.vhdx`. Create a **Generation 2** VM that uses this disk, and set its Secure Boot template to **Microsoft UEFI Certificate Authority**. |

**3. Start the VM.** Its screen shows the address to open, as described in [Get a remote](#get-a-remote).

<details>
<summary>Adding the settings file to a VM</summary>

A VM has no memory card to take out, so add the [settings file](#on-the-pc-image) to the `.img` before you import it. On Linux, with `mtools` installed:

```bash
mcopy -i kalinka.img@@2097152 kalinka-firstboot.conf ::/kalinka-firstboot.conf
```

On macOS, install mtools with `brew install mtools` and run the same command. The boot partition is an EFI partition, which macOS does not mount by itself.

</details>

### D. Install with one command

Use this on a computer that already runs one of these 64-bit systems:

- **Debian 13** (trixie)
- **Raspberry Pi OS (64-bit)**, the current release based on Debian 13, or **DietPi** on Debian 13
- **Ubuntu 24.04 LTS**

Open a terminal on that computer, or log in to it over SSH, and run:

```bash
curl -fsSL https://kalinkaplayer.com/install.sh | sudo bash
```

It downloads the latest release and installs the server, its plugins, the browser player, and an output for this computer's own sound card. Everything starts straight away and again after every restart. It takes a few minutes. When it has finished, go on to [Get a remote](#get-a-remote).

<details>
<summary>Starting from a blank Raspberry Pi with Raspberry Pi OS</summary>

1. In [Raspberry Pi Imager](https://www.raspberrypi.com/software/), choose your Pi, then **Raspberry Pi OS (other)** → **Raspberry Pi OS Lite (64-bit)**, then your memory card.
2. At **Customisation**, set a host name (for example `kalinka`), a user name and password, your Wi-Fi if you need it, and turn on **SSH**. Unlike with the Kalinka image, this step is exactly what you want here.
3. Write the card, start the Pi, and log in from your computer: `ssh <your-user>@kalinka.local`.
4. For a DAC HAT without an ID chip, set it up now as its maker describes. This is usually one `dtoverlay=` line in `/boot/firmware/config.txt`, followed by a restart.
5. Run the install command above.

</details>

<details>
<summary>Options for the install command</summary>

You only need these in special cases.

| You want | Run |
|---|---|
| To read the script before running it | `curl -fsSL https://kalinkaplayer.com/install.sh -o install.sh`, read it, then `sudo bash install.sh` |
| A particular version of the server and its plugins (the output and the browser player still come from their latest releases) | `curl -fsSL https://kalinkaplayer.com/install.sh \| sudo bash -s -- 5.0.0` |
| A server with no output on this machine, for example a NAS whose sound nobody hears | `curl -fsSL https://kalinkaplayer.com/install.sh \| sudo KALINKA_RENDERER=0 bash` |
| No browser player | `curl -fsSL https://kalinkaplayer.com/install.sh \| sudo KALINKA_WEB=0 bash` |

Running the command again upgrades whatever is installed. Kalinka normally [upgrades itself](#keeping-it-up-to-date), though.

</details>

<details>
<summary>Installing without the script</summary>

From the [latest server release](https://github.com/Kalinka-Player/KalinkaPlayer/releases/latest), download every file that ends in `_all.deb`, plus `SHA256SUMS`. Add `kalinka-web_…_all.deb` from the [latest app release](https://github.com/Kalinka-Player/KalinkaAI/releases/latest). For sound on this machine, add the renderer `.deb` whose name matches your system and processor, for example `debian-13.arm64` or `ubuntu-24.04.amd64`, from the [renderer releases](https://github.com/Kalinka-Player/KalinkaPlayer/releases?q=kalinka-renderer-v&expanded=true). Then, in the download folder:

```bash
sha256sum -c SHA256SUMS --ignore-missing
sudo apt install ./*.deb
```

</details>

<details>
<summary>Fedora</summary>

Fedora gets the server and the output, but not the browser player, and the app's upgrade button does not work there. Download `kalinka-server-…noarch.rpm` from the [latest server release](https://github.com/Kalinka-Player/KalinkaPlayer/releases/latest), then:

```bash
sudo dnf install ./kalinka-server-*.noarch.rpm
curl -fsSL https://kalinkaplayer.com/install-renderer.sh | sudo bash
```

To upgrade later, install the newer `.rpm` the same way.

</details>

## Settings on the card

The Kalinka images come with **no login and no Wi-Fi**. A published image cannot carry a password, because everyone who downloads it would get the same one. Kalinka plays music without either. You need them only to put the player on Wi-Fi, or to log in to it, for example to copy music onto it. On a Raspberry Pi, the same place also names a DAC HAT.

To get at the settings, unplug the card after writing it and plug it back in. A drive called **KALINKA-BT** appears. On the PC image it stays hidden; see *The drive does not appear* under [On the PC image](#on-the-pc-image).

> **Windows may say the disk needs formatting. Click Cancel.** That message is about the part of the card that Windows cannot read, and formatting would erase the image. macOS may say a disk is not readable: click **Ignore**.

### On a Raspberry Pi image

The Pi images are built on [DietPi](https://dietpi.com), and use its settings files. Open **`dietpi.txt`** on the KALINKA-BT drive in a text editor (Notepad is fine), and change only the lines you need:

| To get | Change this line | For example |
|---|---|---|
| A login, as `root` or `dietpi` | `AUTO_SETUP_GLOBAL_PASSWORD=` | `AUTO_SETUP_GLOBAL_PASSWORD=choose-a-password` |
| Wi-Fi | `AUTO_SETUP_NET_WIFI_ENABLED=0` | `AUTO_SETUP_NET_WIFI_ENABLED=1` |
| Wi-Fi in your country | `AUTO_SETUP_NET_WIFI_COUNTRY_CODE=GB` | `AUTO_SETUP_NET_WIFI_COUNTRY_CODE=US` |
| Your time zone, instead of UTC | `AUTO_SETUP_TIMEZONE=UTC` | `AUTO_SETUP_TIMEZONE=Europe/London` |
| A DAC HAT without an ID chip, the headphone socket or HDMI | `CONFIG_SOUNDCARD=none` | `CONFIG_SOUNDCARD=hifiberry-digi` (see below) |

For Wi-Fi, also open **`dietpi-wifi.txt`** and put your network's name and password between the quotes on these two lines:

```sh
aWIFI_SSID[0]='Your network name'
aWIFI_KEY[0]='your Wi-Fi password'
```

Save both files, eject the card safely, and start the Pi. It reads the files on its first start and then **removes them from the card**, because a password should not stay there. To change them later, log in and run `dietpi-config`, or write the card again.

**Sound card names.** A HAT with an ID chip needs nothing. For anything else, `CONFIG_SOUNDCARD` takes one of these names, and DietPi's `dietpi-config` lists the rest under **Audio Options**:

| Plays through | `CONFIG_SOUNDCARD=` |
|---|---|
| HiFiBerry DAC+ / DAC+ Pro | `hifiberry-dacplus-std` / `hifiberry-dacplus-pro` |
| HiFiBerry Digi+ / Digi+ Pro | `hifiberry-digi` / `hifiberry-digi-pro` |
| IQaudio DAC+ | `iqaudio-dacplus` |
| Allo Boss DAC | `allo-boss-dac-pcm512x-audio` |
| JustBoom DAC | `justboom-dac` |
| The Pi's headphone socket (Pi 3 and 4) | `rpi-bcm2835-3.5mm` |
| The Pi's HDMI (Pi 3 and 4) | `rpi-bcm2835-hdmi` |

The first start restarts the Pi once to switch the sound card on. A USB DAC needs no setting: pick it in the output's settings in the app.

**Logging in.** With a password set, log in over SSH as `root` or `dietpi`, for example `ssh root@<address>`. The first login starts DietPi's own setup, which updates the system and may restart it. Kalinka does not need it and keeps working through it.

<details>
<summary>An SSH key instead of a password</summary>

In `dietpi.txt`, remove the `#` in front of `AUTO_SETUP_SSH_PUBKEY=` and put the whole line from your `.pub` file after the `=`. Every account stays without a password, and only your key gets in.

</details>

### On the PC image

Put a small text file called `kalinka-firstboot.conf` on the KALINKA-BT drive before the first start:

1. The drive holds a file called `kalinka-firstboot.conf.example`. **Copy it** and name the copy exactly `kalinka-firstboot.conf`. Windows hides file extensions by default, so check the name does not end in `.txt`.
2. Open the copy in a text editor. Every line in it is explained. A line that starts with `#` is ignored, so remove the `#` from each line you fill in and leave the rest alone. Keep `PASSWORD_HASH` commented out unless you put a real hash in it, because a set `PASSWORD_HASH` wins over `PASSWORD`. For example:

   ```sh
   USERNAME=kalinka
   PASSWORD='choose-a-password'
   WIFI_SSID='Your network name'
   WIFI_PASSWORD='your Wi-Fi password'
   WIFI_COUNTRY=GB
   ```

   `WIFI_COUNTRY` is your two-letter country code: GB, US, DE, FR and so on. Leave out whatever you do not need. With a network cable, the three Wi-Fi lines can go. `TIMEZONE`, for example `TIMEZONE=Europe/London`, sets the player's clock; without it the player runs on UTC.

3. Save the file, eject the drive safely, and start the PC.

The player reads the file on its first start, applies it, and then **deletes it**, because a Wi-Fi password should not stay on the drive. If the file is still there after the first start, the player could not read it at all.

The same file also works later. Put it back on a player that is already in use and restart it.

<details>
<summary>The drive does not appear</summary>

On the PC image, **KALINKA-BT** is an EFI boot partition. Windows, macOS and Linux desktops all keep those hidden, so mount it by hand.

**Linux:** `lsblk -o NAME,LABEL` shows which partition is labelled KALINKA-BT, for example `sdb2`. Then:

```bash
sudo mount /dev/sdb2 /mnt
sudo cp /mnt/kalinka-firstboot.conf.example /mnt/kalinka-firstboot.conf
sudo nano /mnt/kalinka-firstboot.conf
sudo umount /mnt
```

**macOS:** `diskutil list` shows the partition labelled KALINKA-BT, for example `disk4s2`. `sudo diskutil mount disk4s2` mounts it at `/Volumes/KALINKA-BT`; copy and edit the file there with `sudo cp` and `sudo nano`, then `diskutil unmount disk4s2`.

**Windows:** open a Command Prompt **as administrator** and run `diskpart`, then:

```text
list volume
select volume <number of the ~512 MB FAT32 volume labelled KALINKA-BT>
assign letter=K
exit
```

Open **Notepad as administrator**, open `K:\kalinka-firstboot.conf.example`, and use **Save as** to save it as `K:\kalinka-firstboot.conf`, with **Save as type** set to **All files**. File Explorer cannot open this drive, but an administrator Notepad can.

</details>

<details>
<summary>More secure alternatives to a plain-text password</summary>

Instead of `PASSWORD`, use `PASSWORD_HASH` with a hash made by `openssl passwd -6`. The password itself then never touches the drive. To log in with an SSH key, set `SSH_AUTHORIZED_KEY` to the whole line from your `.pub` file. The example file explains both options.

</details>

## Get a remote

Give the player a couple of minutes after you switch it on. Then connect to it in one of two ways.

**With the Kalinka app (easiest).** Install it on a phone or computer on the same network; the [app's page](https://github.com/Kalinka-Player/KalinkaAI#-install) says which file to download for Android, Windows or Linux. The app searches the network and lists every Kalinka server it finds, with its address. A new server is called **My Kalinka Service** until you rename it. Tap it, then **Connect**.

<img src="https://github.com/Kalinka-Player/KalinkaAI/raw/main/docs/images/setup/1-find-server.png" width="300" alt="The app listing the Kalinka servers it found, each with its address">

**With a web browser.** Open `http://<address>:8000`, for example `http://192.168.1.50:8000`. Type the `http://` and the `:8000` in full. To find the address:

- the Kalinka app shows it under the server's name;
- your router's web page lists connected devices, and the player appears as **kalinka**;
- a screen plugged into the player shows it:

  <img src="images/install/console-address.png" width="560" alt="The player's screen: Kalinka Player is running: open http://10.0.2.15:8000 in a browser">

  This picture comes from a test VM on NAT. On a normal home network the address usually starts with `192.168.`

- on a machine where you ran the one-command install, `hostname -I` prints it.

## Run the setup wizard

Whichever remote you open, the **setup wizard** starts. It asks where your music is, which output to play through, and whether an amplifier handles the volume, and then it plays a test tone. The browser skips the first step, because it already knows which server it belongs to. The [first-run setup guide](https://github.com/Kalinka-Player/KalinkaAI/blob/main/docs/first-run-setup.md) explains every step.

## Put your music on it

Pick whichever of these matches where your music is. You can combine them: Kalinka treats each one as a **music source** of **My Library**.

**On a NAS or another computer.** No copying is needed. In the wizard, or later under **Server settings → Input modules → My Library → Music sources**, add a **Network share**. Enter the address of the NAS or computer, the shared folder, and the user name and password for the share. Guest access to a share with no password does not work yet. The server reads the share itself, so there is nothing to set up on the player.

**Copied onto the player.** Every Kalinka installation has a music folder, `/srv/kalinka/music`, that anyone may write to, and Kalinka reads it from the start. On the images, copying music there needs a login, which you [set on the card](#settings-on-the-card). Use a file-transfer program that speaks **SFTP**:

| On | Use |
|---|---|
| Windows | [WinSCP](https://winscp.net/): protocol **SFTP**, host = the player's address, your user name and password. Open `/srv/kalinka/music` and drag your albums in. |
| macOS | [Cyberduck](https://cyberduck.io/): **Open Connection** → **SFTP**, the same details. |
| Linux | In your file manager, open `sftp://<user>@<address>/srv/kalinka/music` (in GNOME Files: **Other Locations**). |
| Terminal | `scp -r ~/Music/* <user>@<address>:/srv/kalinka/music/` |

Albums appear once they have finished copying. Kalinka ignores files that are still changing.

**In another folder on the server's machine.** This includes a USB disk. Add it as a **Folder on the server**. The server runs as a user called `kalusr`, and it can only see files that every user may read. Most home folders need their access opened up once:

```bash
chmod o+X /home/<you>
chmod -R o+rX /home/<you>/Music
```

<details>
<summary>A USB disk on a player with no desktop</summary>

Kalinka does not mount USB disks by itself, so mount it once and permanently. `lsblk -f` lists the disks; note the **UUID** of yours. Then:

```bash
sudo mkdir -p /mnt/music
echo 'UUID=<your-uuid> /mnt/music auto nofail,ro 0 0' | sudo tee -a /etc/fstab
sudo mount -a
```

Add `/mnt/music` as a **Folder on the server**. `nofail` lets the player start normally when the disk is unplugged, and `ro` means Kalinka only ever reads the disk. For a disk formatted in NTFS, write `ntfs3` in place of `auto`.

A disk that a desktop opens by itself, under `/media/<you>/`, is usually private to your user, so `kalusr` cannot read it. Mount it as shown here instead.

</details>

After that, the library fills in by itself. First Kalinka finds your files, then it tidies their details online, and then, if you turned it on, it builds AI search. On a Raspberry Pi with a large collection, the first pass can take hours. Kalinka works normally in the meantime.

To improve how Kalinka repairs your tags, add a free [AcoustID](https://acoustid.org/) key in the server settings. Kalinka then recognises tracks by their sound as well as by their names.

## Add more outputs

An output is anywhere Kalinka can play. The server's own machine already has one. You can add two other kinds:

| Output | Good for | How |
|---|---|---|
| **Kalinka renderer** on another machine | The best sound: bit-perfect and gapless | Install it with one command (below) |
| **A browser tab** | Quick listening on a laptop, with nothing to install | Open `http://<address>:8000` and pick the browser from the output list. It is named after the browser, for example *Chrome on Windows*. There is no gapless playback and no bit-perfect guarantee. |

Any Linux computer near an amplifier or DAC can run a renderer: a Raspberry Pi of any model from the Zero 2 W up, an old laptop, or a mini-PC. It finds the server by itself and appears in the app's list of outputs.

**To set up a renderer on another machine:**

1. Give the machine a 64-bit Debian 13, Raspberry Pi OS (64-bit), Ubuntu 24.04 or Fedora system. For a Pi, follow steps 1–4 of *Starting from a blank Raspberry Pi* under [option D](#d-install-with-one-command), but not step 5, which would install a whole server. Choose a **host name** that says where it is, such as `livingroom`: the output is named after it (*Kalinka Renderer on livingroom*).
2. On that machine, run:

   ```bash
   curl -fsSL https://kalinkaplayer.com/install-renderer.sh | sudo bash
   ```

3. Within a few seconds it appears in the app, both under the cast icon in the player and in the wizard's **Audio output** step. Choose it, open its **gear** to pick the sound device (your DAC, HDMI and so on), and play the **test tone**.

<img src="https://github.com/Kalinka-Player/KalinkaAI/raw/main/docs/images/setup/4-audio-output.png" width="300" alt="Two outputs in the app: Kalinka Renderer on envel-lenovo and Kalinka Renderer on raspberrypi">

A few things to know:

- The renderer must be on the **same network** as the server. Guest Wi-Fi, Wi-Fi with "client isolation" turned on, and separate VLANs all block it; see [Troubleshooting](#troubleshooting). Once it has found the server, it connects out to it, so nothing needs opening in the other direction.
- For an extra output, use plain Raspberry Pi OS Lite or DietPi with the renderer command, **not** a second Kalinka image. The image always includes a server too, and a second server would then appear in the app.
- There is no renderer for Windows or macOS yet. On those computers, use a browser tab as the output.
- Other Linux systems can run the renderer as a flatpak, which has to be [built from source](../packages/kalinka-renderer/flatpak/README.md) for now. A renderer installed that way, or built from source, cannot replace itself, so it is never offered an upgrade.

## Keeping it up to date

Kalinka checks for a new release every hour. When there is one, the app offers an **upgrade** button. To have it upgrade by itself, turn on **Auto upgrade** in the server settings: it then installs between 3 and 6 in the morning, while nothing is playing. That is the player's own clock, which on the images is UTC unless [the settings on the card](#settings-on-the-card) name a time zone.

The server, its plugins, the browser player and every renderer move together, and renderers go first, so that a renderer is never left too old to play for its server. A renderer that has fallen too far behind shows an upgrade button in the app's output list.

A renderer that nobody looks after can also update itself every night, without the server:

```bash
sudo systemctl enable --now kalinka-renderer-upgrade.timer
```

On the images, the operating system under Kalinka is an ordinary Debian, or DietPi on a Raspberry Pi. If you have a login, update it now and then: `sudo apt update && sudo apt full-upgrade` on the PC image, `dietpi-update` and then the same `apt` command on a Pi.

## Troubleshooting

**The app finds no server.** The phone and the player must be on the same network. Guest Wi-Fi, Wi-Fi with "client isolation", and a VM on NAT all hide the server. Use **Enter Address Manually** with `<address>:8000` to check whether the server itself is fine: if that works, only discovery is blocked.

**The browser cannot connect.** Wait two minutes after switching the player on. Type `http://`, not `https://`, and add `:8000` at the end.

**The Pi does not start.** Check you wrote the right image: `rpi5` for a Pi 5, `rpi234` for the others. Write the card again, and try another card if that fails. Use the official power supply.

**Wi-Fi or the login does not work.** On a Pi, check that `AUTO_SETUP_NET_WIFI_ENABLED=1` is set and that the network name and password in `dietpi-wifi.txt` are between the quotes. The Pi removes the files from the card after its first start, so write the card again to try once more. On the PC image, look at the drive again: if `kalinka-firstboot.conf` is still there, the player could not read it. Check that the file name is exactly right, with no hidden `.txt` on the end, and that any value containing spaces is inside quotes. Then start the player again. If the file is gone but the login still fails, a line was left starting with `#`, or `PASSWORD_HASH` held the example's placeholder: write the file again with those fixed.

**No outputs in the list, or no sound.** The output runs as a service of its own. On the player, `systemctl status kalinka-renderer` should say `active (running)`. If it says the unit could not be found, no output is installed on that machine: run the renderer command from [Add more outputs](#add-more-outputs) there. If there is no sound, open the output's gear and try another sound device: HDMI, the headphone socket and a USB DAC are separate devices. On a Pi, the headphone socket and HDMI stay off, and a HAT without an ID chip stays silent, until `CONFIG_SOUNDCARD` names them ([Settings on the card](#settings-on-the-card)). Once the Pi is running, change that line in `/boot/dietpi.txt` and restart it. Until an output works, you can listen in the browser.

**An output on another machine does not appear.** On that machine, run `sudo journalctl -u kalinka-renderer -f`. A line containing `[Discovery] Found` means it has seen the server. No such line means the network is blocking discovery, and a line about `renderer_proto` means that server does not accept renderers at all. Put both machines on the same network, or point the renderer at the server's address directly:

```bash
sudo systemctl edit kalinka-renderer
```

In the editor that opens, add these lines, using your server's address, then save and run `sudo systemctl restart kalinka-renderer`:

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/kalinka-renderer --server 192.168.1.50:8000
```

**The library stays empty.** The music folder is wrong, or the `kalusr` user cannot read it; see [Put your music on it](#put-your-music-on-it). A network share also needs the right user name and password.

**Logs.** In the app, **Server settings → General → Support → Download server logs** prepares a ZIP of the server's recent logs to attach to an issue. Kalinka removes passwords and keys from it, but it still contains file names and network addresses, so look through it before you post it. It needs a running server. When the server does not start, read the journal directly: `sudo journalctl -u kalinka` for the server and `sudo journalctl -u kalinka-renderer` for the renderer. Upgrades log under units of their own, so after a failed upgrade include those too; a renderer's is `kalinka-renderer-upgrade`. This saves the last day of all the server's units to a file you can attach:

```bash
sudo journalctl -u kalinka -u kalinka-upgrade -u kalinka-restart --since "1 day ago" > kalinka.log
```

For more detail in the logs, turn on **Expert** in the server settings, search for `log_level` and raise it.

**Asking for help.** Questions and reports are welcome in [the testing thread](https://github.com/Kalinka-Player/KalinkaPlayer/discussions/133). Something you can reproduce is easier to act on as an [issue](https://github.com/Kalinka-Player/KalinkaPlayer/issues).
