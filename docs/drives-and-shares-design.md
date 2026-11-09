# Drives & shares — design

Drives & shares is a screen under Configuration → Streamer → Storage. It lists every drive and network share the streamer can read music from, mounted or not. From it you mount or unmount each one and choose whether it comes back after a restart. NFS shares found on the network are listed there too.

The screen replaces one thing: the **Mounts** list in Streamer → Storage, with its "+ NFS share" and "+ Disk on this machine" buttons. Everything else stays as it is:

- **Drives,** the source on the main screen, keeps browsing, playing, **Add to library** and **Eject**.
- **The "connected" toast** still appears when a drive is plugged in.
- **Mounting** works as described in [architecture.md](architecture.md): the root helper, the request files, and the storage manager watching mountinfo.

A volume mounted from the new screen appears in Drives like any other.

This document covers the first version: USB drives, disks inside the streamer, and NFS shares. §9 is SMB, which fits the same model and comes later.

## 1. Decisions

### 1.1 A screen, not settings

A mount has live state: mounting, mounted, failed with a reason. It also has actions: mount, unmount, eject. Settings has neither. Each entry was a form whose saved values only said what to try at the next start, and the result was never shown beside it. The two "+" buttons gave no hint of what they were for until tapped.

On the new screen, a row is a device as it is now, with the buttons that apply to it. Settings keeps only the two switches that really are preferences:

- **Mount USB drives automatically**;
- **Show drives as a source**.

### 1.2 One row per device, mounted or not

The screen lists what the streamer could mount, not only what it has. A row comes from any of these:

- **The volumes** `StorageManager.snapshot()` already builds: internal storage, Kalinka's own mounts, and disks the system mounted under `/media`, `/mnt` or `/run/media`.
- **The filesystems nothing has mounted**, from `StorageManager.unmounted_filesystems()`. It already leaves out filesystems in `/etc/fstab`, those on a disk the system runs from, and USB drives while automount is on.
- **The saved devices** in `base_config.storage.mounts`, plugged in or not, so that one taken away can still be forgotten.
- **The NFS exports** the last scan found (§5).

The same device never appears twice. A saved share that the scan also finds is one row, and so is a saved disk that is mounted.

### 1.3 Mount is asked for in the device's dialog

A row that is not mounted has no button, only a chevron. Tapping it opens the device's dialog, with its settings, **Mount after restart** among them, and **Cancel** and **Mount**. A mounted row keeps **Unmount**, or **Eject** for a USB drive, and its dialog ends in **Cancel** and **Save**.

- **Mount** mounts the device now with the settings in the dialog. A disk inside the streamer or an NFS share not saved yet is saved once the mount succeeds, with Mount after restart on unless the dialog turned it off: nothing else would ever mount them, so mounting one is taken as wanting it kept. A USB drive is mounted unsaved unless the dialog changed something of it, because it is plugged in and pulled out.
- **A device is never saved without being mounted** from the screen. A found share whose dialog is cancelled stays found, and one whose mount fails is not kept either: it stays where it was listed, says why it failed, and its dialog still offers Mount, with the settings that were tried. Its failure note can be put away (✕), and the server forgets the try at a restart. A share added by address that fails says why in the Add dialog, to be put right there, and is put away with the dialog.
- **A saved device that fails** stays saved, since it is meant to mount at start: its failure has no ✕, and **Forget** in its dialog is how it goes.
- **Unmount** asks first, then unmounts the device and leaves Mount after restart as it is. The question says what stays, the files and the saved connection, and, where the setting is on, that the device comes back after the streamer restarts.
- **Turning Mount after restart off** in a mounted device's dialog leaves it mounted until the next restart; turning it on mounts nothing now.

A saved device can be **forgotten** from its dialog, which unmounts it and deletes the record.

### 1.4 Mount answers once the device is mounted

**Mount** waits for the outcome rather than for the request to be filed. The server waits up to 10 s for the root helper's answer, short of the app's 15 s timeout, and replies with where the device stands: mounted, failed with the reason, or still mounting.

