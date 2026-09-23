# Built in-place from the source tree (no tarball):
#   rpmbuild -bb --build-in-place --define "server_version X.Y.Z" \
#     rpm/kalinka-server.spec
# See scripts/build_rpm.sh, which stages the wheels + manifests first.
#
# One noarch RPM carries the whole app bundle — server, SDK and first-party
# plugin wheels — unlike the deb split; Fedora boxes get everything from a
# single package. Runtime is identical to the deb: bootstrap.sh creates a
# venv under /opt/kalinka and installs the shipped wheels.
#
# The deb's kalinka-upgrade.* units are NOT shipped: the in-place upgrade
# path fetches the apt-based installer, which cannot work here. Upgrades
# happen by installing a newer RPM.

Name:           kalinka-server
Version:        %{server_version}
Release:        1%{?dist}
Summary:        Kalinka Music Player (server)
License:        GPL-3.0-or-later
URL:            https://github.com/madenvel/KalinkaPlayer
BuildArch:      noarch

BuildRequires:  systemd-rpm-macros
Requires:       python3 >= 3.11
Requires:       python3-pip
%{?systemd_requires}

%description
Kalinka Music Player service used in a pair with the Kalinka App. Pure
Python; audio output is played by the separately-packaged kalinka-renderer.

%install
install -d %{buildroot}/opt/kalinka/wheels
install -m 644 build-rpm-stage/wheels/*.whl %{buildroot}/opt/kalinka/wheels/
install -d %{buildroot}/opt/kalinka/allowed_packages
install -m 644 build-rpm-stage/allowed_packages/*.json %{buildroot}/opt/kalinka/allowed_packages/
install -m 755 scripts/bootstrap.sh %{buildroot}/opt/kalinka/bootstrap.sh
install -m 644 ../../README.md %{buildroot}/opt/kalinka/README.md
install -m 644 LICENSE %{buildroot}/opt/kalinka/LICENSE
install -D -m 644 scripts/kalinka.service %{buildroot}%{_unitdir}/kalinka.service
install -D -m 644 scripts/kalinka-restart.path %{buildroot}%{_unitdir}/kalinka-restart.path
install -D -m 644 scripts/kalinka-restart.service %{buildroot}%{_unitdir}/kalinka-restart.service
install -D -m 644 scripts/kalinka-journal-reader.socket %{buildroot}%{_unitdir}/kalinka-journal-reader.socket
install -D -m 644 scripts/kalinka-journal-reader@.service %{buildroot}%{_unitdir}/kalinka-journal-reader@.service
install -D -m 644 scripts/kalinka.tmpfiles.conf %{buildroot}%{_tmpfilesdir}/kalinka.conf
install -D -m 644 rpm/kalinka-server.sysusers %{buildroot}%{_sysusersdir}/kalinka-server.conf

%pre
%sysusers_create_compat rpm/kalinka-server.sysusers

%post
%systemd_post kalinka.service
%tmpfiles_create kalinka.conf
# Mirror the deb postinst: config dir locked to the service user, a world-
# writable music drop-off (sticky, setgid — single-admin appliance), units
# enabled and the server (re)started so an install is immediately live.
# Mirrors the deb: the server no longer opens sound devices, so it gives up
# the audio group older versions granted it. Held back while a renderer old
# enough to still run as kalusr is installed, since that one would lose its
# sound card with it.
if id -nG kalusr 2>/dev/null | tr ' ' '\n' | grep -qx audio; then
    if [ -f /usr/lib/systemd/system/kalinka-renderer.service ] && \
       grep -q '^User=kalusr' /usr/lib/systemd/system/kalinka-renderer.service; then
        :
    else
        gpasswd -d kalusr audio >/dev/null 2>&1 || :
    fi
fi
install -d -m 0750 -o kalusr -g kalusr /etc/kalinka
[ -d /srv/kalinka ] || install -d -m 0755 /srv/kalinka
if [ ! -d /srv/kalinka/music ]; then
    install -d -m 3777 -o root -g kalusr /srv/kalinka/music
fi
systemctl enable --now kalinka-restart.path >/dev/null 2>&1 || :
systemctl enable --now kalinka-journal-reader.socket >/dev/null 2>&1 || :
systemctl enable kalinka.service >/dev/null 2>&1 || :
systemctl restart kalinka.service >/dev/null 2>&1 || :

%preun
%systemd_preun kalinka.service kalinka-restart.path kalinka-journal-reader.socket

%postun
%systemd_postun_with_restart kalinka.service

%files
/opt/kalinka/wheels/
/opt/kalinka/allowed_packages/
/opt/kalinka/bootstrap.sh
/opt/kalinka/README.md
%license /opt/kalinka/LICENSE
%{_unitdir}/kalinka.service
%{_unitdir}/kalinka-restart.path
%{_unitdir}/kalinka-restart.service
%{_unitdir}/kalinka-journal-reader.socket
%{_unitdir}/kalinka-journal-reader@.service
%{_tmpfilesdir}/kalinka.conf
%{_sysusersdir}/kalinka-server.conf

%changelog
* Tue Aug 04 2026 Dmitry Savin <envelsavinds@gmail.com> - 0.0.0-1
- Initial package
