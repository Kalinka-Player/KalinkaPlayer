# Installation

## Ready-to-flash images

If the machine has nothing on it yet, skip the OS install: the [`kalinka-image-v*` releases](https://github.com/Kalinka-Player/KalinkaPlayer/releases?q=kalinka-image-v&expanded=true) carry a minimal Debian 13 with the whole player already installed — one for the Raspberry Pi 4 / 400 / CM4, one for any x86-64 PC or virtual machine. Flash it, power it on, open `http://<its-ip>:8000`, and it plays. The root filesystem grows into the card by itself, and a file on the boot partition sets up a login account and Wi-Fi if you want them. See [`packages/kalinka-image/README.md`](../packages/kalinka-image/README.md).

## Quick install

On a machine that already runs Debian, Ubuntu or Raspberry Pi OS, one command sets up a complete player:

```bash
curl -fsSL https://kalinkaplayer.com/install.sh | sudo bash
```

It installs the app bundle (server, plugins, SDK), the browser player (`kalinka-web`), and the renderer (`kalinka-renderer`) on that same machine — so audio plays through its sound card straight away — and both services start on boot. Re-running it upgrades everything that is already installed.

Two environment variables opt out of the extras: `KALINKA_RENDERER=0` for a server that only drives renderers on other machines, and `KALINKA_WEB=0` to leave out the browser player. Note that the web player needs no renderer of its own: the browser *is* the output, so a server reached from a browser plays audio with nothing else installed.

## Getting started

The installer leaves you with a running server, and the rest of the setup happens from a client rather than in config files — the server has nothing to edit by hand.

**1. Get a client, and finish the setup in it.** Either one works, and you can use both:

- **The Kalinka Music App** — [Android, Linux and Windows builds](https://github.com/Kalinka-Player/KalinkaAI/releases/latest). It finds the server on its own; if discovery is blocked on your network, **Settings → Connection** takes a host and port by hand.
- **Any web browser** — go to `http://<server-ip>:8000`. Nothing to install, and the page plays audio itself, so you can hear something without setting up an output first.

Whichever you open walks you through first-time setup: naming the server, pointing it at your music and choosing where sound comes out. The step-by-step version with screenshots lives in the app repo — [first-run setup guide](https://github.com/Kalinka-Player/KalinkaAI/blob/main/docs/first-run-setup.md).

**2. Add music.** The installer creates `/srv/kalinka/music`, writable by anyone so you can drop files in over SFTP or a file manager without `sudo`:

```bash
scp -r ~/my-albums/* you@<server-ip>:/srv/kalinka/music/
```

Already have a music folder elsewhere? Leave that directory alone and set **Settings → Local Library → music folders** to your path instead. The server reads your files as the `kalusr` user, so make sure it can: a directory it cannot enter, or files dropped with a `0600` umask, simply stay invisible.

**3. Wait for the library.** Indexing starts on its own and metadata enrichment follows — the app shows progress, and a large collection on a Pi takes a while the first time. Nothing needs restarting.

**4. Choose where the sound comes out.** The renderer installed alongside the server appears as an output straight away; the browser you opened in step 1 is another. Pick one in the app and press play. The speaker test tone in the output settings confirms you picked the right box before you trust it with an album.

**5. Check on it whenever you need to:**

```bash
systemctl status kalinka            # the server
systemctl status kalinka-renderer   # the thing that makes sound
journalctl -u kalinka -f            # follow the server log
```

Optional extras live in **Settings**: a free [AcoustID](https://acoustid.org/) key improves metadata repair, a free [Jamendo](https://developer.jamendo.com/) client id adds their Creative-Commons catalog, and **AI search** turns on semantic search over your own library (see [Smart Search](#smart-search) — it wants 4 GB of RAM).

## Troubleshooting

**No sound, or no outputs to pick from.** Something has to play the audio, and the server does not — check the renderer with `systemctl status kalinka-renderer`. If the unit doesn't exist, no renderer is installed on that machine: run the renderer installer below. Meanwhile the browser player at `http://<server-ip>:8000` always works, since the browser is its own output.

**A renderer on another machine never appears.** It finds the server over mDNS, so multicast has to reach it — see the caveats under *Renderers on other machines*. `journalctl -u kalinka-renderer -f` on that box says what it sees: `[Discovery] Found …` means it worked, silence means the announcements aren't arriving, and a line about `renderer_proto` means that server announces no renderer support at all.

**The app can't find the server.** Same mDNS story, one layer up. Enter the address by hand in **Settings → Connection** to confirm the server is otherwise fine — if that works, the problem is discovery, not Kalinka.

**Music doesn't show up.** Check permissions first (step 2 above), then the indexer status in the app. Files still being copied are deliberately ignored until they stop changing, so a large upload appears only once it lands.

**Logs.** In the app, **Settings → Support → Download server logs** prepares a ZIP of the server's recent logs to attach to an issue. The server leaves out every line holding a credential it knows of or anything that looks like one, but skim the file before posting it: it also holds file and track names and network addresses. It needs a running server; when the server fails to start, read the journal directly. Both log to the journal: `journalctl -u kalinka` for the server, `journalctl -u kalinka-renderer` for the renderer. An upgrade and the restart after it log under units of their own, so after a failed upgrade ask for those too (`kalinka-renderer-upgrade` for the renderer's). This saves the last day of all three server units to a file you can attach to an issue:

```bash
sudo journalctl -u kalinka -u kalinka-upgrade -u kalinka-restart --since "1 day ago" > kalinka.log
```

Raise the server's verbosity with `log_level` in **Settings**.

## Renderers on other machines

The renderer bundled by the quick install plays through the machine the server runs on. To make another box an output — a Pi wired to an amplifier, say — install only the renderer there:

```bash
curl -fsSL https://kalinkaplayer.com/install-renderer.sh | sudo bash
```

It picks the right package for that machine (`.deb` on Debian/Ubuntu, `.rpm` on Fedora, per architecture) and starts the service; there is also a flatpak for other distributions, see [`packages/kalinka-renderer/flatpak/README.md`](../packages/kalinka-renderer/flatpak/README.md). Renderers appear in the app's output list on their own — but only if they can hear the server: **discovery is mDNS**, so the renderer and the server must share a network segment where multicast to `224.0.0.251:5353` gets through. Wi-Fi access points with client isolation or multicast filtering, VLANs without an mDNS reflector and Docker's default bridge all block it; put both ends on the same subnet, or bridge mDNS across it. Once a renderer has found the server it connects out to it and fetches media over HTTP itself, so nothing else needs opening in the other direction.

Upgrading one afterwards does not need a shell on that machine. The app's output list carries an upgrade button on any renderer a published release would bring forward, and offers it as the fix on one that has fallen too far behind to play at all; the server brings its renderers forward before it upgrades itself, so a set of them moves in the order that keeps working; and `systemctl enable --now kalinka-renderer-upgrade.timer` on the renderer has it install the latest release nightly with no server involved — worth enabling on a box nobody will be standing next to. Re-running the install command by hand still works too.

A renderer installed some other way — the flatpak, or built from source — reports that it cannot replace itself, and is never offered an upgrade it could not perform.

## Updating

With `server.auto_upgrade` enabled the server checks the published releases hourly and upgrades itself during quiet hours while playback is stopped; otherwise the app offers the upgrade and you press the button. Either way it runs the same installer as above, so the server, the plugins, the browser player and the local renderer all move together — the renderer has its own release train (`kalinka-renderer-v*`) and version, and is upgraded whenever a newer one has been published. The renderer goes in first: a release can move the protocol the two speak, and a renderer understands the version before its own as well as its own, so upgrading it ahead of the server never leaves the machine in the pairing that does not work. Renderers on other machines are brought forward the same way, before the server moves.

