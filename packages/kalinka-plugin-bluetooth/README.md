# Bluetooth audio input

Receives Classic Bluetooth A2DP audio on the Core host with BlueZ and BlueALSA
(4.3.1 or newer). Kalinka displays **Bluetooth** as the input and plays through
the selected local or remote renderer. Only the renderer opens the DAC. The
receiver's PCM is forwarded without FLAC encoding, file spooling or a local/
remote compression switch.

Requires SDK 3.21 and a Core/renderer release supporting live PCM v1. Upgrade
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

Enable the plugin in Kalinka settings and restart the server once to load it.
Choose **Pair a phone for two minutes**, save, and select the receiver in the
phone's Bluetooth settings. The action resets after saving. Alternatively,
**Allow audio from a paired phone** authorizes an existing bond. The settings
show receiver status and phones with audio access. Audio access is stored
separately; revoking it does not remove a provisioning bond.

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
