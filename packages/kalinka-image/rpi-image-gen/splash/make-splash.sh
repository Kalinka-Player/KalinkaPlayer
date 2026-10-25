#!/bin/bash
# Draws kalinka.tga, the Pi kernel's fullscreen_logo, from the Kalinka logo; needs rsvg-convert and ImageMagick 7.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOGO="$HERE/../../../../docs/images/kalinka_logo.svg"
# The app's page background, so the splash hands over to the now-playing display without a flash.
BACKGROUND='#080808'
# The kernel crops rather than scales, so the logo stays narrower than the narrowest official Pi screen, 720 px.
LOGO_WIDTH=680

rsvg-convert --width "$LOGO_WIDTH" "$LOGO" \
  | magick -size 1920x1080 "xc:$BACKGROUND" - -gravity center -composite \
      +dither -colors 224 -alpha off -type TrueColor -compress none "$HERE/kalinka.tga"