The row says **Mounting…** beside the name while it waits, then **Mounted**, with **Unmount** as its button. A mount the server answers as still under way stays at Mounting… until the list shows it mounted or failed.

A failure says **Failed** beside the name, and the device's dialog mounts it again. Under the row, a failure note says in plain words what to do, with **Technical details** for the mount program's own words, which the details dialog can copy. The server sends both: `error` is the plain words, `error_detail` the mount's.

- **The mount's words.** systemd-mount only says "Job failed. See journalctl -xe", so the root helper reads the mount unit's journal for what the mount program said.
- **Plain words for NFS.** A share's server that refuses the streamer is told which address to allow, the one a packet from the streamer to it leaves from. A missing export, an NFS version the server does not offer and a server that does not answer each get a sentence of their own. Any other failure of a share says "Check the connection or share settings."; of a drive, "Check that the drive is plugged in and readable."
- **Kalinka's own words** need no details: a helper that cannot be reached or did not answer is said as it is.

A server without the root helper cannot mount anything. It is a plain install, not the package, such as a development checkout. Its list says so in `mounting_available`, every `can_mount` is off, and the mount endpoints answer 503. The screen pins a note above the list, only as wide as its message, and offers no Mount. A request the helper's directory refuses fails at once with "Kalinka could not reach its mount helper"; it never appears as merely not mounted.

### 1.5 USB drives follow automount

While **Mount USB drives automatically** is on, every USB drive mounts by itself as it does today. Its row offers only **Eject** and has no settings, because there is nothing for it to decide.

With automount off, a USB drive is listed as not mounted, and its dialog has **Mount when plugged in**, off by default, and **Mount**. A saved USB drive with it on mounts at every start *and* whenever it is plugged in. A drive that is remembered but left unmounted after being plugged back in would surprise more people than it helps.

### 1.6 A device's id comes from what it is

| Device | Id |
| --- | --- |
| Filesystem | Its UUID |
| NFS share | `nfs-` followed by the first 12 hex digits of the SHA-1 of `host:export` |

The id is the device's directory under `/media/kalinka` and its volume id in `GET /server/storage/volumes`, however it was mounted.

- **The same directory every time.** Automount, an unsaved mount and a saved one all mount a drive at `/media/kalinka/<UUID>`. Saving a record never moves a mounted volume, and a library folder on it keeps its path.
- **Valid record ids.** `RECORD_ID_PATTERN` (`^[A-Za-z0-9_-]{1,64}$`) accepts both forms, so new records take the device's id.
- **Records saved before this change keep their `rec_…` ids.** A saved share is matched to a found one by host and export, not by id.

### 1.7 The server says what can be done

