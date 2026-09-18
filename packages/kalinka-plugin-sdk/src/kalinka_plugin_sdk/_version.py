"""Single source of truth for the SDK version.

This is a hand-maintained source file (the SDK does not use setuptools_scm).
`pyproject.toml` reads it via ``[tool.setuptools.dynamic]`` and ``__init__.py``
re-exports it. Bump the major only for a breaking API change, then move the
consumers' ``>=N,<N+1`` pins onto the new major. See RELEASING.md.
"""

__version__ = "3.2.0"
