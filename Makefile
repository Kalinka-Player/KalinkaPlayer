## KalinkaPlayer Development Makefile

.PHONY: clean test system-test help venv-env kalinka-server-deb kalinka-server-rpm kalinka-plugins-deb build-all-deb copy-debs build-env dev-setup dev-run renderer-build renderer-clean renderer-deb renderer-rpm proto image-rpi4 image-amd64 image-test

## --- Local-from-source dev environment (no root, no systemd) ------------------
## Everything lands in a per-user fakeroot under $(KALINKA_PREFIX) instead of the
## system FHS paths. Override the venv or prefix on the command line, e.g.
##   make dev-run KALINKA_PREFIX=$$HOME/kalinka VENV=.venv
##
## If a venv is already active ($VIRTUAL_ENV set) it is reused as-is — no second
## venv is created. Otherwise we fall back to ./.venv (created on first setup).
VENV ?= $(if $(VIRTUAL_ENV),$(VIRTUAL_ENV),.venv)
KALINKA_PREFIX ?= $(HOME)/kalinka
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
## Interpreter used to create a fresh venv. Requires Python >= 3.11; the
## production image (Raspberry Pi) runs 3.13. Override to pick a specific
## interpreter, e.g. PYTHON=/opt/python3.13/bin/python3.
PYTHON ?= python3
## Prepended to PATH when running the deb build scripts, so their bare
## `python3` / `pip` resolve to the venv instead of the system interpreter
## (whose pip refuses installs on PEP 668 "externally managed" distros).
VENV_BIN := $(abspath $(VENV))/bin

## Every editable package in the workspace, SDK first. dev-setup installs them
## in a single pip run so the resolver sees one consistent set: installing the
## SDK alone leaves the previously-installed server and plugins pinned to the
## SDK major they were built against, and pip reports that as a conflict.
DEV_PACKAGES := packages/kalinka-plugin-sdk packages/kalinka-server \
	$(filter-out packages/kalinka-plugin-sdk,$(wildcard packages/kalinka-plugin-*))

## Create the venv (if missing) and give it the wheel-building toolchain (pip,
## build, setuptools-scm). Shared prerequisite of build-env and dev-setup,
## which differ only in what they install into it.
venv-env:
	@if [ -n "$(VIRTUAL_ENV)" ]; then \
		echo "Reusing active venv: $(VIRTUAL_ENV)"; \
	elif [ -d $(VENV) ]; then \
		echo "Reusing existing venv: $(VENV)"; \
	else \
		command -v $(PYTHON) >/dev/null 2>&1 || { \
			echo "ERROR: '$(PYTHON)' not found on PATH."; \
			echo "  Install Python >= 3.11 (e.g. 'sudo apt install python3 python3-venv')"; \
			echo "  or point PYTHON at it: make build-env PYTHON=/path/to/python3.13"; \
			exit 1; }; \
		echo "Creating venv at $(VENV) with $$($(PYTHON) --version)"; $(PYTHON) -m venv $(VENV); \
	fi
	@ver=$$($(PY) -c 'import sys; print("%d.%d" % sys.version_info[:2])'); \
	if $(PY) -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 11) else 1)'; then :; else \
		echo "ERROR: venv Python is $$ver, but >= 3.11 is required."; \
		echo "  Recreate it: rm -rf $(VENV) && make build-env PYTHON=python3.13"; \
		exit 1; \
	fi
	@echo "Using $$($(PY) --version)"
	@$(PIP) install --upgrade --quiet pip build setuptools-scm

## venv-env plus the SDK, which the plugin deb builds need importable: their
## manifest-export step imports kalinka_plugin_sdk (pulling pydantic/pyyaml).
## Installing it editable lets pip track those deps — no hardcoded list to
## drift.
build-env: venv-env
	@echo "Installing kalinka-plugin-sdk (editable)..."
	@$(PIP) install --quiet -e packages/kalinka-plugin-sdk