A device carries `can_mount`, `can_unmount`, `can_eject`, `can_set_at_start`, `can_edit` and `can_forget`. `can_edit` says whether it has settings at all. The app shows a button where a flag is set and never works one out from the kind of device. The rules in §4 then live in one place. This is the same arrangement as `can_edit` in [collections-design.md](collections-design.md#15-can_edit-is-how-a-client-knows). The flags are advisory: the endpoints and the root helper refuse what they would refuse anyway.

### 1.8 Looking for shares only while someone looks

Discovery runs only while the screen is open. Each request for the device list is answered from what is already known. If that is older than the scan interval, it starts a new scan in the background. This is how the SMB host finder in localfiles (`suggest/smb_discovery.py`) treats the settings page.

A closed screen costs the network nothing. A host that does not answer holds up nothing either, because probes run on threads of their own with timeouts, as `drives/fsio.py` reads drives.

### 1.9 Saving goes through the config commit

Saving, changing and forgetting a device all edit `base_config.storage.mounts` through the `apply_changes` that `PUT /server/config` uses. `POST /server/storage/adopt` and `/release` already do the same (`storage/routes.py`). Validation, saving and the change reaching the storage manager stay on one path. There is no second store that could disagree with the settings file.

## 2. The screen

It opens from a **Drives & shares** row in Streamer → Storage, in place of the Mounts list. It is built from the settings pages' own pieces: caps section headings, `SettingsCard` rows, the inline status badge plugin rows use, and `KalinkaDialog` for everything asked. Its layout on a wide panel:

```
←  Drives & shares                                          +

ON THIS STREAMER
  Internal storage
  212 GB free
  Archive  ✓ Mounted by the system
  Disk · 2 TB
  Samsung 980                                               ›
  Disk · 1 TB

USB
  SanDisk Ultra  ✓ Mounted                        [Eject]   ›
  USB drive · 31 GB

SAVED NETWORK SHARES
  Music  ✓ Mounted                              [Unmount]   ›
  NFS · nas.local
  flac  ⓘ Failed                                            ›
  NFS · RASPBERRYPI
  ┌ ⓘ RASPBERRYPI does not let this streamer in. Add
  │   192.168.1.50 to the share's NFS exports.
  └   Technical details ›

FOUND ON YOUR NETWORK                                 REFRESH
  video                                                     ›
  NFS · DS920
```

**Rows.**
- A row shows the device's label with its state beside it, then its `summary`. The state is **Mounted**, **Mounting…**, **Unmounting…**, **Ejecting…**, **Failed**, **Not connected**, **Mounted by the system**, or **Not mounted** for a device that cannot be mounted now. A device Kalinka can mount needs no "Not mounted", and a found share has no state of its own.
- Only a mounted row has a button: **Eject** for a USB drive, **Unmount** for anything else. It sits beside the text where both fit and under it on a phone or at large text sizes, as a plugin row's Install does.
- A failed row has the failure note of §1.4 under it.
- A device with `can_edit`, any Kalinka mounts, opens its dialog when tapped, and shows a chevron to say so.

**The device's dialog** opens in the middle of the pane. Its head is one row: the device's icon, and beside it its name, its summary and, for a share, `host:/export`. Then
- **Name**;
- **Mount after restart**, or **Mount when plugged in** for a USB drive. Turning it off where **Add to library** has put the device's music in My Library (the volume's `library.holds`) says under the switch that the library will find the folder missing after a restart;
- **Advanced settings**, folded under one row that says how they are set ("Read-only · NFS 4"): **Read-only** and, for a share, **NFS version**. On a mounted device it says that a change mounts it again, as a changed record is remounted today;
- **Forget share** or **Forget drive** for a saved one, which asks first.

It ends in **Cancel** and **Mount** for a device not mounted; Mount is off, with the reason under the settings, for a drive that is not plugged in or a server that cannot mount. A mounted device's ends in **Cancel** and **Save**. Either sends only what changed: Mount with the mount (`POST /devices/{id}/mount`), Save on its own (`PUT /devices/{id}`).

**Devices without actions.** Rows the screen only reports:
- **Internal storage** shows its free space;
- **a disk the system mounted** is marked "Mounted by the system".

They are listed because people look for them there.

**Empty states and scanning.**
- With nothing mounted and nothing found, the screen still shows Internal storage, and **+** in the title bar.
- While a scan runs, the found section's heading says **Looking…**, and an empty section says "Looking for shares…".
- When a scan finds nothing, the found section says so in one line.
- **REFRESH** asks every host again (`POST /shares/scan`), keeping what they said until they answer.

**Add network share**, the **+** in the title bar, opens in the middle of the pane with the same head, and asks for the **Server** and the **Folder**, with **Mount after restart** on and the same **Advanced settings**. As the server's name is typed, it is asked for its exports, and the Folder field offers them under Browse; a server that lists none, as an NFSv4-only one may, still takes a typed folder (§5). **Mount** saves the share as set and mounts it, and the dialog closes once the server answers, the row then saying how it went. A refusal, such as a folder that is not a path, stays in the dialog to be put right.

## 3. What a device is

`GET /server/storage/devices` answers:

