#!/bin/bash

set +e

CURRENT_HOSTNAME=`cat /etc/hostname | tr -d " \t\n\r"`
if [ -f /usr/lib/raspberrypi-sys-mods/imager_custom ]; then
   /usr/lib/raspberrypi-sys-mods/imager_custom set_hostname livingroom
else
   echo livingroom >/etc/hostname
   sed -i "s/127.0.1.1.*$CURRENT_HOSTNAME/127.0.1.1\tlivingroom/g" /etc/hosts
fi
FIRSTUSER=`getent passwd 1000 | cut -d: -f1`
FIRSTUSERHOME=`getent passwd 1000 | cut -d: -f6`
if [ -f /usr/lib/raspberrypi-sys-mods/imager_custom ]; then
   /usr/lib/raspberrypi-sys-mods/imager_custom enable_ssh -k 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMTAXUCow+vA6wxO3P9dTplkpfu14khgcRqSqhZmi/vX listener@laptop' 'ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAgFjZ6rLZ6SgF8jairlWgsyzkIY3gPcRTdoKDgxVZEx8Q= listener@phone'
else
   install -o "$FIRSTUSER" -m 700 -d "$FIRSTUSERHOME/.ssh"
   install -o "$FIRSTUSER" -m 600 <(printf "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMTAXUCow+vA6wxO3P9dTplkpfu14khgcRqSqhZmi/vX listener@laptop\necdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAgFjZ6rLZ6SgF8jairlWgsyzkIY3gPcRTdoKDgxVZEx8Q= listener@phone") "$FIRSTUSERHOME/.ssh/authorized_keys"
   echo 'PasswordAuthentication no' >>/etc/ssh/sshd_config
   systemctl enable ssh
fi
if [ -f /usr/lib/userconf-pi/userconf ]; then
   /usr/lib/userconf-pi/userconf 'listener' '$5$fixturesalt$J0a1n2u3a4r5y6f7i8x9t0u1r2e3h4a5s6h7f8i9x0A1B'
else
   echo "$FIRSTUSER:"'$5$fixturesalt$J0a1n2u3a4r5y6f7i8x9t0u1r2e3h4a5s6h7f8i9x0A1B' | chpasswd -e
   if [ "$FIRSTUSER" != "listener" ]; then
      usermod -l "listener" "$FIRSTUSER"
      usermod -m -d "/home/listener" "listener"
      groupmod -n "listener" "$FIRSTUSER"
      if grep -q "^autologin-user=" /etc/lightdm/lightdm.conf ; then
         sed /etc/lightdm/lightdm.conf -i -e "s/^autologin-user=.*/autologin-user=listener/"
      fi
      if [ -f /etc/systemd/system/getty@tty1.service.d/autologin.conf ]; then
         sed /etc/systemd/system/getty@tty1.service.d/autologin.conf -i -e "s/$FIRSTUSER/listener/"
      fi
      if [ -f /etc/sudoers.d/010_pi-nopasswd ]; then
         sed -i "s/^$FIRSTUSER /listener /" /etc/sudoers.d/010_pi-nopasswd
      fi
   fi
fi
if [ -f /usr/lib/raspberrypi-sys-mods/imager_custom ]; then
   /usr/lib/raspberrypi-sys-mods/imager_custom set_wlan 'Home Network' '71b81377d300cb6b621dcf5fcef399c47b73d1bf524901aff20f0c3a8c87223a' 'GB'
else
cat >/etc/wpa_supplicant/wpa_supplicant.conf <<'WPAEOF'
country=GB
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
ap_scan=1

update_config=1
network={
	ssid="Home Network"
	psk=71b81377d300cb6b621dcf5fcef399c47b73d1bf524901aff20f0c3a8c87223a
}

WPAEOF
   chmod 600 /etc/wpa_supplicant/wpa_supplicant.conf
   rfkill unblock wifi
   for filename in /var/lib/systemd/rfkill/*:wlan ; do
       echo 0 > $filename
   done
fi
if [ -f /usr/lib/raspberrypi-sys-mods/imager_custom ]; then
   /usr/lib/raspberrypi-sys-mods/imager_custom set_keymap 'gb'
   /usr/lib/raspberrypi-sys-mods/imager_custom set_timezone 'Europe/London'
else
   rm -f /etc/localtime
   echo "Europe/London" >/etc/timezone
   dpkg-reconfigure -f noninteractive tzdata
cat >/etc/default/keyboard <<'KBEOF'
XKBMODEL="pc105"
XKBLAYOUT="gb"
XKBVARIANT=""
XKBOPTIONS=""

KBEOF
   dpkg-reconfigure -f noninteractive keyboard-configuration
fi
rm -f /boot/firstrun.sh
sed -i 's| systemd.run.*||g' /boot/cmdline.txt
exit 0
