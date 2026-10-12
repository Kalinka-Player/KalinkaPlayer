# shellcheck shell=bash
# The Pi's config.txt, where a section header limits what follows it to some boards.

# Appends a line where every board reads it: after the last section, if that was for some boards only.
boot_config_append() {
  local config="$1" line="$2" last
  last="$(grep -E '^[[:blank:]]*\[' "$config" | tail -n1 | tr -d '[:blank:]\r' || true)"
  if [ -s "$config" ] && [ -n "$(tail -c1 "$config")" ]; then
    printf '\n' >> "$config"
  fi
  if [ -n "$last" ] && [ "$last" != '[all]' ]; then
    printf '[all]\n' >> "$config"
  fi
  printf '%s\n' "$line" >> "$config"
}
