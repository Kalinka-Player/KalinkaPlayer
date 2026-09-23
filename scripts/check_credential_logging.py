#!/usr/bin/env python3
"""Flag code that could write a credential to a log.

A credential never reaches a log in plain text, and the review is where that
has to be caught: CI runs this on every pull request, and each finding shows
as an annotation on the line in the diff.

Flagged, in any log call (``logger.info(...)`` and kin, ``print``) or in the
arguments of a raised exception, whose text ends up logged:

* a value whose name marks a credential — ``password``, ``api_key``,
  ``config.smb.password``, ``data["token"]`` — or a container of them such as
  ``headers``; ``loggable(...)`` and plain checks like ``len(token)`` or
  ``token is None`` are not values;
* a message that pairs a credential's name with a placeholder, such as
  ``"password=%s"`` or ``f"token: {x}"``.

A reviewed false positive is kept with ``# log-safe: <why>`` on any line of
the flagged call.

Usage: check_credential_logging.py [PATH ...]; the default is every
package's sources. Exit status 1 when anything is flagged.
"""

import argparse
import ast
import io
import os
import re
import sys
import tokenize
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional, Sequence, Set

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "packages" / "kalinka-server" / "src"))

from kalinka_server.credential_names import is_credential_name  # noqa: E402

_LOG_METHODS = frozenset(
    ("debug", "info", "warning", "warn", "error", "exception", "critical", "fatal", "log")
)

# Hold credentials without being named for one.
_CARRIER_NAMES = frozenset(("headers", "cookies"))

# Their result is not the value they are given.
_SAFE_CALLS = frozenset(
    ("loggable", "is_secret", "is_secret_path", "bool", "len", "type", "isinstance", "hasattr", "keys")
)

# Keywords of a log call that configure it rather than carry a value.
_LOG_OPTIONS = frozenset(("exc_info", "stack_info", "stacklevel"))

_PLACEHOLDER_PAIR_RE = re.compile(
    r"([\w.\-]+)[\"']?\s*[:=]\s*[\"']?(%[-#0 +]*\d*(?:\.\d+)?[a-z]|\{)"
)

_SUPPRESS_RE = re.compile(r"#\s*log-safe:\s*\S")


@dataclass(frozen=True)
class Finding:
    """One place that could log a credential, as a reviewer sees it."""

    path: str
    line: int
    column: int
    message: str

    def plain(self) -> str:
        return f"{self.path}:{self.line}:{self.column + 1}: {self.message}"

    def github(self) -> str:
        return (
            f"::error file={self.path},line={self.line},col={self.column + 1},"
            f"title=Credential in a log::{self.message}"
        )


def _terminal_name(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_log_call(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id == "print"
    if isinstance(func, ast.Attribute) and func.attr in _LOG_METHODS:
        receiver = _terminal_name(func.value)
        return receiver is not None and "log" in receiver.lower()
    return False


def _names_a_credential(name: str) -> bool:
    return is_credential_name(name) or name.lower() in _CARRIER_NAMES


def _credential_values(node: ast.AST) -> Iterator[str]:
    """The credential-named values ``node`` would put into a message."""
    if isinstance(node, (ast.Compare, ast.Constant)):
        return
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return
    if isinstance(node, ast.Call) and _terminal_name(node.func) in _SAFE_CALLS:
        return
    if isinstance(node, ast.Name) and _names_a_credential(node.id):
        yield node.id
    elif isinstance(node, ast.Attribute) and _names_a_credential(node.attr):
        yield node.attr
    elif (
        isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
        and _names_a_credential(node.slice.value)
    ):
        yield node.slice.value
    elif isinstance(node, ast.Dict):
        for key, value in zip(node.keys, node.values):
            if (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and _names_a_credential(key.value)
                and not isinstance(value, ast.Constant)
            ):
                yield key.value
    if isinstance(node, ast.Call):
        called = _terminal_name(node.func)
        if called is not None and _names_a_credential(called):
            yield f"{called}()"
        for keyword in node.keywords:
            if keyword.arg and _names_a_credential(keyword.arg):
                yield keyword.arg
        # A method's result comes from its receiver: `password.strip()`.
        receiver = [node.func.value] if isinstance(node.func, ast.Attribute) else []
        children = [*receiver, *node.args, *(k.value for k in node.keywords)]
    elif isinstance(node, ast.Attribute):
        # The object is not what gets logged: `config.name` is not `config`.
        return
    else:
        children = list(ast.iter_child_nodes(node))
    for child in children:
        yield from _credential_values(child)


def _message_template(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value if isinstance(part, ast.Constant) else "{}" for part in node.values
        )
    return None


def _placeholder_names(template: str) -> Iterator[str]:
    for found in _PLACEHOLDER_PAIR_RE.finditer(template):
        if is_credential_name(found.group(1)):
            yield found.group(1)


def _call_arguments(call: ast.Call) -> List[ast.AST]:
    return list(call.args) + [
        keyword.value for keyword in call.keywords if keyword.arg not in _LOG_OPTIONS
    ]


def _problems(call: ast.Call, kind: str) -> Iterator[str]:
    for argument in _call_arguments(call):
        for name in _credential_values(argument):
            yield f"{kind} gets `{name}`, which names a credential"
        template = _message_template(argument)
        if template is not None:
            for name in _placeholder_names(template):
                yield f"{kind} message fills in `{name}`, which names a credential"


def _suppressed_lines(source: str) -> Set[int]:
    lines = set()
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT and _SUPPRESS_RE.search(token.string):
                lines.add(token.start[0])
    except (tokenize.TokenError, SyntaxError):
        pass
    return lines


def check_source(source: str, path: str) -> List[Finding]:
    """Everything in one module's source that could log a credential."""
    tree = ast.parse(source, filename=path)
    suppressed = _suppressed_lines(source)
    findings = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_log_call(node):
            call, kind = node, "log call"
        elif isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            call, kind = node.exc, "raised exception"
        else:
            continue
        span = range(node.lineno, (node.end_lineno or node.lineno) + 1)
        if suppressed.intersection(span):
            continue
        for message in dict.fromkeys(_problems(call, kind)):
            findings.append(Finding(path, node.lineno, node.col_offset, message))
    return findings


def default_paths() -> List[Path]:
    return sorted((REPO / "packages").glob("*/src"))


def python_files(paths: Sequence[Path]) -> Iterator[Path]:
    for path in paths:
        if path.is_file():
            yield path
            continue
        for file in sorted(path.rglob("*.py")):
            if not file.name.endswith("_pb2.py"):
                yield file


def check_paths(paths: Sequence[Path]) -> List[Finding]:
    findings = []
    for file in python_files(paths):
        try:
            shown = str(file.resolve().relative_to(REPO))
        except ValueError:
            shown = str(file)
        findings.extend(check_source(file.read_text(encoding="utf-8"), shown))
    return findings


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="*", type=Path)
    args = parser.parse_args(argv)
    findings = check_paths(args.paths or default_paths())
    github = os.environ.get("GITHUB_ACTIONS") == "true"
    for finding in findings:
        print(finding.github() if github else finding.plain())
    if findings:
        print(
            f"{len(findings)} place(s) could log a credential. Log without the value, "
            "or mark a reviewed false positive with `# log-safe: <why>`.",
            file=sys.stderr,
        )
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