```json
{
  "devices": [
    {
      "id": "nfs-3f9a1c0b7e21",
      "medium": "network",
      "label": "Music",
      "summary": "NFS · DS920",
      "state": "mounted",
      "error": null,
      "error_detail": null,
      "managed_by": "kalinka",
      "saved": true,
      "at_start": true,
      "connected": true,
      "found": true,
      "read_only": true,
      "nfs": {"host": "nas.local", "export": "/volume1/music", "version": "4"},
      "server": "DS920",
      "clients": ["192.168.1.0/24"],
      "library": null,
      "can_mount": false,
      "can_unmount": true,
      "can_eject": false,
      "can_set_at_start": true,
      "can_edit": true,
      "can_forget": true
    }
  ],
  "scan": {"running": false, "finished_at": 1762171200.0},
  "mounting_available": true
}
```

| Field | Meaning |
| --- | --- |
| `medium` | `internal`, `disk`, `usb` or `network`. The `Medium` of `storage/describe.py`, which already picks the row icon (`internal_storage`, `disk`, `usb_drive`, `network_share`). |
| `state` | A `VolumeState`. `failed` carries `error`, in plain words, and `error_detail`, the mount's own, where Kalinka did not word the failure itself (§1.4). A device not saved has a state only while a mount of it from the screen is under way or after it failed. |
| `can_dismiss` | For a device not saved whose mount failed: the failure can be put away (§1.3). |
| `managed_by` | `kalinka` for what Kalinka mounts or can mount; `system` for fstab, a desktop or an automounter. A `system` device has no actions. |
| `saved` | Whether a record in `base_config.storage.mounts` stands for it. |
| `at_start` | Mount after restart. For a device not saved yet, the switch mounting it would keep it with: on for a disk or a share, off for a USB drive. `null` where it does not apply: internal storage, system mounts and USB drives while automount is on. |
| `connected` | `false` for a saved drive that is not plugged in. It can be forgotten, not mounted. |
| `found` | For a share: whether the last scan saw it. A saved share not found is still listed, and a mount attempt says why it failed. |
| `nfs` | Host, export and version, for a share. |
| `server`, `clients` | For a share the scan saw: what its host calls itself, and who the export lets in, as its host says (§5). The screen shows neither on its own; the summary carries the server's name where it helps. |
| `library` | The volume's `library`, for the note under Mount after restart (§2). |
| `mounting_available` | On the list: `false` where the server has no mount helper (§1.4). |

The device list does not repeat the volume list. It adds what the volumes cannot say: devices that are not mounted, and what may be done with each.

For a drive or a disk, `summary` is what `summary_of` gives its volume, its kind and size, so it reads the same in Drives and on the screen. Internal storage, already under On this streamer, says only its free space. For a share, it is `NFS · ` and its server: the name the host announced where the scan saw one, else the host it is mounted from. The settings show its `host:/export` in full.

## 4. Devices and what happens to them

| Device | Listed while | Actions | Mount after restart | After a restart |
| --- | --- | --- | --- | --- |
| Internal storage | Always | None | Never shown | There |
| Mounted by the system | Mounted | None | Never shown | As the system decides |
| Disk inside the streamer | Present, not on the system disk, not in fstab; or saved | Mount in its dialog, or Unmount; Save once mounted; Forget once saved | On, unless turned off in its dialog | Mounted if it is on |
| USB drive, automount on | Plugged in | Eject | Never shown | Mounted by automount |
| USB drive, automount off | Plugged in, or saved | Mount in its dialog, or Eject; Save once mounted; Forget once saved | Off, unless turned on in its dialog | Mounted if it is on, and on every plug-in |
| NFS share found, not saved | It answered the last scan | Mount in its dialog | On, unless turned off in its dialog | Not mounted |
| NFS share saved | Always | Mount in its dialog, or Unmount; Save once mounted; Forget | As set; Unmount does not change it | Mounted if it is on |

- **Eject** stays what it is: an unmount through `POST /server/storage/volumes/{id}/eject` that also powers a USB disk off. It is offered in Drives and on the screen.
- **A USB drive is let go with Eject**, which leaves Mount when plugged in as it is, since the drive is meant to come back. The server still allows Unmount, which leaves it powered; the screen offers one button per row, and Eject is the one a drive about to be pulled out needs.
- **A disk Kalinka refuses**, such as one that `/etc/fstab` places or one on the system disk, is never listed. Every such check stays in the root helper, as it is now.
- **A drive is a USB drive** when its disk is on the USB bus, as the udev rule that mounts drives sees it, not only when udev's `ID_BUS` says `usb`: a disk in a USB enclosure reports `ata`.

