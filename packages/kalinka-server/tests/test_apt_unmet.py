"""apt's report of what it could not satisfy, read into the words the app
shows, and nothing read from any other failure."""

import pytest

from kalinka_server.plugin_management.apt_unmet import (
    Unmet,
    candidate,
    needs,
    unmet_dependencies,
)

PREAMBLE = """\
Reading package lists...
Building dependency tree...
Reading state information...
Note, selecting '{0}' instead of '/var/tmp/kalinka-plugin.3k9q2x1a/{0}_1.0.0_all.deb'
Some packages could not be installed. This may mean that you have
requested an impossible situation or if you are using the unstable
distribution that some required packages have not yet been created
or been moved out of Incoming.
The following information may help to resolve the situation:

"""
# apt 2.7 on Ubuntu 24.04 with Ubuntu's bluez installed and its older
# bluez-alsa-utils available.
UBUNTU_2404 = PREAMBLE.format("kalinka-plugin-bluetooth") + """\
The following packages have unmet dependencies:
 kalinka-plugin-bluetooth : Depends: bluez (>= 5.79) but 5.72-0ubuntu5.4 is to be installed
                            Depends: bluez-alsa-utils (>= 4.3.1) but it is not going to be installed
                            Depends: bluez-obexd (>= 5.79) but 5.72-0ubuntu5.4 is to be installed
E: Unable to correct problems, you have held broken packages.
"""
# apt 3.0 on Debian 13 adds what its new solver found to the same report.
DEBIAN_13 = PREAMBLE.format("kalinka-plugin-demo") + """\
Unsatisfied dependencies:
 kalinka-plugin-demo : Depends: kalinka-plugin-sdk (>= 3.30) but 3.21.0 is to be installed
                       Depends: libdemo7 but it is not installable
Error: Unable to correct problems, you have held broken packages.
Error: The following information from --solver 3.0 may provide additional context:
   Unable to satisfy dependencies. Reached two conflicting decisions:
   1. kalinka-plugin-demo:amd64=1.0.0 is selected for install
   2. kalinka-plugin-demo:amd64 Depends libdemo7
      but none of the choices are installable:
      [no choices]
"""


def report(*lines):
    return PREAMBLE.format("kalinka-plugin-demo") + "\n".join(
        [
            "The following packages have unmet dependencies:",
            *lines,
            "E: Unable to correct problems, you have held broken packages.",
            "",
        ]
    )


def test_reads_every_version_floor_ubuntu_2404_refuses():
    assert unmet_dependencies(UBUNTU_2404, "kalinka-plugin-bluetooth") == [
        Unmet("bluez", ">=", "5.79", "5.72-0ubuntu5.4", True),
        Unmet("bluez-alsa-utils", ">=", "4.3.1", None, True),
        Unmet("bluez-obexd", ">=", "5.79", "5.72-0ubuntu5.4", True),
    ]


def test_reads_debian_13_past_its_solver_context():
    assert unmet_dependencies(DEBIAN_13, "kalinka-plugin-demo") == [
        Unmet("kalinka-plugin-sdk", ">=", "3.30", "3.21.0", True),
        Unmet("libdemo7", None, None, None, False),
    ]


def test_reads_a_pre_dependency_and_an_architecture_qualified_name():
    output = report(
        " kalinka-plugin-demo:amd64 : PreDepends: libc6:arm64 (= 1:2.41-6) but it is not installable"
    )
    assert unmet_dependencies(output, "kalinka-plugin-demo") == [
        Unmet("libc6:arm64", "=", "1:2.41-6", None, False)
    ]


def test_reads_only_the_plugins_own_lines():
    output = report(
        " libfoo2 : Depends: libbar (>= 2) but 1.0-1 is to be installed",
        "           Depends: libbaz but it is not installable",
        " kalinka-plugin-demo : Depends: libdemo7 but it is not installable",
    )
    assert unmet_dependencies(output, "kalinka-plugin-demo") == [
        Unmet("libdemo7", None, None, None, False)
    ]


