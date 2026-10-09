"""The check that keeps the image release notes and the docs linked to the guide.

The notes link docs/installation.md by heading instead of repeating its
steps, so a renamed heading, a step written back into the notes or a heredoc
indented wrongly would otherwise first show on a published release. The
repository itself must pass it.
"""

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
_SPEC = importlib.util.spec_from_file_location(
    "check_release_notes", REPO / "scripts" / "check_release_notes.py"
)
check = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check)

GUIDE = (REPO / check.GUIDE).read_text(encoding="utf-8")
NOTES = (REPO / check.IMAGE_NOTES).read_text(encoding="utf-8")
WORKFLOW = (REPO / check.IMAGE_WORKFLOW).read_text(encoding="utf-8")
GUIDE_URL = (
    "https://github.com/Kalinka-Player/KalinkaPlayer/blob/{}/docs/installation.md"
)


def _messages(notes: str = NOTES, workflow: str = WORKFLOW):
    return [problem.message for problem in check.image_notes_problems(notes, workflow)]


def test_the_repository_passes():
    assert check.check_repository() == []


@pytest.mark.parametrize(
    "heading, anchor",
    [
        ("Install the server", "install-the-server"),
        ("A. Raspberry Pi image", "a-raspberry-pi-image"),
        (
            "3. Wi-Fi, a login or a DAC HAT? Change a setting on the card",
            "3-wi-fi-a-login-or-a-dac-hat-change-a-setting-on-the-card",
        ),
        ("The `kalinka-firstboot.conf` file", "the-kalinka-firstbootconf-file"),
        ("See [Logs](#logs)", "see-logs"),
        ("<b>Logs</b>", "logs"),
    ],
)
def test_a_heading_gets_the_anchor_github_gives_it(heading, anchor):
    assert check.slug(heading) == anchor


def test_a_renamed_heading_breaks_the_guides_own_links_to_it():
    guide = GUIDE.replace("## Settings on the card\n", "## Card settings\n")
    problems = list(check.broken_guide_links(guide, {check.GUIDE: guide}))
    assert problems
    assert all("`#settings-on-the-card`" in problem.message for problem in problems)


def test_a_repeated_heading_is_numbered_and_a_fenced_comment_is_no_heading():
    markdown = "## Logs\n\n```sh\n# not a heading\n```\n\n## Logs\n"
    assert check.heading_anchors(markdown) == {"logs", "logs-1"}


def test_a_closing_hash_run_is_not_part_of_the_heading():
    assert check.heading_anchors("## Logs ##\n## C#\n") == {"logs", "c"}


def test_a_fence_closes_only_on_its_own_marker():
    markdown = (
        "~~~\n```\n# not a heading\n~~~\n\n"
        "````md\n```\n# nor this\n````\n\n## Logs\n"
    )
    assert check.heading_anchors(markdown) == {"logs"}


def test_a_renamed_heading_breaks_the_notes_links_to_it():
    guide = GUIDE.replace("## Settings on the card\n", "## Card settings\n")
    problems = list(check.broken_guide_links(guide, {check.IMAGE_NOTES: NOTES}))
    assert [problem.message for problem in problems] == [
        "`#settings-on-the-card` names no heading in docs/installation.md"
    ]


def test_the_notes_the_workflows_and_the_docs_are_all_checked_for_links():
    linkers = check.guide_linkers()
    assert set(check.RELEASE_WORKFLOWS) <= set(linkers)
    assert {
        check.IMAGE_NOTES,
        ".github/workflows/image-build.yml",
        ".github/workflows/image-release.yml",
        ".github/workflows/supervisor-release.yml",
        "README.md",
        "docs/log-export-design.md",
        "packages/kalinka-image/README.md",
    } <= set(linkers)


def test_a_renamed_heading_breaks_a_bare_link_from_beside_the_guide():
    design = "docs/log-export-design.md"
    text = (REPO / design).read_text(encoding="utf-8")
    guide = GUIDE.replace("## Troubleshooting\n", "## When something goes wrong\n")
    problems = list(check.broken_guide_links(guide, {design: text}))
    assert [problem.message for problem in problems] == [
        "`#troubleshooting` names no heading in docs/installation.md"
    ]


