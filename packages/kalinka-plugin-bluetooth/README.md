# Bluetooth audio input

Receives Classic Bluetooth A2DP audio on the Core host with BlueZ and BlueALSA
(4.3.1 or newer). Kalinka displays **Bluetooth** as the input and plays through
the selected local or remote renderer. Only the renderer opens the DAC. The
receiver's PCM is forwarded without FLAC encoding, file spooling or a local/
remote compression switch.

BlueALSA is used as a Bluetooth receiver and codec decoder. The plugin calls
its D-Bus `PCM1.Open` method once and reads PCM directly from the returned file
descriptor; samples do not travel in D-Bus messages. This capture path uses
neither ALSA PCM devices nor `arecord`, PipeWire, or a local playback loopback.
Replacing BlueALSA with raw BlueZ transport access would also require owning
A2DP codec negotiation, packet handling, decoding and reconnect behaviour.

Requires SDK 3.22 and a Core/renderer release supporting live PCM v1. Upgrade
remote renderers too; an older renderer reports an upgrade requirement.

## Setup

Install `kalinka-plugin-bluetooth`, BlueZ and BlueALSA (Python 3.11+).
Build the Debian package with `./scripts/build_deb.sh` from this package
directory. The Fedora server RPM bundles the plugin and service files;
install its optional BlueALSA backend before enabling the input. On Debian the plugin
package depends on `bluez-alsa-utils` and installs a dedicated
`kalinka-bluealsa.service`, configured for **A2DP sink** with SBC as the
baseline codec. Disable any
other BlueALSA instance on the same adapter. Do not run `bluealsa-aplay`:
Kalinka's renderer owns playback. Desktop PipeWire/WirePlumber Bluetooth audio
management must also be disabled for this adapter before using BlueALSA.

After selecting this backend, run `sudo systemctl enable --now kalinka-bluealsa`.
Package installation leaves that choice to the administrator so it does not
stop a Bluetooth manager already in use.

If BlueZ or BlueALSA is unavailable, the plugin keeps the error in receiver
status and logs it once while it persists. Reconnection backs off from two
seconds to at most 30 seconds; starting the backend allows automatic recovery.
Leave the plugin disabled on hosts that do not receive Bluetooth audio.

Open **Connect a device** on Home or in the server menu. Enable Bluetooth,
choose **Pair a device**, then select the displayed name in your phone's
Bluetooth settings. These changes apply without restarting Core, including
the first enable after boot. Pairing closes after two minutes or the first
successful A2DP connection; playback need not start to finish pairing.

The panel also offers **Allow audio** for an existing bond, **Disconnect audio**
without losing permission, and **Remove audio access**. Removing access restores
trust to its pre-grant value and disconnects the A2DP profile; it keeps the bond
and provisioning access. The receiver denies new ungranted audio connections,
including while disabled. Grants from older test images have an unknown prior
trust value: preserve that trust but still disconnect audio on revocation.

Only persistent configuration belongs in plugin settings. Leave **Bluetooth
name** empty to follow the streamer name. A custom name overrides it. A rename
waits while provisioning owns the shared adapter lease.

Pairing and the supervisor's BLE setup share an adapter lease under
`/run/lock/kalinka-bluetooth`. Pairing reports busy while BLE setup owns it.
Neither disabling the audio plugin nor closing its pairing window powers off
the adapter. Upgrade the supervisor alongside this plugin to enable that
coordination. The plugin runs as `kalusr`; the package installs its narrow
D-Bus policy and runtime-directory ownership.

The first active authorized sender owns playback. Play/pause/next/previous use
AVRCP where the phone supports it; metadata has a device-name fallback. A queue
takeover pauses the sender and blocks automatic reclaim until a new sender
play transition or reconnect. Output changes keep capture running and start
fresh audio on the new renderer. Absolute volume is routed to Kalinka's output
volume authority, with BlueALSA software gain disabled.

## Development and limits

Install with `pip install -e packages/kalinka-plugin-sdk -e
packages/kalinka-plugin-bluetooth`. Use `KALINKA_PREFIX` for a writable local
state/runtime root; system D-Bus still needs the packaged permissions or a
development policy for your own user. Tests use fake BlueZ/BlueALSA state and
real pipes, without changing the host's Bluetooth configuration.

The backend accepts the actual signed little-endian 16/24/32-bit mono/stereo
PCM format. It supports the BlueALSA `Sampling`/`Rate` and packed/array volume
API differences. Optional codecs depend on the installed BlueALSA build;
HFP/HSP and LE Audio are outside this input. Ethernet is recommended when
Wi-Fi cannot maintain the live budget. Phone-to-speaker latency and optional
codec interoperability still require measurements on the intended hardware.
