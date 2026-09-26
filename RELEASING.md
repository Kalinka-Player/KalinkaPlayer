# Releasing & Versioning

How to cut a release and bump versions in this monorepo. Read this before
tagging or changing any version — it is the source of truth for the process.

## The model (four version lines)

There are four independent things to version, and they work differently:

| What | Packages | Versioned by | Needs a git tag? |
|------|----------|--------------|------------------|
| **App bundle** | `kalinka-server`, `kalinka-plugin-localfiles`, `kalinka-plugin-musiccast`, `kalinka-plugin-dummydevice` | A single `kalinka-vX.Y.Z` git tag (via setuptools_scm) | **Yes** |
| **Renderer** | `kalinka-renderer` (deb/rpm/flatpak) | Its own `kalinka-renderer-vX.Y.Z` git tag | **Yes** (its own) |
| **Plugin SDK** | `kalinka-plugin-sdk` | Its **own SemVer** — a constant in source | **No** |
| **Appliance images** | `kalinka-*-rpi234-arm64.img.xz`, `kalinka-*-rpi5-arm64.img.xz`, `kalinka-*-amd64.img.xz` | Their own `kalinka-image-vX.Y.Z` git tag | **Yes** (its own) |

- The **app bundle** is lockstep: one tag versions the server and all
  first-party plugins together.
- The **renderer** has its own release train: a `kalinka-renderer-v*` tag
  builds only the renderer packages, and an app-bundle release never rebuilds
  or re-ships the renderer. Devices upgrade the renderer only when its own
  version moves — a server patch release doesn't restart renderers mid-playback.