def test_the_notes_link_the_guide_at_the_releases_tag():
    rendered = check.render_notes(NOTES, "kalinka-image-v1.2.3")
    assert rendered.returncode == 0, rendered.stderr
    guide = GUIDE_URL.format("kalinka-image-v1.2.3")
    assert f"]({guide}#install-the-server)" in rendered.stdout
    assert f"]({guide}#settings-on-the-card)" in rendered.stdout
    assert "/blob/main/" not in rendered.stdout


@pytest.mark.parametrize("tag", ["", "1.2.3", "kalinka-v1.2.3", "kalinka-image-v"])
def test_the_notes_are_not_written_for_anything_but_an_image_tag(tag):
    rendered = check.render_notes(NOTES, tag)
    assert rendered.returncode == 2
    assert rendered.stdout == ""
    assert "usage:" in rendered.stderr


def test_a_link_the_script_does_not_move_to_the_tag_is_flagged():
    notes = NOTES.replace(
        "/blob/main/docs/installation.md#install-the-server",
        "/tree/HEAD/docs/installation.md#install-the-server",
    )
    assert _messages(notes) == [
        "the notes link the repository at `HEAD` rather than at the release's tag"
    ]


@pytest.mark.parametrize("step", check.GUIDE_STEPS)
def test_a_step_written_back_into_the_notes_is_flagged(step):
    notes = NOTES.replace("<<'EOF'\n", f"<<'EOF'\nThen use `{step}`.\n")
    assert _messages(notes) == [
        f"the notes mention `{step}`: link the step in docs/installation.md instead"
    ]


def test_the_callers_shell_environment_does_not_reach_the_script(tmp_path, monkeypatch):
    noisy = tmp_path / "bash_env.sh"
    noisy.write_text("echo 'sourced from BASH_ENV' >&2\n", encoding="utf-8")
    monkeypatch.setenv("BASH_ENV", str(noisy))
    monkeypatch.setenv("LC_ALL", "xx_XX.UTF-8")
    assert _messages() == []


def test_an_indented_eof_leaves_the_heredoc_open():
    [message] = _messages(NOTES.replace("\nEOF\n", "\n  EOF\n"))
    assert message.startswith("bash could not write the notes: ")
    assert "here-document" in message


def test_an_unquoted_heredoc_is_flagged():
    [message] = _messages(NOTES.replace("<<'EOF'", "<<EOF"))
    assert message.startswith("bash could not write the notes: ")


def test_an_indented_table_is_flagged():
    messages = _messages(NOTES.replace("\n|", "\n    |"))
    assert "the notes have no table of images at the start of a line" in messages


def test_an_indented_checksum_line_is_flagged():
    notes = NOTES.replace("\nVerify downloads", "\n    Verify downloads")
    assert _messages(notes) == [
        "the notes have no `sha256sum -c SHA256SUMS` line at the start of a line"
    ]


def test_a_built_target_missing_from_the_table_is_flagged():
    notes = "".join(
        line
        for line in NOTES.splitlines(keepends=True)
        if "-amd64.img.xz` |" not in line
    )
    assert _messages(notes) == [
        "the table has no row for `amd64`, which the workflow builds"
    ]


def test_a_variant_row_does_not_stand_in_for_its_base_target():
    notes = "".join(
        line
        for line in NOTES.splitlines(keepends=True)
        if "-rpi5-arm64.img.xz` |" not in line
    )
    assert _messages(notes) == [
        "the table has no row for `rpi5`, which the workflow builds"
    ]


def test_a_target_the_workflow_starts_building_needs_a_row():
    workflow = WORKFLOW.replace(
        "          - target: amd64\n",
        "          - target: amd64\n            runner: ubuntu-24.04\n"
        "          - target: rpi6\n",
    )
    assert _messages(workflow=workflow) == [
        "the table has no row for `rpi6`, which the workflow builds"
    ]


def test_a_workflow_without_a_build_matrix_is_flagged():
    workflow = WORKFLOW.replace("- target:", "- platform:")
    assert _messages(workflow=workflow) == [
        f"no `target:` in {check.IMAGE_WORKFLOW} to check the table against"
    ]
