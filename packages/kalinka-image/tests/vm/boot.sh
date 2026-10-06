#!/usr/bin/env bash
#
# Boot a copy of a PC image in QEMU and pass once the server answers on its
# default port. The guest is then powered off through its power button, so the
# disk left behind is one a clean shutdown wrote.
#
# Usage: boot.sh <image> <uefi|uefi-secure-boot|bios> <dir>
#
# <dir> receives disk.img, the copy that booted, with serial.log and qemu.log.
# When the server never answers it also gets screen.png: past the firmware and
# GRUB, the kernel writes only to the screen.
#
# Env:
#   OVMF_CODE, OVMF_VARS  firmware and matching variable store
#                         (default: Ubuntu plain UEFI)
#   BOOT_TIMEOUT          seconds the server has to answer (default: 600)
#   HOST_PORT             host port forwarded to the server's (default: 18000)
set -euo pipefail

OVMF_CODE="${OVMF_CODE:-/usr/share/OVMF/OVMF_CODE_4M.fd}"
OVMF_VARS="${OVMF_VARS:-/usr/share/OVMF/OVMF_VARS_4M.fd}"
BOOT_TIMEOUT="${BOOT_TIMEOUT:-600}"
HOST_PORT="${HOST_PORT:-18000}"
SERVER_PORT=8000
SHUTDOWN_TIMEOUT=120

die() { echo "boot: $*" >&2; exit 1; }

[ $# -eq 3 ] || die "usage: boot.sh <image> <uefi|uefi-secure-boot|bios> <dir>"
IMAGE="$1" FIRMWARE="$2" DIR="$3"
[ -r "$IMAGE" ] || die "cannot read $IMAGE"

RUNTIME="$(mktemp -d)"
QEMU_PID=""
cleanup() {
  # Waited for, so nothing still writes the disk when inspect.sh mounts it.
  if [ -n "$QEMU_PID" ] && kill "$QEMU_PID" 2>/dev/null; then
    wait "$QEMU_PID" 2>/dev/null || true
  fi
  rm -rf "$RUNTIME"
}
trap cleanup EXIT

case "$FIRMWARE" in
  uefi-secure-boot)
    cp "$OVMF_VARS" "$RUNTIME/vars.fd"
    FIRMWARE_ARGS=(
      -machine q35,smm=on
      -global driver=cfi.pflash01,property=secure,value=on
      -drive "if=pflash,format=raw,unit=0,readonly=on,file=$OVMF_CODE"
      -drive "if=pflash,format=raw,unit=1,file=$RUNTIME/vars.fd"
    ) ;;
  uefi)
    cp "$OVMF_VARS" "$RUNTIME/vars.fd"
    FIRMWARE_ARGS=(
      -machine q35
      -drive "if=pflash,format=raw,unit=0,readonly=on,file=$OVMF_CODE"
      -drive "if=pflash,format=raw,unit=1,file=$RUNTIME/vars.fd"
    ) ;;
  bios)
    FIRMWARE_ARGS=(-machine q35) ;;
  *) die "unknown firmware '$FIRMWARE'" ;;
esac

# One QMP command, waited for; the greeting and any events are read past.
qmp() {
  python3 - "$RUNTIME/qmp.sock" "$@" <<'PY'
import json, socket, sys

path, command, arguments = sys.argv[1], sys.argv[2], sys.argv[3:]
channel = socket.socket(socket.AF_UNIX)
channel.connect(path)
stream = channel.makefile("rw")

def execute(command, arguments):
    stream.write(json.dumps({"execute": command, "arguments": arguments}) + "\n")
    stream.flush()
    for line in stream:
        reply = json.loads(line)
        if "error" in reply:
            sys.exit(f"{command}: {reply['error']['desc']}")
        if "return" in reply:
            return
    sys.exit(f"{command}: QEMU closed the connection")

stream.readline()
execute("qmp_capabilities", {})
execute(command, json.loads(arguments[0]) if arguments else {})
PY
}

qemu_running() { kill -0 "$QEMU_PID" 2>/dev/null; }

power_off() {
  qmp system_powerdown || return
  local deadline=$((SECONDS + SHUTDOWN_TIMEOUT))
  while qemu_running; do
    [ "$SECONDS" -lt "$deadline" ] || return 1
    sleep 1
  done
}

mkdir -p "$DIR"
rm -f "$DIR/serial.log" "$DIR/qemu.log" "$DIR/screen.png"
cp --sparse=always "$IMAGE" "$DIR/disk.img"

echo "boot: starting $FIRMWARE"
# -cpu host: the CPU of a real PC, not the minimal x86-64 QEMU would model.
qemu-system-x86_64 \
  "${FIRMWARE_ARGS[@]}" \
  -enable-kvm -cpu host -smp 2 -m 2048 -nographic \
  -drive "file=$DIR/disk.img,format=raw,if=virtio" \
  -nic "user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:$HOST_PORT-:$SERVER_PORT" \
  -serial "file:$DIR/serial.log" -monitor none \
  -qmp "unix:$RUNTIME/qmp.sock,server=on,wait=off" \
  > "$DIR/qemu.log" 2>&1 < /dev/null &
QEMU_PID=$!

started=$SECONDS
version=""
while qemu_running && [ $((SECONDS - started)) -lt "$BOOT_TIMEOUT" ]; do
  version="$(curl -fsS --max-time 5 "http://127.0.0.1:$HOST_PORT/server/version" 2>/dev/null)" && break
  version=""
  sleep 5
done

if [ -z "$version" ]; then
  if ! qemu_running; then
    cat "$DIR/qemu.log" >&2
    die "QEMU stopped before the server answered"
  fi
  qmp screendump "{\"filename\": \"$DIR/screen.png\", \"format\": \"png\"}" \
    || echo "boot: no screenshot" >&2
  # Still powered off cleanly: the journal it flushes is what says why.
  power_off || echo "boot: the guest did not power off either" >&2
  die "the server did not answer within ${BOOT_TIMEOUT}s"
fi
echo "boot: the server answered after $((SECONDS - started))s: $version"

power_off || die "the guest did not power off within ${SHUTDOWN_TIMEOUT}s"
wait "$QEMU_PID" || die "QEMU exited with status $?"
QEMU_PID=""
echo "boot: powered off"
