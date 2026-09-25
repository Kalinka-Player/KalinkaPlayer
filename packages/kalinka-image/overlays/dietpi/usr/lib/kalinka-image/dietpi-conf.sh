# shellcheck shell=bash
# KEY=value files read as DietPi reads them: first match wins, #KEY= is not the key, keys are literal.

dietpi_conf_get() {
  local file="$1" key="$2"
  KEY="$key" awk '
    BEGIN { key = ENVIRON["KEY"] "=" }
    { line = $0; sub(/\r$/, "", line); sub(/^[ \t]*/, "", line) }
    index(line, key) == 1 { print substr(line, length(key) + 1); exit }
  ' "$file"
}

# Rewrites the file in place, so its owner and mode survive.
dietpi_conf_set() {
  local file="$1" key="$2" value="$3" updated
  updated="$(KEY="$key" VALUE="$value" awk '
    BEGIN { key = ENVIRON["KEY"] "="; done = 0 }
    !done {
      line = $0; sub(/^[ \t]*/, "", line)
      if (index(line, key) == 1) { print ENVIRON["KEY"] "=" ENVIRON["VALUE"]; done = 1; next }
    }
    { print }
    END { if (!done) print ENVIRON["KEY"] "=" ENVIRON["VALUE"] }
  ' "$file")" || return
  printf '%s\n' "$updated" > "$file"
}