- The **appliance images** have their own train too, and for the same reason twice over: they change when Debian or DietPi does or when the first-boot machinery does, none of which is an app release, and they are over a gigabyte of assets that has no business riding along with every patch. Which app-bundle release goes inside them is a separate choice made at build time.
- The **SDK** is the plugin API contract, versioned by its **own SemVer**,
  independent of the app/`kalinka-v*` version. **Major** = a breaking API
  change; **minor** = a backwards-compatible addition; **patch** = a fix.
  Plugins and the server depend on it as `kalinka-plugin-sdk>=1,<2`, so
  backwards-compatible minor/patch bumps (`1.0 → 1.1 → …`) never break existing
  plugins, while a major bump (`→ 2.0`) does — those plugins must be re-pinned
  and rebuilt. The SDK's `1.x` measures **API compatibility**; the app's `0.x`
  measures **product maturity** — they are different axes and are expected to
  differ. The server also verifies the installed SDK major at startup and
  refuses to run on a mismatch (see [Runtime enforcement](#runtime-enforcement-startup-guard)).

Old per-package tags (`kalinka-server-v*`, `kalinka-plugin-*-v*`, `release-*`)
are inert — nothing matches them anymore. Leave them or delete them; they have
no effect.

---

## When to release

SemVer picks the digit, the calendar picks the date. Keep those two decisions apart: a week that produced one feature is still a MINOR even if it merged the day after a release, and a week of nothing but fixes is still a PATCH even when the window comes round. The moment the calendar starts choosing the number, the number stops meaning anything.

| Line | How often |
|------|-----------|
| **App bundle** (`kalinka-v*`) | At most weekly. Finished work waits for the window. |
| **Renderer** (`kalinka-renderer-v*`) | Only when renderer code changes — no schedule. |
| **SDK** | Not a release at all: bump it in the commit that changes the API. |

- **App bundle — a weekly ceiling, not a weekly quota.** Every release is an `install-release.sh` run for every user, so four releases in a week is four interruptions carrying value they would have taken in one. Nothing says you must ship each week; the rule only caps how often. Between windows `main` accumulates unreleased work, which is fine as long as `main` stays releasable — that is the standing cost of holding the line.
- **A defect that is hurting users in the field ships immediately**, as a PATCH, window or no window. Having that escape hatch is exactly what makes the weekly ceiling comfortable to hold for everything else.
- **The renderer is deliberately rarer.** Upgrading it restarts playback on the device, so it moves only when its own code changes and never merely to keep pace with a server release. When a change spans both, release the renderer first so no renderer is left talking to a server it cannot understand.
- **The SDK is a contract identifier, not a shipping event.** It costs one line and triggers no CI, so batching bumps saves nothing and risks the one thing the number is for: `make build-all-deb` publishes `kalinka-plugin-sdk_<version>_all.deb` on every app release, and shipping two different APIs under one version leaves plugin authors unable to pin the difference. Bumping in the same commit as the API change also means it can never be forgotten.

---

## Release the app bundle (server + plugins)

1. Make sure `main` is at the commit you want to ship and the working tree is
   **clean** (a dirty tree produces a `…devN+dirty` version, not a clean one).

2. Tag and push — **one tag releases everything**:
   ```bash
   git tag kalinka-v0.2.0
   git push origin kalinka-v0.2.0
   ```
   Pick `X.Y.Z` by SemVer: MAJOR = breaking server↔client / user-facing change,
   MINOR = backward-compatible feature, PATCH = fixes.

3. Build the `.deb` packages from the tagged commit:
   ```bash
   make build-all-deb      # builds server + all plugins, output in debs/
   ```
   `debs/` is wiped at the start of each build, so it contains only this
   release's artifacts. Every app package will be named `…0.2.0…`; the bundled
   SDK keeps its own SemVer version (e.g. `kalinka-plugin-sdk_1.0.0_all.deb`).

4. Write the release notes. The tag push has `release.yml` publish the release with install instructions and a folded commit log, and nothing that says what changed. Once the workflow has finished, put that above them by hand:
   ```bash
   gh release view kalinka-vX.Y.Z --json body -q .body > generated.md
   $EDITOR highlights.md
   cat highlights.md generated.md > notes.md
   gh release edit kalinka-vX.Y.Z --notes-file notes.md
   ```
   Follow the previous release: a one-line summary, then `## What's new` or `## Fixes` with one bold lead per item telling users what they will notice, then `## Before you upgrade` saying which app and renderer versions it works with. Leave out what users cannot see, such as test-only commits; the commit log already lists them.

> The version comes from `git describe`, so **build from the tagged commit with
> a clean tree**. Between tags you'll get dev versions like `0.2.1.dev3+g<sha>.dYYYYMMDD`,
> which is expected for development builds but not for a release.

### The browser UI package (kalinka-web)

The browser player is released independently from the
[KalinkaAI repo](https://github.com/madenvel/KalinkaAI/releases) (versioned by
the app, not by `kalinka-v*`); the server serves its bundle from
`/usr/share/kalinka-web`. It is **not** attached to server releases —
`install-release.sh` fetches the latest `kalinka-web_*_all.deb` straight from
the app repo at install time, so the two release cadences are decoupled and a
web-UI update ships to users on their next `install-release.sh` run without a
server release. Nothing to do here when cutting a server release.

---

## Release the renderer

1. Tag and push from the commit you want to ship:
   ```bash
   git tag kalinka-renderer-v0.1.0
   git push origin kalinka-renderer-v0.1.0
   ```
   The `renderer-release.yml` workflow builds the debs (Debian 13 and
   Ubuntu 24.04, each on arm64 and amd64), the Fedora 44 RPMs (both
   arches) and the flatpak bundles, and publishes them to the tag's own
   release — never marked "latest" (that slot belongs to `kalinka-v*`).
   Users install with `scripts/install-renderer.sh` (served from `main`,
   like `install-release.sh`), which picks deb vs rpm and the right
   distro and arch for the machine it runs on.

   Publishing this release reaches existing installs on its own:
   `install-release.sh` runs the renderer installer for the machine it is
   on, and the server's update check watches this tag family, so a server
   with auto-upgrade on picks up a renderer release without a `kalinka-v*`
   release to carry it. Renderers on separate boxes still upgrade by
   running the script there.

2. The package version comes from the tag; the `VERSION` in
   `packages/kalinka-renderer/CMakeLists.txt` is only the dev-build fallback.
   Bump it to match the tag when convenient, not as a release step.

Local builds (current distro/arch only, for testing the packaging):
```bash
make renderer-deb    # stripped Release build -> packages/kalinka-renderer/*.deb
make renderer-rpm    # -> packages/kalinka-renderer/*.rpm
```

---

## Release the appliance images

Ready-to-flash images with the whole player already installed — two for the Raspberry Pi, built on DietPi, and one for x86-64 on Debian. See [`packages/kalinka-image/README.md`](packages/kalinka-image/README.md) for what is in them and how they are built.

1. Tag and push. The version is the image train's own, not the app bundle's:
   ```bash
   git tag kalinka-image-v1.0.0
   git push origin kalinka-image-v1.0.0
   ```
   `image-release.yml` builds each image on a runner of its own architecture — nothing is emulated — and publishes all three to the tag's own release, never marked "latest".

2. **Pick the app-bundle release to bake in.** The tag push installs whatever is the latest published `kalinka-v*` at build time, which is usually what you want. To pin a specific one, run the workflow by hand instead (**Actions → Image release → Run workflow**) with `kalinka_version` set; it also takes the existing tag to publish under. The image filename carries the version that actually landed inside it, not the one that was asked for.

3. Cut an image release when the OS side changes — a Debian point release or a DietPi release worth picking up, a first-boot or partitioning fix — or when the app bundle has moved far enough that a freshly flashed card would otherwise spend its first boot upgrading. Not on every app release: the images auto-upgrade like any other install.

Local builds (needs root, and `qemu-user-static` under `binfmt_misc` to cross-build):
```bash
sudo make image-rpi234
sudo make image-rpi5
sudo make image-amd64 KALINKA_VERSION=4.3.2
make image-test
```

---

## Data releases (models, indexes) — never let them become "latest"

Releases that host data assets rather than software (`jamendo-ai-*`,
`clap-onnx-*`) are cut by hand. **Always create them with `--latest=false`**:

```bash
gh release create jamendo-ai-v2 --latest=false --title "…" --notes "…" <assets…>
```

GitHub marks the most recently created release as "latest" by default, and
`/releases/latest` is what the website's download buttons link to and what
`scripts/install-release.sh` users expect — it must always resolve to a
`kalinka-v*` software release. (This went wrong once: `jamendo-ai-v1` was
published a day after `kalinka-v3.2.0` and the download buttons landed users on
sqlite index files.) The release workflow re-pins `--latest` whenever it
publishes a `kalinka-v*` release, but don't rely on publish ordering to fix a
mislabeled data release.

---

## Bump the SDK version

The SDK version lives in **one place**:

- [`packages/kalinka-plugin-sdk/src/kalinka_plugin_sdk/_version.py`](packages/kalinka-plugin-sdk/src/kalinka_plugin_sdk/_version.py) → `__version__ = "1.0.0"`

`pyproject.toml` derives the packaging version from it via
`[tool.setuptools.dynamic] version = {attr = "kalinka_plugin_sdk._version.__version__"}`,
so you never edit the version in two places.

### Minor or patch (backwards compatible — e.g. `1.0.0` → `1.1.0`)
Added an API, fixed a bug, nothing removed/changed:

1. Edit `__version__` in `_version.py` — in the same commit as the API change, not as a later release chore ([When to release](#when-to-release)).
2. Done. Consumers pin `>=1,<2`, which already accepts it — **no plugin
   changes, no re-pinning, no rebuild required**. Existing plugins keep working.

### Major (breaking — e.g. `1.x` → `2.0.0`)
Removed or changed an existing public API (a protocol change):

1. Edit `__version__` in `_version.py` to `2.0.0`.
2. Widen **every consumer pin** from `<2` to `<3`, i.e. `kalinka-plugin-sdk>=2,<3`. Each consumer pins twice — `pyproject.toml` for the wheel, `debian/control.in` for the deb — and the deb pin is the one that decides whether `apt` will install the set at all:
   - `packages/kalinka-server/pyproject.toml` and `packages/kalinka-server/DEBIAN/control.in`
   - `packages/kalinka-plugin-localfiles/` — `pyproject.toml` and `debian/control.in`
   - `packages/kalinka-plugin-jamendo/` — likewise
   - `packages/kalinka-plugin-musiccast/` — likewise
   - `packages/kalinka-plugin-dummydevice/` — likewise
   - `template/cookiecutter-kalinka-plugin/{{cookiecutter.plugin_name}}/debian/control.in`, so a plugin generated after the bump is born on the new major
3. Raise each plugin's `REQUIRES_SDK` floor to the new major. This is a
   *second* gate, checked when the plugin is loaded rather than installed: a
   plugin left at `>=1.0,<2` is skipped at startup even though its package pin
   resolved. The built-in renderer output device declares one too.
4. Update the plugins/server to the new API and confirm they build & run.
5. Release any out-of-tree plugin against the new major — `kalinka-plugin-qobuz`
   lives in its own repo and is not covered by the greps below.
6. Cut a new `kalinka-vX.Y.Z` app release — a protocol break is a server change,
   so the **server version moves with it**. Third-party plugins built for the old
   major won't install against the new server (their `<2` excludes SDK `2.x`).

Find the spots to touch:
```bash
grep -rn 'kalinka-plugin-sdk *[>=<]' packages/*/pyproject.toml   # the 5 consumer pins
grep -rn 'kalinka-plugin-sdk (' packages/*/debian/control.in packages/kalinka-server/DEBIAN/control.in template/*/*/debian/control.in  # the deb pins
grep -rn 'REQUIRES_SDK' packages/*/src --include='*.py'          # the load-time floors
grep -n  '__version__' packages/kalinka-plugin-sdk/src/kalinka_plugin_sdk/_version.py  # the 1 SDK source
```

The SDK ships in the same `make build-all-deb` run as the app bundle; it does
not need a tag or a separate release step.

### Runtime enforcement (startup guard)

Dependency pins only fire when an install goes through the resolver. As a
backstop, the **server checks the installed SDK at startup**: it reads its own
`kalinka-plugin-sdk` requirement (from package metadata — the same `>=1,<2`
pin, no second source of truth) and **refuses to start** if the installed SDK
falls outside it. This catches the cases pins can't — `pip install --no-deps`,
`dpkg --force-depends`, or upgrading the SDK in place to a different major.
"No legacy SDK" means the server supports only the SDK **major** it was built
against. The check lives in
[`sdk_compat.py`](packages/kalinka-server/src/kalinka_server/sdk_compat.py).

---

## Quick reference

```bash
# Cut an app release
git tag kalinka-vX.Y.Z && git push origin kalinka-vX.Y.Z
make build-all-deb                       # -> debs/
gh release edit kalinka-vX.Y.Z --notes-file notes.md   # what changed, above the generated notes

# Bump the SDK (minor/patch): edit one line, then rebuild
$EDITOR packages/kalinka-plugin-sdk/src/kalinka_plugin_sdk/_version.py   # __version__
make build-all-deb

# Cut an appliance image release
git tag kalinka-image-vX.Y.Z && git push origin kalinka-image-vX.Y.Z
```

## Rules of thumb

- **One tag per app release** (`kalinka-v*`). Never tag individual packages —
  the renderer is the one exception, with its own `kalinka-renderer-v*` train.
- **SemVer picks the digit, the calendar picks the date** — see [When to release](#when-to-release).
- **Clean tree on the tagged commit**, or the version carries a dev/dirty suffix.
- **SDK = one constant.** Minor/patch touches only `_version.py`; major also
  widens the four `>=1,<2` consumer pins.
- Compatibility is guaranteed **within a major version only** — that's what the
  `>=N,<N+1` pins encode.
