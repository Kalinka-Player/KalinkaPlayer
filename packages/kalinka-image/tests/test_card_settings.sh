#!/usr/bin/env bash
#
# What the first start reads of Raspberry Pi Imager's files, what it applies
# and what it leaves on the card: card_settings_test.py, on fake roots.
set -uo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 "$TESTS_DIR/card_settings_test.py"
