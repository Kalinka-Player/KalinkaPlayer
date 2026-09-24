"""Presentation schema — display-side description of the settings UI.

The storage model (Pydantic config classes) is the source of truth for values and
validation. This module defines the separate *presentation* layer the backend emits
so the client renders unambiguously without re-mapping anything.

Authoring entry points:
    * Per-field via `Field(..., json_schema_extra={"widget": ..., "help": ...,
      "importance": ..., "setup": ..., "constraints": ...})`.
    * Per-module by the excluded ``name`` field: its ``title`` is the module's
      display name and its ``description`` the one-line summary of what the
      module is. The field itself is never rendered (``exclude=True``), so
      both are read out of band.
    * Per-config-class by declaring class attributes:
          __module_icon__: str          — material icon name for module cards
                                          and source badges
          __module_icon_color__: str    — hex color for icon tile
          __preview_fields__: list[str] — field names to compose the card subtitle
      Or by implementing::
          @classmethod
          def presentation_layout(cls, instance, prefix: str) -> list[SectionSpec]: ...
      for full control of section grouping. Note that such a layout is then
      the *only* source of fields for that subtree — a leaf it forgets to
      list is settable nowhere, expert search included — so prefer the
      auto-derived layout (one section per nested model, in declaration
      order, or none for a model tagged ``inline``) unless the grouping
      genuinely can't be expressed that way.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class Importance(str, Enum):
    """Two-tier UI prominence.

    * SIMPLE — appears in the default settings page. Reserved for fields
      the user is expected to interact with (auth tokens, music folders,
      module enable toggles, the ALSA device, etc.). Must be tagged
      explicitly; the *default* for any field that doesn't declare an
      importance is EXPERT.
    * EXPERT — accessible only through the about:config-style search.
      The structured page won't show these unless the user opens the
      expert view.

    Legacy values ``"normal"`` and ``"advanced"`` (from the previous
    three-tier scheme) are accepted on the input side: ``normal`` maps
    to SIMPLE, ``advanced`` maps to EXPERT. The enum itself only emits
    the two canonical values on the wire.
    """

    SIMPLE = "simple"
    EXPERT = "expert"


class Setup(str, Enum):
    """First-run wizard participation — orthogonal to :class:`Importance`.

    ``Importance`` says where a field lives in the settings screen once
    the server is running; ``Setup`` says whether the app asks for it
    while walking a new user through first-run setup. A field can be any
    combination of the two.

    * REQUIRED — the owning module cannot work until the user supplies a
      value (an API key, an auth token). The wizard must ask, and the
      module stays unconfigured until it is answered, so a required
      field carries no usable default: its declared default is the empty
      value for its type.
    * PROMPT — worth asking during setup, but the default already works.
    * HIDDEN — not part of setup; changeable later in settings. This is
      the default for any field that doesn't declare a ``setup`` tag.
    """

    REQUIRED = "required"
    PROMPT = "prompt"
    HIDDEN = "hidden"


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    DANGER = "danger"


class Widget(str, Enum):
    TEXT = "text"
    RICH_TEXT = "rich_text"
    PASSWORD = "password"
    PATH = "path"
    URL = "url"
    TOGGLE = "toggle"
    NUMBER_INPUT = "number_input"
    NUMBER_SLIDER = "number_slider"
    ENUM_PILLS = "enum_pills"
    ENUM_DROPDOWN = "enum_dropdown"
    LIST_EDITOR = "list_editor"
    FOLDER_LIST = "folder_list"


class Banner(BaseModel):
    text: str
    severity: Severity = Severity.INFO
    title: Optional[str] = None


class Constraints(BaseModel):
    ge: Optional[float] = None
    le: Optional[float] = None
    min_length: Optional[int] = None
    max_length: Optional[int] = None
    step: Optional[float] = None
    pattern: Optional[str] = None
    unit: Optional[str] = None
    slider_min: Optional[float] = None
    slider_max: Optional[float] = None


class OptionSpec(BaseModel):
    """One choice in a dynamic-options enum field.

    ``value`` is the opaque token that gets written back to the
    config (e.g. an ALSA ``hw:CARD=…,DEV=…`` handle); ``label`` is
    what the user sees in the dropdown. Splitting them lets the
    stored identity stay stable (system-readable, survives reboot)
    while the human label can vary with hardware description.

    ``description`` is an optional second line — shown dimmed under
    the label when the dropdown's bottom sheet is open. The collapsed
    trigger still renders the label alone, so adding context here
    doesn't lengthen the rest-state row. Used by ALSA to put the
    PCM-mode description and the ``auto-convert`` / ``not connected``
    hints below the device name without cluttering the trigger.
    """

    value: str
    label: str
    description: Optional[str] = None


class FieldSpec(BaseModel):
    """A single settable leaf in the UI.

    Note on enum dynamism: writable enum fields with *runtime-resolved*
    option lists (e.g. ALSA devices) leave ``enum_values`` empty in
    the schema. Their options ship in the values envelope under
    ``enum_options[path]`` — fresh on every GET /server/config so
    hot-plug is reflected without churning schema_version. Clients
    rendering an enum widget prefer envelope options when present,
    else fall back to ``enum_values``.

    ``dynamic_options=True`` says the same options may arrive for a
    field the user can also type into freely (a folder, an address).
    An enum has nowhere else to get its choices, so envelope presence
    speaks for itself there; an open text field does not, and without
    the flag a client could not tell "nothing to suggest yet" from
    "nothing to suggest, ever" and would have to hide the control the
    moment a resolver came back empty.

    ``dynamic=True`` is a different concept — it marks fields whose
    *value* is plugin-resolved (status views). Those are read-only
    and rejected by PUT /server/config.
    """

    path: str                       # Dotted storage path, e.g. "base_config.server.port"
    label: str
    widget: Widget
    type: str                       # "bool" | "int" | "float" | "str" | "enum" | "list"
    help: Optional[str] = None
    default: Any = None
    readonly: bool = False
    dynamic: bool = False           # Value is resolved by the owning module at request time
    dynamic_options: bool = False   # Options are resolved by the owning module at request time
    importance: Importance = Importance.EXPERT
    setup: Setup = Setup.HIDDEN
    enum_values: Optional[list[str]] = None
    constraints: Optional[Constraints] = None


class VariantSpec(BaseModel):
    """One shape an entry of a collection, or a part of one, can take.

    Built from one class of a discriminated union: ``key`` is the value its
    discriminator field holds, and the label, icon and description come from
    that field's declaration. Paths are relative to the entry.
    """

    key: str
    label: str
    icon: Optional[str] = None
    description: Optional[str] = None
    # Fields whose values make up the card's second line.
    summary: list[str] = Field(default_factory=list)
    fields: list[FieldSpec] = Field(default_factory=list)
    groups: list["GroupSpec"] = Field(default_factory=list)


class GroupSpec(BaseModel):
    """A nested part of an entry, in the order it is declared.

    Either a plain group, with ``fields`` and ``groups`` of its own, or — with
    ``discriminator`` set — a part that takes one of several shapes, one per
    ``variants`` entry. Writing a variant's key into the discriminator is
    what switches the part to it; ``default`` is the shape it takes unset.
    """

    path: str
    title: str
    fields: list[FieldSpec] = Field(default_factory=list)
    groups: list["GroupSpec"] = Field(default_factory=list)
    discriminator: Optional[str] = None
    default: Optional[str] = None
    variants: list[VariantSpec] = Field(default_factory=list)


class CollectionSpec(BaseModel):
    """A list of records, edited one entry at a time.

    The value at ``path`` is a list of objects, each with a stable ``id``.
    A client shows every entry as a card and opens one in a dialog to edit
    it; adding one asks which variant it is when there is more than one.
    The whole list is written back as one value, and a credential an entry
    leaves out keeps what was saved, unless the entry now names another
    server or user: that draws an issue at the credential instead. Paths
    inside the variants are relative to an entry: suggestions for one ride
    ``enum_options`` under ``<path>.<relative path>``, and issues and set
    credentials name the entry by its id, as ``<path>.<id>.<relative
    path>``. An issue names the collection, an entry or a field inside one,
    never a group.

    Old clients ignore it, which is why it rides lists of its own rather
    than a new field widget.
    """

    path: str
    title: str
    help: Optional[str] = None
    # Written with the entry's shape when there is more than one.
    discriminator: Optional[str] = None
    variants: list[VariantSpec] = Field(default_factory=list)
    # Path of the field the collection follows among its siblings; None when
    # it comes before all of them.
    after: Optional[str] = None
    # Paths of fields that hold part of the same value in an older shape. A
    # client showing the collection hides them; one that cannot, ignores this
    # and shows them as before.
    replaces: list[str] = Field(default_factory=list)


class SectionSpec(BaseModel):
    """A labelled group of fields (and optionally nested sub-sections)."""

    id: str
    title: str
    icon: Optional[str] = None
    # Sections default to SIMPLE so the prune step is purely
    # content-driven (an empty/all-expert section is dropped). Setting
    # ``importance=EXPERT`` explicitly force-drops the section in the
    # simple view regardless of children — useful for "advanced tuning"
    # groups that should never surface to casual users.
    importance: Importance = Importance.SIMPLE
    banners: list[Banner] = Field(default_factory=list)
    fields: list[FieldSpec] = Field(default_factory=list)
    collections: list[CollectionSpec] = Field(default_factory=list)
    sections: list["SectionSpec"] = Field(default_factory=list)


class ModuleSpec(BaseModel):
    """A pluggable input module or output device entry on its page.

    Live runtime state (READY/WARNING/ERROR/DISABLED + message + missing
    optional packages) is served by `GET /server/modules`, not the
    schema — the schema describes the static layout. Mixing live state
    into the schema would churn `schema_version` on every transient
    plugin hiccup.

    Top-level scalar fields of the module's CONFIG_MODEL (e.g. localfiles'
    `music_folders`, `db_path`, `scan_interval_minutes`) appear in `fields`
    so the client can render them flat under the module header, not buried
    inside a redundant "General" foldable. Nested BaseModels still become
    entries in `sections`.
    """

    id: str
    kind: Literal["input_module", "device"]
    title: str
    # One sentence on what the source or device *is*, for someone deciding
    # whether to enable it. Unlike ``preview_fields`` (composed from live
    # values) it is static, so it reads the same for a disabled module.
    description: Optional[str] = None
    icon: Optional[str] = None
    icon_color: Optional[str] = None
    preview_fields: list[str] = Field(default_factory=list)
    banners: list[Banner] = Field(default_factory=list)
    fields: list[FieldSpec] = Field(default_factory=list)
    collections: list[CollectionSpec] = Field(default_factory=list)
    sections: list[SectionSpec] = Field(default_factory=list)


class PageSpec(BaseModel):
    """A top-level tab in the settings screen."""

    id: str
    title: str
    icon: Optional[str] = None
    banners: list[Banner] = Field(default_factory=list)
    sections: list[SectionSpec] = Field(default_factory=list)
    modules: list[ModuleSpec] = Field(default_factory=list)


class PresentationSchema(BaseModel):
    """Top-level payload of `GET /server/config/schema`.

    Two parallel views over the same underlying config tree:

    * ``pages`` — the **simple** view, a hierarchy of pages → modules →
      sections → fields. Only SIMPLE-tier fields appear here. Module
      cards are always kept (with at least their enable toggle), even
      when the rest of the module is expert-only, so the user can
      always switch a module on or off without leaving simple mode.
    * ``expert_fields`` — a **flat list** of every settable field
      across the whole config tree, sorted by dotted path, used to back
      the about:config-style search. Includes both simple and expert
      tiers so power users have a single searchable surface (each
      entry carries its own ``importance`` tag). Being the complete
      index, it is also what the first-run wizard filters on
      ``setup`` — a field is offered during setup whatever tier it
      belongs to.

    Both views share a single ``schema_version``: a field's
    re-categorisation or any plugin reload invalidates both at once.
    """

    schema_version: str
    pages: list[PageSpec]
    expert_fields: list[FieldSpec] = Field(default_factory=list)


VariantSpec.model_rebuild()
GroupSpec.model_rebuild()
SectionSpec.model_rebuild()