## 5. Finding NFS shares

**Candidates.** A scan asks every host that may serve NFS:

- hosts announcing `_nfs._tcp` over mDNS. Where the announcement's TXT record has a `path`, that is an export, even when the host answers nothing else.
- hosts announcing `_smb._tcp`. A NAS usually serves SMB and NFS together, and announces only SMB.
- the hosts of saved shares, and hosts typed into **Add network share**.

The server already depends on `zeroconf` for its own announcement (`service_discovery.py`). The scan watches the two service types with it, while the screen is open.

**Probe.** Each candidate is asked for its exports the way `showmount -e` asks:

1. rpcbind on port 111, `GETPORT` (program 100000, version 2), for the mount daemon (program 100005, version 3) over TCP;
2. the mount daemon, `EXPORT` (procedure 5), whose answer lists each export with the clients it allows.

Both are small ONC RPC calls, written in Python with timeouts of a couple of seconds and a few hosts asked at a time. They run as the server's own user; nothing about listing exports needs root.

**Pacing.** The first request starts the mDNS watch and a thread that asks each candidate again every 30 s. Both stop a minute after the last request. A search counts as running until every host asked has answered and announcements have had a few seconds to arrive.

**Naming the host.** A share found through mDNS is saved under the host's mDNS name when this machine can look that name up, so it survives a change of address. Otherwise it is saved under the host's IPv4 address. Either way the row matches a saved share that names the host by the other, and a found share is saved with NFS version `4`, which lets the client and server settle on a minor version.

**Limits.** People will meet these, and the screen says so where it can:

- **NFSv4-only servers** often run no mount daemon, so there is nothing to ask. They appear only through an mDNS `path`, or by typing the folder into **Add network share**.
- **The clients an export allows** are listed by the API but not shown, nor judged: an address list on a row read as noise. The mount answers whether the streamer is let in, and a refusal shows on the row.
- **File permissions on the share.** NFS checks access by the reading user's id. The server reads as `kalusr`, so files on a share must be readable by everyone, or the NAS must map every client to one user (squashing). This is the NFS form of what [installation.md](installation.md) already says about drives.
- **No broadcast scan.** Hosts that neither announce themselves nor are typed in are not found. Asking the whole subnet would go through rpcbind's call forwarding, which cannot be relied on to be enabled, so it is left out.

## 6. Server

**Endpoints**, all under `/server/storage`:

| Endpoint | Does |
| --- | --- |
| `GET /devices` | The device list (§3), from what is known. Starts a scan when the last one is stale. |
| `POST /devices/{id}/mount` | Mounts a device, with an optional body of `PUT /devices/{id}` applied as it mounts. A saved one has the body saved first. A disk or share not saved yet is saved once the mount succeeds, with `at_start` as listed unless the body says; a USB drive only where the body changes something. One that fails is not saved (§1.3). Answers `{id, state, error, error_detail}` once the helper has, or after 10 s with `state: mounting` (§1.4). |
| `POST /devices/{id}/unmount` | Unmounts it, keeping the record and its `at_start`. Answers as a mount does; a refusal from the helper answers 502 with its reason. |
| `PUT /devices/{id}` | `{at_start, label, read_only, version}`, any subset, where `can_edit`. Saves a device not saved yet, with `at_start` as listed unless the change says, and mounts nothing. The screen sends it only for a mounted device. |
| `DELETE /devices/{id}` | Forgets a saved device: unmounts it and deletes its record. |
| `DELETE /devices/{id}/failure` | Puts away the failed mount of a device not saved, where `can_dismiss`. |
| `POST /shares/probe` | `{host}`. Adds the host to the candidates and answers its exports. |
| `POST /shares/scan` | Asks every candidate again now; what they said last is listed until they answer. |
| `POST /shares` | `{host, export, version, at_start, read_only}`. Mounts a typed share as asked, saving it as a mount does, or a saved one with those settings saved first; answers as a mount does, and 409 where it is mounted already. |

