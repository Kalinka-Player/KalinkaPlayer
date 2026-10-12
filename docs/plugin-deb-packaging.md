# Kalinka Plugin — Debian Package Structure

This document describes the required layout and conventions for a Kalinka plugin `.deb` package, for both in-tree plugins and external third-party plugins.

---

## Runtime Overview

At startup, `kalinka.service` runs `/opt/kalinka/bootstrap.sh` (via `ExecStartPre`) **as root** before launching the server binary. The bootstrap script:

1. Creates `/opt/kalinka/venv` if it does not exist.
2. Scans `/opt/kalinka/wheels/*.whl` and pip-installs everything it finds.

Plugins therefore do not need to install themselves into the venv during `postinst`. They only need to:
- Place their wheel under `/opt/kalinka/wheels/`
- Declare a dpkg trigger so the server restarts and picks up the new wheel.

---

## Package Filesystem Layout

```
<plugin-name>_<version>_all.deb
└── opt/
    └── kalinka/
        └── wheels/
            └── <plugin_id>-<version>-py3-none-any.whl   ← the Python wheel
```

No other files need to be installed by a plugin package.

---

## DEBIAN/ Control Files

### `DEBIAN/control`  *(required)*

```
Source: kalinka-plugin-<name>
Priority: optional
Maintainer: Your Name <you@example.com>
Build-Depends: debhelper (>= 9)
Standards-Version: 3.9.6
Package: kalinka-plugin-<name>
Version: <version>
Architecture: all
Depends: kalinka-server (>= <min-server-version>)
Description: <Short description>
 <Long description.>
```

`Depends: kalinka-server` ensures the server (and therefore the venv bootstrap mechanism) is present before the plugin is installed.

---

### `DEBIAN/triggers`  *(required)*

```
activate-noawait kalinka-server-restart
```

This is the **only action** the plugin needs to request on install. dpkg will activate the `kalinka-server-restart` trigger, and the kalinka-server package handles restarting the service — **once**, even if multiple plugin packages are installed in the same `dpkg` invocation. Removal activates it too, so the server comes back without the plugin.

The server's trigger handler runs `systemctl try-restart`: a running server restarts, a stopped one stays stopped. Kalinka's own package operations, an install or removal asked for from the app or a removal from the supervisor's page, stop the server first and start it once afterwards, so the plugin's trigger does not start it a second time half way through.

---

### `DEBIAN/prerm`  *(required)*

The prerm script should pip-uninstall the plugin from the venv before dpkg removes the wheel file:

```sh
#!/bin/sh
set -e
case "$1" in
  remove|deconfigure)
    VENVDIR="/opt/kalinka/venv"
    if [ -x "$VENVDIR/bin/pip" ]; then
      "$VENVDIR/bin/pip" uninstall -y kalinka-plugin-<name> || true
    fi
    ;;
esac
exit 0
```

Make the script executable (`chmod 755`).

> **Note:** There is no `postinst` script in plugin packages. Installation into the venv is handled by the server's bootstrap on next startup.

---

## How the Restart Trigger Works

| Step | Actor |
|------|-------|
| `dpkg -i plugin-a.deb plugin-b.deb` | dpkg installs both packages, both activate `kalinka-server-restart` |
| dpkg processes pending triggers | kalinka-server's `postinst triggered` is called **once** |
| `postinst` runs `systemctl try-restart kalinka.service` | A running service restarts |
| `ExecStartPre=+/opt/kalinka/bootstrap.sh` | Bootstrap pip-installs all wheels in `/opt/kalinka/wheels/` |
| `ExecStart` | Server starts with new plugin available |

The `activate-noawait` directive means the plugin package does not wait for the trigger to be processed before completing its own installation, which avoids circular dependency issues.

---

## Minimal `build_deb.sh` Reference

```bash
#!/usr/bin/env bash
set -euo pipefail

PLUGIN_SLUG="kalinka-plugin-<name>"
PLUGIN_ID="kalinka_plugin_<name>"   # underscore form, matches wheel filename

rm -rf pkgroot/
mkdir -p pkgroot/opt/kalinka/wheels pkgroot/DEBIAN

# Build wheel
./scripts/build_wheel.sh
WHEEL_PATH=$(ls dist/*.whl | head -1)
VERSION=$(basename "$WHEEL_PATH" | sed "s/${PLUGIN_ID}-\\(.*\\)-py3-none-any\\.whl/\\1/")

# Copy wheel into package root
cp "dist/${PLUGIN_ID}-${VERSION}-py3-none-any.whl" pkgroot/opt/kalinka/wheels/

# Generate control file
sed "s/@VERSION@/${VERSION}/g" debian/control.in > pkgroot/DEBIAN/control

# Copy dpkg scripts
cp debian/triggers pkgroot/DEBIAN/triggers
cp debian/prerm    pkgroot/DEBIAN/prerm
chmod 755 pkgroot/DEBIAN/prerm

dpkg-deb --root-owner-group --build pkgroot "${PLUGIN_SLUG}_${VERSION}_all.deb"
```

---

## Checklist for a New External Plugin

- [ ] `DEBIAN/control` — correct `Package`, `Version`, `Depends: kalinka-server`
- [ ] `DEBIAN/triggers` — contains `activate-noawait kalinka-server-restart`
- [ ] `DEBIAN/prerm` — pip-uninstalls the package, executable
- [ ] Wheel placed at `opt/kalinka/wheels/<plugin_id>-<version>-py3-none-any.whl`
- [ ] Plugin registers its entry point under the `kalinka.plugins` group in `pyproject.toml`:
  ```toml
  [project.entry-points."kalinka.plugins"]
  my_plugin = "kalinka_plugin_myname:MyPluginClass"
  ```
- [ ] No `postinst` script — the bootstrap handles venv installation at server startup
