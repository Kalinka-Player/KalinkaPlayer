"""Configuration the user keeps as a collection of records.

A list of strings is enough while an entry is a single value. Once an entry
has parts — a server, a folder on it, the account to sign in with — it is a
record, and a list of records needs two things a list of strings does not:
an identity that survives reordering, and a way to hold a credential the
server never hands back.

Declare such a field as ``Records[SomeRecord]``. The server then keys each
entry by its ``id``, leaves every credential out of what it sends a client,
and keeps a credential a client leaves out when it writes the collection
back, for as long as the entry's :meth:`ConfigRecord.credential_scope` stays
the same.

The settings page shows the collection as cards, each opening a dialog. A
union of records, told apart by a ``Literal`` discriminator field, lets an
entry take one of several shapes: that field's ``title`` labels the shape,
its ``description`` explains it and ``json_schema_extra={"icon": ...}``
draws it, and titles the entry's card. A record names the fields
summarised under that title in ``__preview_fields__``, a ``ClassVar``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Hashable, Optional, TypeVar

from pydantic import AfterValidator, BaseModel, Field

#: What an id may be written as. It names the record in dotted paths, so it
#: can hold no dot.
RECORD_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"


def new_record_id() -> str:
    return f"rec_{uuid.uuid4().hex[:12]}"


class ConfigRecord(BaseModel):
    """One entry of a :data:`Records` collection.

    @note ``id`` is the entry's identity for as long as it exists: an issue
        about it, and a credential kept for it, follow it wherever it moves
        in the list. A client adding an entry may leave it out and read back
        the one assigned.
    """

    id: str = Field(default_factory=new_record_id, pattern=RECORD_ID_PATTERN)

    def credential_scope(self) -> Optional[Hashable]:
        """What a credential saved on this record is good for.

        A write that leaves the credential out keeps the saved one only when
        this is unchanged, so pointing an entry at another server never
        sends that server the password meant for the old one. The default
        keeps it for as long as the entry keeps its type.
        """
        return None


def _distinct_ids(records: list[ConfigRecord]) -> list[ConfigRecord]:
    seen: set[str] = set()
    for record in records:
        if record.id in seen:
            raise ValueError(f"two entries share the id {record.id}")
        seen.add(record.id)
    return records


RecordT = TypeVar("RecordT")

#: A collection of records, no two with the same id.
Records = Annotated[list[RecordT], AfterValidator(_distinct_ids)]