Without the mount helper, both mounts answer 503 and save nothing.

`POST /volumes/{id}/eject` stays as it is. `POST /volumes/{id}/mount`, which mounts a saved record and which no app calls, stays as well.

**Records.** `NfsMount` and `DiskMount` in `config_model.py` gain `at_start: bool = True`; the server sets it off when it saves a USB drive. `DiskMount` also gains `usb: bool = False`, which says where a saved drive is listed while it is away. At start, the storage manager asks for a mount only for records with `at_start` on. After that, saving a record mounts nothing. Taking one out unmounts it. Changing how a mounted one mounts, its read-only setting or NFS version, mounts it again. A change to `at_start`, `label` or `usb` neither mounts nor unmounts. The device endpoints set `at_start` only as §1.3 says, and wait on the helper's answer through `StorageManager.answer`.

The **disk picker** for `DiskMount.fs_uuid` (`StorageManager.filesystem_options`) goes with the Mounts list.

**Remembered USB drives.**
- With automount off, `media-mount mount-device` mounts a USB filesystem only if its UUID is in `/var/lib/kalinka/storage/mount-when-present`.
- The server writes that file next to the `no-automount` marker. Each line is a UUID, followed by ` rw` for a drive saved writable, for each saved drive with Mount when plugged in on whose record is named after its UUID. The helper mounts a drive at its UUID, so a record saved before this change, under a `rec_…` id, is mounted only at start.
- The helper reads it as it reads a request. It accepts only a plain file, never through a link, and only lines that match its UUID pattern. Every check it makes on a filesystem stays.
- With automount on, the helper still mounts a listed drive writable if its line says so; otherwise a writable record would find its drive mounted read-only already.
- At start, the helper may mount such a drive from udev before the server asks. Whichever request comes second finds the drive mounted and does nothing.

**Classes.**
- `kalinka_server/storage/devices.py`, `StorageDevices`: builds the device list from the storage manager, the saved records and the NFS discovery. It also turns the endpoints' actions into config changes and mount requests. A mount of a device not saved is a trial it holds in memory: kept, through the config commit, once the storage manager lists the volume mounted, which it hears through `StorageManager.after_change`; listed with its failure otherwise. `StorageManager` keeps its one job, the volumes, and says how a mount asked for a record stands (`attempt`), saved or not.
- `kalinka_server/storage/device_routes.py`: the endpoints.
- `kalinka_server/storage/nfs_discovery.py`, `NfsDiscovery`: the mDNS watch, the candidate hosts, the probe and its cache. `StorageDevices` depends on it through a small protocol, so tests hand it a fake.

**Settings schema.** The `mounts` list stops being described in the presentation schema. Nothing can hide a saved field today: `exclude` would also drop it from the saved config. So the schema processor gains a flag for a record collection that a dedicated screen edits instead. `automount_usb` and `drives_enabled` stay where they are.

**Versions.** The REST API becomes 0.22, and `GET /server/version` gains `storage.devices: true` beside `volumes`, `mounts`, `adopt` and `folders`. Plugins never see devices, so the SDK does not change.

## 7. App

- **Screen.** `lib/screens/drives_and_shares_screen.dart` is built on `ConfigurationPage` (`lib/widgets/configuration_page.dart`). `plugin_catalog_screen.dart` opens it as a `SlideInPanel` overlay, the way it opens Network: a `_drivesOpen` flag, a callback into `ServerSettingsBody`, and the panel among the overlays with its own `handlesBack`.
- **Entry row.** `ServerSettingsBody` adds a `SettingsActionRow` to the `base_config.storage` section's card, after its own settings, through `SchemaSectionRenderer`'s `trailing` rows. It is shown only when the server reports `storage.devices`.
- **Data.** `StorageDevice` joins `lib/data_model/storage.dart`, beside `StorageVolume`. The calls in §6 join `kalinka_player_api_provider.dart`.
- **Provider.** `storageDevicesProvider` fetches the list and fetches again on `volumes_changed`. The screen asks it again every 2 s while open, with a timer of its own as `NetworkSetupPanel` polls Wi-Fi networks, so the polling ends with the screen. The polling also keeps discovery running (§1.8). Two lists read from the same answer are equal, so a poll that brings nothing new rebuilds nothing.
- **Reused pieces.** Rows sit in a `SettingsCard`, with the inline `ModuleStatusBadge` for their state and the icons from `itemIcon()`. A failure under a row is the `FailureNote` that a failed plugin operation shows, shared from `lib/widgets/failure_note.dart`. The dialogs are centred `KalinkaDialog`s, the device's and Add network share with `iconBeside`, which sets the icon tile beside the title and message. They hold `SettingsRow`s, `SettingsToggle`, `SettingsEnumPills` and, for the folder, `SettingsComboInput`. `ConfigurationPage` takes the title bar's one icon action, here **+**.