## One-shot setup: create venv (unless one is active/exists), install sdk +
## server + all plugins (editable), and seed the fakeroot directory tree +
## config. The server is pure Python — audio output lives in the renderer
## (make renderer-build).
dev-setup: venv-env
	@echo "Installing sdk, server and plugins (editable)..."
	@$(PIP) install $(addprefix -e ,$(DEV_PACKAGES))
	@echo "Verifying the server imports (catches missing runtime deps)..."
	@$(PY) -c "import kalinka_server.__main__" || { \
		echo "ERROR: kalinka_server failed to import after install — likely an"; \
		echo "  undeclared runtime dependency. See the traceback above."; \
		exit 1; }
	@echo "Seeding fakeroot under $(KALINKA_PREFIX)..."
	@mkdir -p \
		$(KALINKA_PREFIX)/etc/kalinka \
		$(KALINKA_PREFIX)/var/lib/kalinka \
		$(KALINKA_PREFIX)/srv/kalinka/music \
		$(KALINKA_PREFIX)/var/log/kalinka \
		$(KALINKA_PREFIX)/run/kalinka \
		$(KALINKA_PREFIX)/var/cache/kalinka/numba \
		$(KALINKA_PREFIX)/var/cache/kalinka/artwork
	@test -f $(KALINKA_PREFIX)/etc/kalinka/kalinka_conf.cfg \
		|| cp kalinka_conf.cfg $(KALINKA_PREFIX)/etc/kalinka/kalinka_conf.cfg
	@echo ""
	@echo "Dev environment ready. Start the server with:  make dev-run"
	@echo "  config: $(KALINKA_PREFIX)/etc/kalinka/kalinka_conf.cfg"
	@echo "  logs:   $(KALINKA_PREFIX)/var/log/kalinka/server.log"

## Run the server in the foreground against the fakeroot (Ctrl-C to stop).
## Forward args via ARGS, e.g.  make dev-run ARGS=--debug
##
## Logs use the full timestamped format here; KALINKA_LOG_FORMAT=journal shows
## the terser one systemd gets instead (dev-run pipes stdout through tee, so
## that format is never auto-detected).
dev-run:
	@KALINKA_PREFIX=$(KALINKA_PREFIX) VENV=$(abspath $(VENV)) scripts/dev_run.sh $(ARGS)

## -----------------------------------------------------------------------------

## --- Native renderer (packages/kalinka-renderer) ------------------------------
## C++ protobuf sources are generated at build time by CMake; the Python
## bindings for the server are committed (make proto regenerates them).
RENDERER_DIR := packages/kalinka-renderer
RENDERER_BUILD := $(RENDERER_DIR)/build

renderer-build:
	@cmake -S $(RENDERER_DIR) -B $(RENDERER_BUILD)
	@cmake --build $(RENDERER_BUILD) -j

renderer-clean:
	@rm -rf $(RENDERER_BUILD) $(RENDERER_DIR)/build-release $(RENDERER_DIR)/build-rpm

## Platform-specific renderer packages: stripped Release binary + systemd
## unit. Build on (a container of) the distro you target.
renderer-deb:
	@cd $(RENDERER_DIR) && ./scripts/build_deb.sh

renderer-rpm:
	@cd $(RENDERER_DIR) && ./scripts/build_rpm.sh

## Regenerate the committed Python protobuf bindings (needs grpcio-tools in
## the venv: pip install grpcio-tools).
proto:
	@$(PY) -m grpc_tools.protoc \
		-I $(RENDERER_DIR)/proto/kalinka/renderer/v1 \
		--python_out=packages/kalinka-server/src/kalinka_server/renderer_proto \
		$(RENDERER_DIR)/proto/kalinka/renderer/v1/renderer.proto
	@echo "Regenerated kalinka_server/renderer_proto/renderer_pb2.py"
## -----------------------------------------------------------------------------

## Clean build artifacts
clean:
	@echo "Cleaning build artifacts..."
	@find . -name "*.pyc" -delete
	@find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
	@find . -name "*.egg-info" -type d -exec rm -rf {} + 2>/dev/null || true

## Run tests
test:
	@echo "Running tests..."
	@cd packages/kalinka-plugin-sdk && python -m pytest tests/ -v
	@cd packages/kalinka-server && python -m pytest tests/ -v

## Full-stack system test (tests/system): fakeroot + Samba in podman + real
## indexing, enrichment and AI search over downloaded recordings. Opt-in
## because it is slow and needs podman and the network. Extra pytest args via
## ARGS, e.g. make system-test ARGS=-x
system-test:
	@KALINKA_SYSTEM_TEST=1 $(PY) -m pytest tests/system -o log_cli=true --log-cli-level=INFO $(ARGS)

