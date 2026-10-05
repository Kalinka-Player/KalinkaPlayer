#!/bin/sh
# Starts the demo server with the Jamendo client id taken from JAMENDO_CLIENT_ID.
set -eu

CONFIG=/etc/kalinka/kalinka_conf.cfg
TEMPLATE=/opt/kalinka-demo/kalinka_conf.cfg

if [ -z "${JAMENDO_CLIENT_ID:-}" ]; then
  echo "kalinka-demo: set JAMENDO_CLIENT_ID" >&2
  exit 1
fi
export JAMENDO_CLIENT_ID

umask 077
python3 - "$TEMPLATE" "$CONFIG" <<'PY'
import json
import os
import sys

with open(sys.argv[1]) as template:
    config = json.load(template)
config["input_modules.jamendo.client_id"] = os.environ["JAMENDO_CLIENT_ID"]
with open(sys.argv[2], "w") as out:
    json.dump(config, out, indent=2)
PY
unset JAMENDO_CLIENT_ID

exec kalinka-server --config "$CONFIG" --state /var/lib/kalinka/kalinka_state.json
