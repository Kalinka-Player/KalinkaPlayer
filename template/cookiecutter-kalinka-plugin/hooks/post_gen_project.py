"""Keep only the implementation selected for the generated plugin."""

from pathlib import Path

plugin_id = {{ cookiecutter.plugin_id | tojson }}
plugin_type = {{ cookiecutter.plugin_type | tojson }}

if not plugin_id.isidentifier():
    raise ValueError("plugin_id must be a Python identifier")

unused_type = {"input_module": "device", "device": "input_module"}[plugin_type]
unused_stub = Path("src") / plugin_id / f"{plugin_id}_{unused_type}.py"

if not unused_stub.resolve().is_relative_to(Path.cwd().resolve()):
    raise ValueError("Unused plugin stub must stay inside the generated project")

unused_stub.unlink()
