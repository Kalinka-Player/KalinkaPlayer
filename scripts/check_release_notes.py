#!/usr/bin/env python3
"""Check the image release notes, and the docs, against the install guide.

The image release notes link docs/installation.md by heading rather than
repeat its steps, and nothing else would notice a broken link or a broken
heredoc before a release is published with it. It fails when:

* an ``installation.md#anchor`` link in the notes, in a release workflow, or
  in the Markdown at the top of the repository, in docs/ or at the top of a
  package, or a ``#anchor`` link in the guide itself, names no heading of
  the guide, as happens when a heading is renamed;
* bash, running the notes script for a sample tag, does not write the image
  table with a row for each target the image workflow builds and the
  checksum line at the start of a line, as happens when the heredoc is
  indented wrongly, or links the repository at any ref but that tag;
* the notes give a step the guide owns again: ``dietpi.txt``,
  ``kalinka-firstboot.conf`` or ``dd of=``.

Exit status 1 when any check fails.
"""

import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Iterator, List, Mapping, NamedTuple, Optional, Set

REPO = Path(__file__).resolve().parents[1]
GUIDE = "docs/installation.md"
IMAGE_NOTES = "scripts/image-release-notes.sh"
IMAGE_WORKFLOW = ".github/workflows/image-build.yml"
RELEASE_WORKFLOWS = (
    IMAGE_WORKFLOW,
    ".github/workflows/image-release.yml",
    ".github/workflows/release.yml",
    ".github/workflows/renderer-release.yml",
    ".github/workflows/supervisor-release.yml",
)
DOCS = ("*.md", "docs/*.md", "packages/*/*.md")

SAMPLE_TAG = "kalinka-image-v0.0.0"
GUIDE_STEPS = ("dietpi.txt", "kalinka-firstboot.conf", "dd of=")

_GUIDE_LINK_RE = re.compile(r"(?<![\w-])installation\.md#([\w-]+)")
_SELF_LINK_RE = re.compile(r"\]\(#([\w-]+)\)")
_REPO_REF_RE = re.compile(
    r"github\.com/Kalinka-Player/KalinkaPlayer/(?:blob|tree)/([^/\s)]+)/"
)
_HEADING_RE = re.compile(r"^#{1,6}\s+(.*?\S)(?:\s+#+)?\s*$")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_TAG_RE = re.compile(r"<[^>]+>")
_TARGET_RE = re.compile(r"^\s*-\s*target:\s*(\S+)\s*$", re.MULTILINE)


class Problem(NamedTuple):
    path: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


def slug(heading: str) -> str:
    """The anchor GitHub gives a heading, which it takes from the rendered text."""
    text = _TAG_RE.sub("", _LINK_RE.sub(r"\1", heading))
    return re.sub(r"[^\w\- ]", "", text.lower()).replace(" ", "-")


def heading_anchors(markdown: str) -> Set[str]:
    anchors = set()
    seen: Counter = Counter()
    fence: Optional[str] = None
    for line in markdown.splitlines():
        marker = _FENCE_RE.match(line)
        if fence is None and marker:
            fence = marker.group(1)
            continue
        if fence is not None:
            # Only a bare run of the opening character, at least as long, closes it.
            closes = marker and line.strip() == marker.group(1)
            if closes and marker.group(1).startswith(fence):
                fence = None
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            anchor = slug(heading.group(1))
            anchors.add(f"{anchor}-{seen[anchor]}" if seen[anchor] else anchor)
            seen[anchor] += 1
    return anchors


def broken_guide_links(guide: str, files: Mapping[str, str]) -> Iterator[Problem]:
    anchors = heading_anchors(guide)
    for path, text in files.items():
        patterns = [_GUIDE_LINK_RE]
        if path == GUIDE:
            patterns.append(_SELF_LINK_RE)
        for number, line in enumerate(text.splitlines(), 1):
            for anchor in (a for pattern in patterns for a in pattern.findall(line)):
                if anchor not in anchors:
                    message = f"`#{anchor}` names no heading in {GUIDE}"
                    yield Problem(path, number, message)


def render_notes(script: str, tag: str) -> subprocess.CompletedProcess:
    """Run the notes script for ``tag`` as the workflow does, in a scratch directory."""
    with tempfile.TemporaryDirectory() as scratch:
        # Bare, so no BASH_ENV is sourced and no locale warning passes for an error.
        return subprocess.run(
            ["bash", "-c", script, IMAGE_NOTES, tag],
            cwd=scratch,
            capture_output=True,
            encoding="utf-8",
            env={"PATH": os.environ.get("PATH", os.defpath), "LC_ALL": "C"},
        )


def image_notes_problems(script: str, workflow: str) -> Iterator[Problem]:
    lines = script.splitlines()
    line = next((n for n, text in enumerate(lines, 1) if "<<" in text), 1)

    def problem(message: str) -> Problem:
        return Problem(IMAGE_NOTES, line, message)

    rendered = render_notes(script, SAMPLE_TAG)
    if rendered.returncode or rendered.stderr:
        yield problem(f"bash could not write the notes: {rendered.stderr.strip()}")
        return
    notes = rendered.stdout

    if not re.search(r"^\| Image \|", notes, re.MULTILINE):
        yield problem("the notes have no table of images at the start of a line")
    targets = _TARGET_RE.findall(workflow)
    if not targets:
        yield problem(f"no `target:` in {IMAGE_WORKFLOW} to check the table against")
    for target in targets:
        if not re.search(rf"^\|.*-{re.escape(target)}[-.]", notes, re.MULTILINE):
            yield problem(
                f"the table has no row for `{target}`, which the workflow builds"
            )
    if not re.search(r"^\S.*sha256sum -c SHA256SUMS", notes, re.MULTILINE):
        yield problem(
            "the notes have no `sha256sum -c SHA256SUMS` line at the start of a line"
        )
    for ref in sorted(set(_REPO_REF_RE.findall(notes)) - {SAMPLE_TAG}):
        yield problem(
            f"the notes link the repository at `{ref}` rather than at the release's tag"
        )
    for step in GUIDE_STEPS:
        if step in notes:
            yield problem(
                f"the notes mention `{step}`: link the step in {GUIDE} instead"
            )


def guide_linkers() -> List[str]:
    docs = {
        path.relative_to(REPO).as_posix()
        for pattern in DOCS
        for path in REPO.glob(pattern)
    }
    return [*RELEASE_WORKFLOWS, IMAGE_NOTES, *sorted(docs)]


def check_repository() -> List[Problem]:
    files = {
        path: (REPO / path).read_text(encoding="utf-8") for path in guide_linkers()
    }
    return [
        *broken_guide_links(files[GUIDE], files),
        *image_notes_problems(files[IMAGE_NOTES], files[IMAGE_WORKFLOW]),
    ]


def main() -> int:
    problems = check_repository()
    for problem in problems:
        print(problem)
    if problems:
        print(f"{len(problems)} problem(s) in the release notes.", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
