"""The notes every kalinka-image-v* release is published with.

They once carried their own flashing and first-boot steps, which drifted from
docs/installation.md: they told PC users to write to a FAT partition that the
guide explains is a hidden EFI one. The steps now live only in the guide, and
the notes link to it — so a link has to land on a heading the guide still has.
"""

import functools
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
NOTES = REPO / "scripts" / "image-release-notes.sh"
GUIDE = REPO / "docs" / "installation.md"
TAG = "kalinka-image-v1.2.3"
GUIDE_LINK = re.compile(r"\]\(([^)#]*/docs/installation\.md)#([^)]+)\)")


@functools.cache
def _notes() -> str:
    return subprocess.run(
        ["bash", str(NOTES), TAG], capture_output=True, text=True, check=True
    ).stdout


def _anchors(markdown: str) -> set[str]:
    """The anchors GitHub gives a document's headings."""
    prose = re.sub(r"^```.*?^```", "", markdown, flags=re.M | re.S)
    return {
        re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")
        for heading in re.findall(r"^#{1,6} (.+)$", prose, re.M)
    }


def test_the_notes_link_the_guide_as_it_stands_at_the_tag():
    links = GUIDE_LINK.findall(_notes())
    assert {url for url, _ in links} == {
        f"https://github.com/Kalinka-Player/KalinkaPlayer/blob/{TAG}"
        "/docs/installation.md"
    }
    assert [anchor for _, anchor in links] == [
        "install-the-server",
        "settings-on-the-card",
    ]


def test_every_anchor_the_notes_link_is_a_heading_of_the_guide():
    linked = {anchor for _, anchor in GUIDE_LINK.findall(_notes())}
    assert linked <= _anchors(GUIDE.read_text())


def test_the_notes_leave_writing_and_setting_up_an_image_to_the_guide():
    notes = _notes()
    assert "```" not in notes
    assert not re.search(r"^#", notes, re.M)
    for settings_file in ("dietpi.txt", "dietpi-wifi.txt", "kalinka-firstboot.conf"):
        assert settings_file not in notes


def test_the_notes_keep_what_only_the_release_page_can_say():
    notes = _notes()
    for image in ("-rpi234-arm64.img.xz", "-rpi5-arm64.img.xz", "-amd64.img.xz"):
        assert image in notes
    assert "sha256sum -c SHA256SUMS --ignore-missing" in notes