@pytest.mark.parametrize(
    "output",
    [
        pytest.param(
            report(" libfoo2 : Depends: libbar (>= 2) but 1.0-1 is to be installed"),
            id="another package broken",
        ),
        pytest.param(
            report(
                " kalinka-plugin-demo : Depends: pipewire-alsa but it is not going to be installed or",
                "                                pulseaudio but it is not installable",
            ),
            id="alternatives",
        ),
        pytest.param(
            report(
                " kalinka-plugin-demo : Depends: libdemo7 but it is not installable",
                "                       Depends: mail-transport-agent",
            ),
            id="one line not read",
        ),
        pytest.param(
            report(
                " kalinka-plugin-demo : Breaks: kalinka-server (<< 6) but 5.3.0 is to be installed"
            ),
            id="conflict",
        ),
        pytest.param(
            report(
                " kalinka-plugin-demo : Depends: libdemo7 but 7.0-1 is to be installed"
            ),
            id="unversioned yet to be installed",
        ),
        pytest.param(
            "Reading package lists...\n"
            "E: dpkg was interrupted, you must manually run 'dpkg --configure -a' to correct the problem.\n",
            id="dpkg interrupted",
        ),
        pytest.param(
            "Reading package lists...\n"
            "E: Invalid archive signature\n"
            "E: Could not read meta data from /var/tmp/kalinka-plugin.3k9q2x1a/kalinka-plugin-demo_1.0.0_all.deb\n"
            "E: The package lists or status file could not be parsed or opened.\n",
            id="unreadable package",
        ),
        pytest.param("", id="nothing said"),
    ],
)
def test_reads_nothing_from_another_failure(output):
    assert unmet_dependencies(output, "kalinka-plugin-demo") == []


def test_policy_names_the_candidate_and_nothing_else():
    policy = (
        "bluez-alsa-utils:\n"
        "  Installed: (none)\n"
        "  Candidate: 4.1.1-2\n"
        "  Version table:\n"
        "     4.1.1-2 500\n"
        "        500 https://kalinka:s3cret@mirror.example.org/ubuntu noble/universe amd64 Packages\n"
    )
    assert candidate(policy) == "4.1.1-2"
    assert candidate("libdemo7:\n  Installed: (none)\n  Candidate: (none)\n") is None
    assert candidate("") is None


def test_names_each_dependency_with_what_this_system_has_or_offers():
    offered = {"bluez-alsa-utils": "4.1.1-2"}
    message = needs(
        unmet_dependencies(UBUNTU_2404, "kalinka-plugin-bluetooth"), offered.get
    )
    assert message == (
        "Needs bluez 5.79 or newer (this system has 5.72-0ubuntu5.4), "
        "bluez-alsa-utils 4.3.1 or newer (this system offers 4.1.1-2, which apt "
        "will not install) and bluez-obexd 5.79 or newer (this system has "
        "5.72-0ubuntu5.4)."
    )


def test_names_a_package_no_source_offers():
    message = needs(unmet_dependencies(DEBIAN_13, "kalinka-plugin-demo"), {}.get)
    assert message == (
        "Needs kalinka-plugin-sdk 3.30 or newer (this system has 3.21.0) and "
        "libdemo7 (no package source here offers it)."
    )


@pytest.mark.parametrize(
    "relation, required",
    [
        (">=", "libdemo 2 or newer"),
        (">>", "libdemo newer than 2"),
        ("<=", "libdemo 2 or older"),
        ("<<", "libdemo older than 2"),
        ("=", "libdemo 2 exactly"),
    ],
)
def test_words_each_relation(relation, required):
    unmet = [Unmet("libdemo", relation, "2", "3", True)]
    assert needs(unmet, {}.get) == f"Needs {required} (this system has 3)."


def test_says_apt_will_not_install_what_it_cannot_look_up():
    unmet = [Unmet("libdemo", None, None, None, True)]
    assert needs(unmet, {}.get) == "Needs libdemo (apt will not install it)."