## Catch names that do not exist before a user does
lint:
	@echo "Checking for undefined names..."
	@python -m flake8 --select=F821,F822 packages/*/src

## Helper function to move debs to debs directory
copy-debs:
	@rm -rf debs
	@mkdir -p debs
	@for dir in packages/*/; do \
		for deb in "$$dir"/*.deb; do \
			if [ -f "$$deb" ]; then \
				echo "Moving $$(basename $$deb) to debs/"; \
				mv "$$deb" debs/; \
			fi; \
		done; \
	done

## Build kalinka-server deb package. Runs with the venv's bin first on PATH
## so the script's bare `python3` builds the wheel from the venv.
kalinka-server-deb: build-env
	@echo "Building kalinka-server deb package..."
	@cd packages/kalinka-server && PATH="$(VENV_BIN):$$PATH" ./scripts/build_deb.sh

## Noarch RPM carrying the whole app bundle (server + SDK + plugin wheels).
kalinka-server-rpm: build-env
	@echo "Building kalinka-server rpm package..."
	@cd packages/kalinka-server && PATH="$(VENV_BIN):$$PATH" ./scripts/build_rpm.sh

## Build all plugin deb packages (SDK, local files, musiccast, dummydevice, jamendo)
kalinka-plugins-deb: build-env
	@echo "Building plugin deb packages..."
	@for dir in packages/kalinka-plugin-*; do \
		if [ -f "$$dir/scripts/build_deb.sh" ]; then \
			echo "Building $$(basename $$dir)..."; \
			(cd "$$dir" && PATH="$(VENV_BIN):$$PATH" ./scripts/build_deb.sh) || exit 1; \
		fi; \
	done

## Build all deb packages (server, plugins and renderer)
build-all-deb: kalinka-server-deb kalinka-plugins-deb renderer-deb copy-debs
	@echo "All deb packages built successfully!"
	@echo "Debs moved to debs/ directory"

## --- Appliance images (packages/kalinka-image) ---------------------------------
## Bootable Debian images with the whole player already installed. Both need
## root for loop devices and mounts, and building for another architecture
## needs qemu-user-static registered with binfmt_misc:
##   sudo make image-rpi4                 # latest published release
##   sudo make image-rpi4 KALINKA_VERSION=4.3.2
IMAGE_DIR := packages/kalinka-image
KALINKA_VERSION ?=

image-rpi4:
	@$(IMAGE_DIR)/build-image.sh rpi4 $(KALINKA_VERSION)

image-amd64:
	@$(IMAGE_DIR)/build-image.sh amd64 $(KALINKA_VERSION)

## The tests that edit /etc run in a throwaway Debian container, since that is
## the only way to let them near a real useradd and a real ssh-keygen.
image-test:
	@if command -v podman >/dev/null 2>&1 || command -v docker >/dev/null 2>&1; then \
		engine=$$(command -v podman || command -v docker); \
		echo "Running the image tests in debian:trixie via $$(basename $$engine)"; \
		$$engine run --rm -v "$(CURDIR)/$(IMAGE_DIR):/img:z" -w /img \
			-e KALINKA_IMAGE_TEST_DISPOSABLE=1 -e DEBIAN_FRONTEND=noninteractive \
			debian:trixie bash -c 'apt-get update -qq >/dev/null && \
				apt-get install -y -qq --no-install-recommends \
					openssh-client openssl passwd python3 util-linux fdisk >/dev/null && \
				bash tests/run-tests.sh'; \
	else \
		echo "No podman or docker: running only the tests that need neither root nor a container."; \
		$(IMAGE_DIR)/tests/run-tests.sh; \
	fi
## -----------------------------------------------------------------------------

## Show help
help:
	@echo "KalinkaPlayer Development Commands:"
	@echo ""
	@echo "  dev-setup         One-shot local setup: venv + editable installs + native build + ~/kalinka fakeroot"
	@echo "  dev-run           Run the server in the foreground against the fakeroot (Ctrl-C to stop)"
	@echo "  dev-rebuild-native  Rebuild the native C++ extension, then restart to load it"
	@echo "  system-test       Full-stack indexing test: fakeroot + Samba in podman + real enrichment/AI search"
	@echo ""
	@echo "  build-native      Build the native player C++ extension"
	@echo "  venv-env          Create the venv (if missing) with the wheel-build toolchain"
	@echo "  build-env         venv-env plus the editable SDK (what the deb builds need)"
	@echo "  kalinka-server-deb  Build kalinka-server deb package"
	@echo "  kalinka-plugins-deb Build all plugin deb packages (including SDK)"
	@echo "  build-all-deb     Build all deb packages (server, plugins, renderer) and move to debs/"
	@echo "  image-rpi4        Build the Raspberry Pi 4 appliance image (needs root)"
	@echo "  image-amd64       Build the x86-64 appliance image (needs root)"
	@echo "  image-test        Run the appliance image tests"
	@echo "  copy-debs         Move built deb packages to debs/ directory"
	@echo "  test              Run all tests"
	@echo "  lint              Check for undefined names (F821)"
	@echo "  clean             Clean build artifacts"
	@echo "  help              Show this help message"