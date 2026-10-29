"""Generated plugins contain only the implementation their type uses."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from cookiecutter.exceptions import FailedHookException
from cookiecutter.hooks import run_script_with_context

TEMPLATE = Path(__file__).resolve().parents[1]


def _generate(output, plugin_type, plugin_id, hooks):
    context = []
    if plugin_type is not None:
        context.append(f"plugin_type={plugin_type}")
    if plugin_id is not None:
        context.append(f"plugin_id={plugin_id}")
    config = output.with_suffix(".json")
    config.write_text(
        json.dumps(
            {
                "cookiecutters_dir": str(output.parent / "cookiecutters"),
                "replay_dir": str(output.parent / "replay"),
            }
        )
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cookiecutter",
            "--no-input",
            "--config-file",
            str(config),
            "--accept-hooks",
            hooks,
            "--output-dir",
            str(output),
            str(TEMPLATE),
            *context,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return output / "kalinka-plugin-myawesome"


def _files(project):
    return {
        path.relative_to(project): path.read_bytes()
        for path in project.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("plugin_type", ["input_module", "device"])
@pytest.mark.parametrize("plugin_id", ["kalinka_plugin_myawesome", "custom_plugin"])
def test_generation_removes_only_the_other_type(tmp_path, plugin_type, plugin_id):
    other_type = "device" if plugin_type == "input_module" else "input_module"
    sentinel = tmp_path / "src" / plugin_id / f"{plugin_id}_{other_type}.py"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("existing project")
    template_project = TEMPLATE / "{{cookiecutter.plugin_name}}"
    template_files = _files(template_project)
    baseline = _generate(tmp_path / "without hooks", plugin_type, plugin_id, "no")
    generated = _generate(tmp_path / "with hooks", plugin_type, plugin_id, "yes")
    expected = _files(baseline)
    unused = Path("src") / plugin_id / f"{plugin_id}_{other_type}.py"

    del expected[unused]

    assert _files(generated) == expected
    assert sentinel.read_text() == "existing project"
    assert _files(template_project) == template_files


def test_generation_uses_the_default_plugin_type_and_name(tmp_path):
    generated = _generate(tmp_path / "defaults", None, None, "yes")
    package = generated / "src" / "kalinka_plugin_myawesome"

    assert {path.name for path in package.iterdir()} == {
        "__init__.py",
        "config_model.py",
        "module_setup.py",
        "kalinka_plugin_myawesome_input_module.py",
    }


def _run_hook(project, plugin_id="custom_plugin", plugin_type="input_module"):
    run_script_with_context(
        str(TEMPLATE / "hooks" / "post_gen_project.py"),
        str(project),
        {"cookiecutter": {"plugin_id": plugin_id, "plugin_type": plugin_type}},
    )


@pytest.mark.parametrize("plugin_type", ["input_module", "device"])
def test_hook_refuses_a_stub_outside_the_project(tmp_path, plugin_type):
    project = tmp_path / "generated"
    project.mkdir()
    outside = tmp_path / "existing"
    outside.mkdir()
    (project / "src").symlink_to(outside, target_is_directory=True)
    package = outside / "custom_plugin"
    package.mkdir()
    for suffix in ("input_module", "device"):
        (package / f"custom_plugin_{suffix}.py").write_text("existing code")
    before = _files(outside)

    with pytest.raises(FailedHookException):
        _run_hook(project, plugin_type=plugin_type)

    assert _files(outside) == before


@pytest.mark.parametrize("plugin_id", ["../outside", "/absolute", "quoted'name"])
def test_hook_refuses_invalid_package_names(tmp_path, plugin_id, capfd):
    (tmp_path / "src").mkdir()
    (tmp_path / "outside").mkdir()
    sentinel = tmp_path / "outside_device.py"
    sentinel.write_text("existing code")

    with pytest.raises(FailedHookException):
        _run_hook(tmp_path, plugin_id=plugin_id)

    assert sentinel.read_text() == "existing code"
    assert "plugin_id must be a Python identifier" in capfd.readouterr().err


def test_hook_refuses_an_unknown_plugin_type(tmp_path):
    package = tmp_path / "src" / "custom_plugin"
    package.mkdir(parents=True)
    for suffix in ("input_module", "device"):
        (package / f"custom_plugin_{suffix}.py").write_text("generated code")
    before = _files(tmp_path)

    with pytest.raises(FailedHookException):
        _run_hook(tmp_path, plugin_type="unknown")

    assert _files(tmp_path) == before


def test_hook_reports_a_missing_stub(tmp_path):
    with pytest.raises(FailedHookException):
        _run_hook(tmp_path)