## 8. Compatibility

- **An older app on a new server** no longer sees the Mounts list. Shares and disks saved there keep mounting at start, and Drives and the two switches work as before.
- **A new app on an older server** sees no `storage.devices`, so it shows no row. The server's schema still describes the Mounts list, and the app shows it as it does now.
- **Saved records** need no migration. Missing `at_start` reads as on, which is how they behave today, and their `rec_…` ids stay their volume ids.

## 9. Later: SMB

SMB shares join the same screen and the same model. A mounted share is a volume like an NFS one, so Drives browses it and **Add to library** offers it.

- **Discovery.** `SmbHostDiscovery` moves from localfiles into the SDK, as SDK 3.20 moved the media readers, so the server and My Library share one finder. Listing the shares on a host is new: it needs a login on most NAS boxes, so a found SMB host opens a sign-in, then its shares.
- **Mounting.** The root helper mounts with cifs. The server keeps the password in the record behind the `password` widget, so it is never sent to a client and is logged as `<secret>`. It reaches the helper in a request the helper turns into a credentials file only root can read, under `/run/kalinka-mounts`. No result or error repeats it.
- **My Library's Network share** stays. It reads a share into the index without mounting it, which is right for a library. Drives & shares is for playing straight from a share.

## 10. Deliberate exclusions

- Nothing in Drives changes or moves to the screen. **Add to library** stays only in Drives.
- The screen is not opened from Drives.
- Kalinka never edits `/etc/fstab`, never mounts a partition of the disk the system runs from, and keeps read-only as every mount's default.
- No broadcast scans for NFS (§5).
- No sharing out: the streamer reads shares and serves none.

## 11. Delivery

Each step ships with its tests in the same commit.

1. **Devices on the server.**
   - Covers internal storage, system mounts, disks, USB drives and saved shares; mount, unmount, Mount after restart and forget; waiting on the helper's answer; a server without the helper; `at_start`; the schema flag; API 0.22.
   - Tests: `tests/test_storage_devices.py` for the list, the rules of §4 and the answers, against a fake helper; route tests beside `test_storage_routes.py`; `test_storage_manager.py` for `at_start` at start; `test_storage_requests.py` for a helper that is missing or refuses the request file.
2. **Finding NFS shares.**
   - Covers `NfsDiscovery`, `/shares/probe` and `/shares`.
   - Tests: `tests/test_nfs_discovery.py`, with the rpcbind and mount calls answered by fake sockets and their XDR checked byte for byte, plus a dead host that times out.
3. **Remembered USB drives in the root helper.**
   - Tests: added to `test_media_mount.py`. A listed drive mounts with automount off, an unlisted one does not, and a link or a malformed line in the list is refused.
4. **The screen in the app.**
   - Tests: `test/drives_and_shares_screen_test.dart` for rows and their states, a button only on a mounted row and where it sits, Mount from the dialog with its changes and then Mounting… and Mounted, Save only for a mounted device, a failed mount and its details, a failed try put away, a share added by address that fails staying in its dialog, a found share moving groups as it mounts, the Unmount question, what Save sends, Forget, Add network share, Refresh, a server without the helper and a demo server; the Streamer row in `plugin_catalog_ui_test.dart`.

[installation.md](installation.md) and the storage paragraph of [architecture.md](architecture.md) change with step 1.
