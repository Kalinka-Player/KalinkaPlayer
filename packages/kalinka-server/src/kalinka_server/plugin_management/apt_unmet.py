"""Name what apt could not satisfy, from the report it prints when it refuses.

Under ``LC_ALL=C`` apt words the report the same from Ubuntu 24.04's apt 2.7
to Debian 13's apt 3.0: a line for each package it refuses, naming its first
unmet dependency, and an indented line for each further one, such as
``Depends: bluez (>= 5.79) but 5.72-1 is to be installed``, ``… but it is not
going to be installed`` or ``… but it is not installable``.

A report is explained only when every line about the package is read here:
an alternative, a virtual package or a conflict keeps the generic message.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable, Optional

# Debian's own character sets, which also keep a URL out of the message.
_NAME = r"[a-z0-9][a-z0-9+.-]*(?::[a-z0-9-]+)?"
_VERSION = r"[A-Za-z0-9.+~:-]+"
_SUBJECT = re.compile(r" (\S+) :(.*)")
_ENTRY = re.compile(
    rf"(?:Pre)?Depends: (?P<name>{_NAME})"
    rf"(?: \((?P<relation><<|<=|=|>=|>>) (?P<version>{_VERSION})\))?"
    rf" but (?:(?P<found>{_VERSION}) is to be installed"
    r"|it is (?P<state>not installable|not going to be installed))"
)
_CANDIDATE = re.compile(rf"^\s*Candidate: ({_VERSION})$", re.MULTILINE)
_RELATIONS = {
    ">=": "{} or newer",
    ">>": "newer than {}",
    "<=": "{} or older",
    "<<": "older than {}",
    "=": "{} exactly",
}


@dataclass(frozen=True)
class Unmet:
    """A dependency apt could not satisfy, and what apt said about it."""

    name: str
    relation: Optional[str]
    version: Optional[str]
    #: The version apt would install instead, when it names one.
    found: Optional[str]
    #: False when no package source offers the package at all.
    installable: bool


def unmet_dependencies(output: str, package: str) -> list[Unmet]:
    """What apt's output says ``package`` lacks; empty unless all of it is read."""
    unmet: list[Unmet] = []
    mine = False
    for line in output.splitlines():
        head = _SUBJECT.fullmatch(line)
        if head:
            mine = head[1].partition(":")[0] == package
            line = head[2]
        elif not line.startswith("  "):
            mine = False
        if not mine:
            continue
        entry = _ENTRY.fullmatch(line.strip())
        if entry is None:
            return []
        unmet.append(
            Unmet(
                name=entry["name"],
                relation=entry["relation"],
                version=entry["version"],
                found=entry["found"],
                installable=entry["state"] != "not installable",
            )
        )
    return unmet


def candidate(policy: str) -> Optional[str]:
    """The version ``apt-cache policy`` would install, or None."""
    found = _CANDIDATE.search(policy)
    return found[1] if found else None


def needs(unmet: list[Unmet], offered: Callable[[str], Optional[str]]) -> str:
    """One sentence naming each dependency, the version it needs and what
    this system has; ``offered`` looks up a version apt would not install."""
    parts = [f"{_required(item)} ({_situation(item, offered)})" for item in unmet]
    if len(parts) > 1:
        parts[-2:] = [f"{parts[-2]} and {parts[-1]}"]
    return f"Needs {', '.join(parts)}."


def _required(item: Unmet) -> str:
    if item.relation is None:
        return item.name
    return f"{item.name} {_RELATIONS[item.relation].format(item.version)}"


def _situation(item: Unmet, offered: Callable[[str], Optional[str]]) -> str:
    if item.found:
        return f"this system has {item.found}"
    if not item.installable:
        return "no package source here offers it"
    version = offered(item.name)
    if version is None:
        return "apt will not install it"
    return f"this system offers {version}, which apt will not install"
