"""Schema emitter: walks Pydantic config models to build the presentation schema
and a flat values dict for the wire.

The old nested "{type, title, fields}" wire format has been replaced by two
independent payloads:

    GET /server/config/schema  → PresentationSchema (pages + expert_fields)
    GET /server/config         → {"schema_version", "values": flat dotted-path dict,
                                  "secrets_set": credential paths holding a value}
    PUT /server/config         → {"schema_version", "changes": {path: value}}

``PresentationSchema`` carries two parallel views:

    * ``pages`` — simple view, hierarchical; only SIMPLE-tier fields appear.
      Module cards are always kept with at least their enable toggle.
    * ``expert_fields`` — flat list of every settable field (SIMPLE +
      EXPERT), sorted by dotted path, backing the about:config search UI.

The default tier for a field that doesn't declare ``importance`` is EXPERT.
Anything user-facing must opt in explicitly via
``Field(json_schema_extra={"importance": "simple"})``.

A second, independent tag drives the app's first-run wizard:
``Field(json_schema_extra={"setup": "required" | "prompt"})``, defaulting
to ``hidden``. It rides on every emitted FieldSpec and does not move a
field between the simple and expert views.

A third, ``Field(json_schema_extra={"private": True})``, keeps a personal value
— a host, a path, a user name — out of every log line the server writes about
the configuration, which then names the field and says it was updated.
Clients still get the value; a credential is private without the tag. See
:mod:`config_secrets`.

A list of records (``Records[...]`` of the SDK) is described as a
collection: cards a client opens one at a time, each entry laid out by the
shape its discriminator names. Collections ride lists of their own, which old
clients ignore, and never the expert list, which has no editor for one. A
list of models that are not records is not described at all.

A monotonic ``schema_version`` string lets the client detect staleness
after plugin reloads.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, List, Union, get_origin, get_args

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from kalinka_plugin_sdk import ConfigRecord
from kalinka_plugin_sdk.module_config import ModuleConfig

from .config_overrides import (
    default_variant,
    discriminator_of,
    is_record_list,
    models_in,
)
from .config_secrets import is_private, is_secret, loggable, public_records
from .dynamic_field_registry import DynamicFieldEntry, resolve_value
from .options_registry import OptionsRegistry
from .presentation_schema import (
    Banner,
    CollectionSpec,
    Constraints,
    FieldSpec,
    GroupSpec,
    Importance,
    ModuleSpec,
    OptionSpec,
    PageSpec,
    PresentationSchema,
    SectionSpec,
    Setup,
    Severity,
    VariantSpec,
    Widget,
)

logger = logging.getLogger(__name__.split(".")[-1])


# ---------------------------------------------------------------------------
# Type utilities
# ---------------------------------------------------------------------------


def _unwrap_optional(annotation: Any) -> Any:
    origin = get_origin(annotation)
    if origin is Union:
        non_none = [a for a in get_args(annotation) if a is not type(None)]
        if len(non_none) == 1:
            return non_none[0]
    return annotation


def annotation_to_type(annotation: Any) -> str:
    """Map a Pydantic annotation to a simple wire type string."""
    annotation = _unwrap_optional(annotation)

    origin = get_origin(annotation)
    if origin in (list, List):
        args = get_args(annotation)
        inner = annotation_to_type(args[0]) if args else "str"
        return f"list[{inner}]"

    if isinstance(annotation, type):
        if issubclass(annotation, BaseModel):
            return "section"
        if issubclass(annotation, Enum):
            return "enum"
        if annotation.__module__ == "builtins":
            return annotation.__name__
        return f"{annotation.__module__}.{annotation.__qualname__}"

    return str(annotation)


def _json_extra(field: FieldInfo) -> dict[str, Any]:
    extra = getattr(field, "json_schema_extra", None)
    return extra if isinstance(extra, dict) else {}


def _extract_constraints(field: FieldInfo, extras: dict[str, Any]) -> Constraints | None:
    c = Constraints()
    found = False
    for m in field.metadata or []:
        for attr in ("ge", "gt", "le", "lt", "min_length", "max_length"):
            if hasattr(m, attr):
                val = getattr(m, attr)
                if val is None:
                    continue
                if attr == "gt":
                    c.ge = val
                elif attr == "lt":
                    c.le = val
                else:
                    setattr(c, attr, val)
                found = True
    extra_c = extras.get("constraints") or {}
    for k, v in extra_c.items():
        if hasattr(c, k):
            setattr(c, k, v)
            found = True
    return c if found else None


def _infer_widget(wire_type: str, field_name: str, extras: dict[str, Any]) -> Widget:
    # Explicit opt-in from the model takes precedence
    widget_hint = extras.get("widget")
    if widget_hint:
        try:
            return Widget(widget_hint)
        except ValueError:
            logger.warning("Unknown widget %r on field %s", widget_hint, field_name)

    if wire_type == "bool":
        return Widget.TOGGLE
    if wire_type == "enum":
        return Widget.ENUM_PILLS
    if wire_type in ("int", "float"):
        return Widget.NUMBER_INPUT
    if wire_type.startswith("list["):
        return Widget.LIST_EDITOR
    return Widget.TEXT


# Legacy three-tier tags accepted for plugin back-compat. The wire model
# only emits "simple"/"expert" — see presentation_schema.Importance.
_LEGACY_IMPORTANCE_ALIASES = {
    "normal": Importance.SIMPLE,
    "advanced": Importance.EXPERT,
}


#: Widgets a suggestion list can be rendered on. The dropdown takes its
#: choices from one; the open inputs offer them beside what the user types.
#: The rest have no room for a suggestion and no use for one — pills among
#: them, which lay out a fixed set and have nowhere to put a late arrival.
_SUGGESTIBLE_WIDGETS = frozenset(
    {
        Widget.TEXT,
        Widget.PATH,
        Widget.URL,
        Widget.LIST_EDITOR,
        Widget.FOLDER_LIST,
        Widget.ENUM_DROPDOWN,
    }
)


def _dynamic_options_from_extras(
    extras: dict[str, Any], widget: Widget, path: str
) -> bool:
    if not extras.get("dynamic_options"):
        return False
    if widget not in _SUGGESTIBLE_WIDGETS:
        logger.warning(
            "Field %s is tagged dynamic_options but renders as %s, which "
            "cannot show suggestions; ignoring the tag",
            path,
            widget.value,
        )
        return False
    return True


def _importance_from_extras(
    extras: dict[str, Any], default: Importance = Importance.EXPERT
) -> Importance:
    """Resolve a tier tag, falling back to ``default`` when untagged.

    The two callers pass opposite defaults on purpose. A *leaf* defaults
    to EXPERT, so the structured page shows only what a developer opted
    in. A *section* defaults to SIMPLE, so pruning is content-driven — a
    group disappears once every leaf under it is expert — and tagging a
    section ``"expert"`` force-hides the whole group whatever its
    children say.
    """
    hint = extras.get("importance")
    if hint is None:
        return default
    if hint in _LEGACY_IMPORTANCE_ALIASES:
        return _LEGACY_IMPORTANCE_ALIASES[hint]
    try:
        return Importance(hint)
    except ValueError:
        logger.warning(
            "Unknown importance %r; defaulting to %s", hint, default.value
        )
        return default


def _setup_from_extras(extras: dict[str, Any]) -> Setup:
    """Resolve the first-run tag, defaulting to HIDDEN when untagged.

    Independent of ``importance``: setup asks *when the app collects a
    value*, importance asks *where it sits in the settings screen*.
    """
    hint = extras.get("setup")
    if hint is None:
        return Setup.HIDDEN
    try:
        return Setup(hint)
    except ValueError:
        logger.warning("Unknown setup %r; defaulting to hidden", hint)
        return Setup.HIDDEN


def _warn_if_required_has_default(path: str, field: FieldInfo) -> None:
    """A REQUIRED field must start empty — the wizard reads "still unset"
    off the value itself, so a usable default would report the module as
    configured before the user has supplied anything."""
    if field.is_required():
        return
    try:
        default = field.get_default(call_default_factory=True)
    except ValueError:
        # Factory wants the other fields' values; a diagnostic isn't worth
        # building them.
        return
    if not default:
        return
    if is_private(field):
        logger.warning(
            "Field %s is tagged setup=required but has a default; a required "
            "field must default to the empty value for its type",
            path,
        )
    else:
        logger.warning(
            "Field %s is tagged setup=required but defaults to %s; a "
            "required field must default to the empty value for its type",
            path,
            loggable(default),
        )


def _enum_values(field: FieldInfo) -> list[str] | None:
    ann = _unwrap_optional(field.annotation)
    if isinstance(ann, type) and issubclass(ann, Enum):
        return [e.value for e in ann.__members__.values()]
    return None


# ---------------------------------------------------------------------------
# Leaf → FieldSpec
# ---------------------------------------------------------------------------


def _build_field_spec(
    path: str, field_name: str, field: FieldInfo
) -> FieldSpec | None:
    """Turn a single non-section Pydantic field into a FieldSpec. Returns None
    if the field is excluded from serialization (e.g. internal `name`).
    """
    if field.exclude:
        return None

    wire_type = annotation_to_type(field.annotation)
    if wire_type == "section":
        raise ValueError(f"_build_field_spec called on section {field_name}")

    extras = _json_extra(field)
    setup = _setup_from_extras(extras)
    if setup is Setup.REQUIRED:
        _warn_if_required_has_default(path, field)
    widget = _infer_widget(wire_type, field_name, extras)
    return FieldSpec(
        path=path,
        label=field.title or field_name,
        help=extras.get("help") or field.description or None,
        widget=widget,
        type=wire_type,
        default=None if is_secret(field) else field.default,
        readonly=bool(field.frozen),
        dynamic_options=_dynamic_options_from_extras(extras, widget, path),
        importance=_importance_from_extras(extras),
        setup=setup,
        enum_values=_enum_values(field) if wire_type == "enum" else None,
        constraints=_extract_constraints(field, extras),
    )


def _is_record_collection(annotation: Any) -> bool:
    return is_record_list(annotation) and all(
        issubclass(model, ConfigRecord) for model in models_in(annotation)
    )


def _record_parts(
    model: type[BaseModel], prefix: str, skip: frozenset[str]
) -> tuple[list[FieldSpec], list[GroupSpec]]:
    fields: list[FieldSpec] = []
    groups: list[GroupSpec] = []
    for name, field in model.model_fields.items():
        if name in skip or field.exclude or is_record_list(field.annotation):
            continue
        path = f"{prefix}{name}"
        models = models_in(field.annotation)
        if models:
            groups.append(_group_spec(path, name, field, models))
        else:
            spec = _build_field_spec(path, name, field)
            if spec is not None:
                fields.append(spec)
    return fields, groups


def _variant_spec(
    model: type[BaseModel],
    discriminator: str | None,
    prefix: str,
    fallback_label: str,
) -> VariantSpec:
    """One shape, labelled by the declaration of its discriminator field."""
    tag = model.model_fields.get(discriminator) if discriminator else None
    skip = {discriminator} if discriminator else set()
    if issubclass(model, ConfigRecord):
        skip.add("id")
    fields, groups = _record_parts(model, prefix, frozenset(skip))
    return VariantSpec(
        key="" if tag is None else str(tag.default),
        label=(tag.title if tag is not None and tag.title else fallback_label),
        icon=None if tag is None else _json_extra(tag).get("icon"),
        description=None if tag is None else tag.description,
        summary=list(getattr(model, "__preview_fields__", [])),
        fields=fields,
        groups=groups,
    )


def _group_spec(
    path: str, name: str, field: FieldInfo, models: tuple[type[BaseModel], ...]
) -> GroupSpec:
    title = field.title or name
    discriminator = discriminator_of(field.annotation, field)
    if discriminator is None:
        fields, groups = _record_parts(models[0], f"{path}.", frozenset())
        return GroupSpec(path=path, title=title, fields=fields, groups=groups)
    return GroupSpec(
        path=path,
        title=title,
        discriminator=discriminator,
        default=default_variant(field, discriminator),
        variants=[
            _variant_spec(model, discriminator, f"{path}.", model.__name__)
            for model in models
        ],
    )


def _collection_spec(
    path: str, field: FieldInfo, after: str | None
) -> CollectionSpec:
    item = get_args(field.annotation)[0]
    discriminator = discriminator_of(item)
    extras = _json_extra(field)
    parent, _, name = path.rpartition(".")
    title = field.title or name
    return CollectionSpec(
        path=path,
        title=title,
        help=extras.get("help") or field.description or None,
        discriminator=discriminator,
        variants=[
            _variant_spec(model, discriminator, "", title)
            for model in models_in(item)
        ],
        after=after,
        replaces=[f"{parent}.{other}" for other in extras.get("replaces", [])],
    )


def _reanchor(
    collections: list[CollectionSpec],
    fields: list[FieldSpec],
    shown: list[FieldSpec],
) -> list[CollectionSpec]:
    """``collections`` placed after the nearest field a pruned view still
    shows, so dropping the one a collection followed does not move it to the
    end."""
    order = [f.path for f in fields]
    kept = {f.path for f in shown}
    placed: list[CollectionSpec] = []
    for collection in collections:
        at = order.index(collection.after) if collection.after in order else -1
        while at >= 0 and order[at] not in kept:
            at -= 1
        placed.append(
            collection.model_copy(update={"after": order[at] if at >= 0 else None})
        )
    return placed


# ---------------------------------------------------------------------------
# Auto-derived section layout (when a config class does not override)
# ---------------------------------------------------------------------------


def _split_general(
    sections: list[SectionSpec], prefix: str
) -> tuple[list[FieldSpec], list[CollectionSpec], list[SectionSpec]]:
    """Split off ``prefix``'s auto "General" section so its fields and
    collections sit on whatever wraps them, not under a redundant heading."""
    general_id = f"{prefix}.general"
    fields: list[FieldSpec] = []
    collections: list[CollectionSpec] = []
    rest: list[SectionSpec] = []
    for s in sections:
        if s.id == general_id and not s.sections:
            fields = s.fields
            collections = s.collections
        else:
            rest.append(s)
    return fields, collections, rest


def _auto_sections(model: BaseModel, prefix: str) -> list[SectionSpec]:
    """Default layout: scalars of this model go into a single 'General' section;
    each nested BaseModel becomes its own section (recursively), unless it is
    tagged ``inline``, in which case its scalars join this model's own at the
    point it is declared.
    """
    scalar_fields: list[FieldSpec] = []
    collections: list[CollectionSpec] = []
    nested_sections: list[SectionSpec] = []

    for field_name, field in model.__class__.model_fields.items():
        child_path = f"{prefix}.{field_name}" if prefix else field_name
        last_field = scalar_fields[-1].path if scalar_fields else None
        if is_record_list(field.annotation):
            if _is_record_collection(field.annotation):
                collections.append(_collection_spec(child_path, field, last_field))
            continue
        wire_type = annotation_to_type(field.annotation)

        if wire_type == "section":
            extras = _json_extra(field)
            promoted_fields, promoted_collections, kept_sections = _split_general(
                _sections_for(getattr(model, field_name), child_path), child_path
            )
            if extras.get("inline"):
                scalar_fields.extend(promoted_fields)
                collections.extend(
                    c if c.after else c.model_copy(update={"after": last_field})
                    for c in promoted_collections
                )
                nested_sections.extend(kept_sections)
                continue

            nested_sections.append(
                SectionSpec(
                    id=child_path,
                    title=field.title or field_name,
                    importance=_importance_from_extras(extras, Importance.SIMPLE),
                    fields=promoted_fields,
                    collections=promoted_collections,
                    sections=kept_sections,
                )
            )
        else:
            spec = _build_field_spec(child_path, field_name, field)
            if spec is not None:
                scalar_fields.append(spec)

    result: list[SectionSpec] = []
    if scalar_fields or collections:
        result.append(
            SectionSpec(
                id=f"{prefix}.general" if prefix else "general",
                title="General",
                fields=scalar_fields,
                collections=collections,
            )
        )
    result.extend(nested_sections)
    return result


def _sections_for(model: BaseModel, prefix: str) -> list[SectionSpec]:
    """Return sections for a config class, honoring a `presentation_layout`
    override if defined; otherwise auto-derive.
    """
    override = getattr(model.__class__, "presentation_layout", None)
    if callable(override):
        try:
            return list(override(model, prefix))
        except Exception as exc:
            logger.exception("presentation_layout(%s) failed: %s", prefix, exc)
    return _auto_sections(model, prefix)


# ---------------------------------------------------------------------------
# Module/device → ModuleSpec
# ---------------------------------------------------------------------------


def _find_section_by_id(
    sections: list[SectionSpec], target_id: str
) -> SectionSpec | None:
    """Walk a section tree (DFS) and return the section whose id matches."""
    for s in sections:
        if s.id == target_id:
            return s
        nested = _find_section_by_id(s.sections, target_id)
        if nested is not None:
            return nested
    return None


def _dynamic_field_spec(
    entry: DynamicFieldEntry,
) -> FieldSpec:
    """Build a FieldSpec from a dynamic-field declaration."""
    decl = entry.decl
    try:
        widget = Widget(decl.widget)
    except ValueError:
        logger.warning(
            "Dynamic field %s declared unknown widget %r; falling back to 'text'",
            entry.full_path,
            decl.widget,
        )
        widget = Widget.TEXT
    importance = _importance_from_extras({"importance": decl.importance})
    return FieldSpec(
        path=entry.full_path,
        label=decl.label,
        widget=widget,
        type=decl.value_type,
        help=decl.help,
        readonly=True,
        dynamic=True,
        importance=importance,
    )


def _insert_after_enabled(section: SectionSpec, field_spec: FieldSpec) -> None:
    """Insert a dynamic field directly after the section's `enabled` field.

    Convention: every sub-feature config exposes an ``enabled`` toggle as
    its first scalar field. A "Status" view that reflects whether the
    sub-feature is currently working is most useful when it sits right
    next to that toggle, not buried at the bottom of the section. If
    the section has no ``enabled`` field, fall back to appending — the
    rule's relative ordering is then meaningless.
    """
    for i, f in enumerate(section.fields):
        if f.path.endswith(".enabled"):
            section.fields.insert(i + 1, field_spec)
            return
    section.fields.append(field_spec)


def _inject_dynamic_fields(
    sections: list[SectionSpec],
    module_path_prefix: str,
    entries: Iterable[DynamicFieldEntry],
) -> None:
    """Insert dynamic-field FieldSpecs into the matching nested sections.

    Plugins reference sections by *relative* id (e.g. "searcher"); the
    server prepends the module's full path prefix to find them in the
    auto-generated section tree. Each injected field lands directly
    after the section's ``enabled`` toggle (or at the end if none).
    """
    for entry in entries:
        decl = entry.decl
        if decl.section_id:
            target_id = f"{module_path_prefix}.{decl.section_id}"
        else:
            target_id = f"{module_path_prefix}.general"
        target = _find_section_by_id(sections, target_id)
        if target is None and decl.section_id:
            # Try the auto-generated 'general' bucket inside the named section
            target = _find_section_by_id(
                sections, f"{module_path_prefix}.{decl.section_id}.general"
            )
        if target is None:
            logger.warning(
                "Dynamic field %s declared section_id %r but no matching "
                "section was emitted under %s; field will not appear in the "
                "schema",
                entry.full_path,
                decl.section_id,
                module_path_prefix,
            )
            continue
        _insert_after_enabled(target, _dynamic_field_spec(entry))


def module_icon(config_cls: type) -> str | None:
    """The material icon name a module's config declares for it, if any: its
    settings card, and its badge wherever music from it is shown."""
    return getattr(config_cls, "__module_icon__", None)


def _module_spec(
    config: ModuleConfig,
    kind: str,
    *,
    path_prefix: str,
    dynamic_entries: Iterable[DynamicFieldEntry] = (),
) -> ModuleSpec:
    """Build the static schema entry for a module.

    Live state (READY/WARNING/ERROR + message + missing packages) is
    served separately by GET /server/modules; the schema deliberately
    omits it so schema_version stays stable across transient plugin
    state changes.

    A module's top-level scalar fields are hoisted to ``ModuleSpec.fields``
    so the client renders them as a flat list under the module header.
    Nested config models remain in ``sections``. The auto-generated
    "General" sub-section that the section walker emits for these
    scalars is dropped — these are essential settings, not something to
    bury in a foldable.
    """
    cls = config.__class__
    prefix = f"{path_prefix}.{config.name}"
    sections = _sections_for(config, prefix)

    _inject_dynamic_fields(sections, prefix, dynamic_entries)

    # After injection, so a dynamic field can target the general section.
    module_fields, module_collections, kept_sections = _split_general(
        sections, prefix
    )

    name_field = cls.model_fields["name"]
    title = name_field.title or config.name
    banners_raw = getattr(cls, "__module_banners__", [])
    banners = [b if isinstance(b, Banner) else Banner(**b) for b in banners_raw]

    return ModuleSpec(
        id=config.name,
        kind=kind,  # type: ignore[arg-type]
        title=title,
        description=name_field.description,
        icon=module_icon(cls),
        icon_color=getattr(cls, "__module_icon_color__", None),
        preview_fields=list(getattr(cls, "__preview_fields__", [])),
        banners=banners,
        fields=module_fields,
        collections=module_collections,
        sections=kept_sections,
    )


# ---------------------------------------------------------------------------
# Simple-view pruning
# ---------------------------------------------------------------------------


def _prune_sections_to_simple(sections: list[SectionSpec]) -> list[SectionSpec]:
    """Return a copy of ``sections`` containing only the SIMPLE-tier leaves.

    A section is dropped when either:

    * it is itself tagged ``importance=EXPERT`` (developer explicitly
      marked the entire group as expert-only — e.g. the "Buffers &
      decoders" section), OR
    * *all* its fields are EXPERT, it holds no collection, *and* every
      sub-section recursively prunes empty.

    A collection is always kept: the expert list has no editor for one, so
    this view is the only place it can be reached.

    Field order is preserved. The returned tree is fully new — callers
    retain ownership of the original (used to build the expert flat
    list).
    """
    kept: list[SectionSpec] = []
    for s in sections:
        if s.importance == Importance.EXPERT:
            continue
        simple_fields = [f for f in s.fields if f.importance == Importance.SIMPLE]
        pruned_children = _prune_sections_to_simple(s.sections)
        if not simple_fields and not s.collections and not pruned_children:
            continue
        kept.append(
            SectionSpec(
                id=s.id,
                title=s.title,
                icon=s.icon,
                importance=s.importance,
                banners=list(s.banners),
                fields=simple_fields,
                collections=_reanchor(s.collections, s.fields, simple_fields),
                sections=pruned_children,
            )
        )
    return kept


def _prune_module_to_simple(module: ModuleSpec) -> ModuleSpec:
    """Prune a module to its SIMPLE-tier surface.

    The module shell is *always* retained, even when every settable
    field below it is EXPERT — the enable toggle (and any other simple
    fields) must remain reachable so the user can switch the module on
    or off without entering expert mode.

    The module-level ``.enabled`` field is *also* always retained,
    regardless of importance. A plugin that forgot to tag its
    ``enabled`` field SIMPLE would otherwise lose the toggle entirely
    in the simple view, leaving the user with a card that displays a
    module but offers no way to turn it on or off. That's hostile, so
    we treat ``enabled`` as a guaranteed-simple field by contract — it
    matches what users expect, and plugin authors can't accidentally
    break it.
    """
    kept_fields: list[FieldSpec] = []
    for f in module.fields:
        if f.importance == Importance.SIMPLE or f.path.endswith(".enabled"):
            kept_fields.append(f)
    return ModuleSpec(
        id=module.id,
        kind=module.kind,
        title=module.title,
        description=module.description,
        icon=module.icon,
        icon_color=module.icon_color,
        preview_fields=list(module.preview_fields),
        banners=list(module.banners),
        fields=kept_fields,
        collections=_reanchor(module.collections, module.fields, kept_fields),
        sections=_prune_sections_to_simple(module.sections),
    )


def _prune_page_to_simple(page: PageSpec) -> PageSpec:
    return PageSpec(
        id=page.id,
        title=page.title,
        icon=page.icon,
        banners=list(page.banners),
        sections=_prune_sections_to_simple(page.sections),
        modules=[_prune_module_to_simple(m) for m in page.modules],
    )


# ---------------------------------------------------------------------------
# Flat expert-list collector
# ---------------------------------------------------------------------------


def _collect_fields_from_sections(
    sections: list[SectionSpec], out: list[FieldSpec]
) -> None:
    for s in sections:
        out.extend(s.fields)
        _collect_fields_from_sections(s.sections, out)


def _collect_fields_from_pages(pages: list[PageSpec]) -> list[FieldSpec]:
    """Walk the full (unpruned) page tree and return every leaf field.

    Dynamic (plugin-resolved, read-only) fields are excluded — the
    expert/about:config view is a *settable* surface, and dynamic
    fields are status displays that don't accept writes.

    The list is sorted by dotted path so the about:config search UI
    has a stable ordering it can paginate against. Duplicate paths
    are de-duplicated (a defensive guard; the auto/override section
    builders today don't emit the same path twice, but
    ``presentation_layout`` overrides could in principle).
    """
    bucket: list[FieldSpec] = []
    for p in pages:
        _collect_fields_from_sections(p.sections, bucket)
        for m in p.modules:
            bucket.extend(m.fields)
            _collect_fields_from_sections(m.sections, bucket)

    seen: set[str] = set()
    unique: list[FieldSpec] = []
    for f in bucket:
        if f.dynamic or f.path in seen:
            continue
        seen.add(f.path)
        unique.append(f)
    unique.sort(key=lambda f: f.path)
    return unique


# ---------------------------------------------------------------------------
# Flat values emitter
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfigValues:
    """What a client may know of the configuration.

    @note A credential is never among ``values``; ``secrets_set`` names the
        ones that hold something, which is all a client can learn of them.
    """

    values: dict[str, Any]
    secrets_set: frozenset[str]


def _flatten_values(
    model: BaseModel, prefix: str, out: dict[str, Any], secrets_set: set[str]
) -> None:
    for field_name, field in model.__class__.model_fields.items():
        if field.exclude:
            continue
        path = f"{prefix}.{field_name}" if prefix else field_name
        value = getattr(model, field_name, None)
        if isinstance(value, BaseModel):
            _flatten_values(value, path, out, secrets_set)
        elif is_secret(field):
            if value:
                secrets_set.add(path)
        elif is_record_list(field.annotation):
            out[path] = public_records(value, path, secrets_set)
        elif isinstance(value, Enum):
            out[path] = value.value
        else:
            out[path] = value


async def build_values(
    base_config: BaseModel,
    input_modules: dict[str, ModuleConfig],
    devices: dict[str, ModuleConfig],
    dynamic_entries: Iterable[DynamicFieldEntry] = (),
) -> ConfigValues:
    """Flat `{dotted_path: value}` for every settable + dynamic field bar the
    credentials, and which credentials are set.

    Static values come from the in-memory Pydantic config models.
    Dynamic values are resolved sequentially via the registry. All
    current resolvers read in-memory bookkeeping (no I/O), so parallel
    gather buys nothing over a plain for-loop. Failures are logged but
    omitted (the field appears with no value rather than failing the
    whole request).
    """
    known = build_static_values(base_config, input_modules, devices)
    for entry in dynamic_entries:
        value = await resolve_value(entry)
        if value is not None:
            known.values[entry.full_path] = value
    return known


def build_static_values(
    base_config: BaseModel,
    input_modules: dict[str, ModuleConfig],
    devices: dict[str, ModuleConfig],
) -> ConfigValues:
    """:func:`build_values` without the dynamic fields, which hold no
    credential: enough for ``secrets_set``, without asking any plugin."""
    out: dict[str, Any] = {}
    secrets_set: set[str] = set()
    _flatten_values(base_config, "base_config", out, secrets_set)
    for name, module in input_modules.items():
        _flatten_values(module, f"input_modules.{name}", out, secrets_set)
    for name, device in devices.items():
        _flatten_values(device, f"devices.{name}", out, secrets_set)
    return ConfigValues(values=out, secrets_set=frozenset(secrets_set))


async def build_enum_options(
    registry: OptionsRegistry,
) -> dict[str, list[OptionSpec]]:
    """Resolve every dynamic-options path in the registry.

    Returned as a flat ``{path: [OptionSpec, ...]}`` map that goes
    alongside ``values`` in the GET /server/config envelope. Paths
    whose resolver returned ``None`` (resolver missing, exception
    inside the resolver) are omitted, so the client sees no entry
    rather than an empty/half-populated list and can fall back to
    rendering the current value as a plain text row.
    """
    out: dict[str, list[OptionSpec]] = {}
    for path in registry.paths():
        options = await registry.resolve(path)
        if options is not None:
            out[path] = options
    return out


# ---------------------------------------------------------------------------
# Set/get by dotted path (no "root." / ".fields." prefixes)
# ---------------------------------------------------------------------------


def get_field_value(model: BaseModel, field_path: List[str]) -> Any:
    current = model
    for part in field_path:
        current = getattr(current, part)
    return current


# ---------------------------------------------------------------------------
# Top-level presentation schema
# ---------------------------------------------------------------------------


def _entries_for(
    registry: dict[str, DynamicFieldEntry],
    kind: str,
    plugin_id: str,
) -> list[DynamicFieldEntry]:
    return [e for e in registry.values() if e.kind == kind and e.plugin_id == plugin_id]


def build_presentation(
    base_config: BaseModel,
    input_modules: dict[str, ModuleConfig],
    devices: dict[str, ModuleConfig],
    *,
    input_modules_with_errors: dict[str, tuple[ModuleConfig, str]] | None = None,
    devices_with_errors: dict[str, tuple[ModuleConfig, str]] | None = None,
    dynamic_field_registry: dict[str, DynamicFieldEntry] | None = None,
) -> PresentationSchema:
    input_modules_with_errors = input_modules_with_errors or {}
    devices_with_errors = devices_with_errors or {}
    registry = dynamic_field_registry or {}

    # General page: let KalinkaConfig.presentation_layout() shape the sections
    general_sections = _sections_for(base_config, "base_config")
    general_banners_raw = getattr(base_config.__class__, "__page_banners__", [])
    general_banners = [
        b if isinstance(b, Banner) else Banner(**b) for b in general_banners_raw
    ]

    general_page = PageSpec(
        id="general",
        title="General",
        banners=general_banners,
        sections=general_sections,
    )

    # Modules page
    module_specs: list[ModuleSpec] = []
    for name, m in input_modules.items():
        module_specs.append(
            _module_spec(
                m,
                kind="input_module",
                path_prefix="input_modules",
                dynamic_entries=_entries_for(registry, "input_module", name),
            )
        )
    for name, (m, _err) in input_modules_with_errors.items():
        module_specs.append(
            _module_spec(m, kind="input_module", path_prefix="input_modules")
        )

    modules_page = PageSpec(id="modules", title="Input modules", modules=module_specs)

    # Devices page
    device_specs: list[ModuleSpec] = []
    for name, d in devices.items():
        device_specs.append(
            _module_spec(
                d,
                kind="device",
                path_prefix="devices",
                dynamic_entries=_entries_for(registry, "device", name),
            )
        )
    for name, (d, _err) in devices_with_errors.items():
        device_specs.append(_module_spec(d, kind="device", path_prefix="devices"))

    devices_page = PageSpec(
        id="devices",
        title="Devices",
        banners=[
            Banner(
                text=(
                    "Devices are auto-discovered. Configuration changes require a "
                    "server restart."
                ),
                severity=Severity.INFO,
            )
        ],
        modules=device_specs,
    )

    full_pages = [general_page, modules_page, devices_page]

    # Build the two views from the same source tree:
    #   * pages — pruned to SIMPLE; structured navigation in the default UI.
    #   * expert_fields — every leaf (SIMPLE + EXPERT), flat and sorted,
    #     for about:config-style search. Dynamic (read-only) fields are
    #     excluded — they live in the simple view only as status displays.
    simple_pages = [_prune_page_to_simple(p) for p in full_pages]
    expert_fields = _collect_fields_from_pages(full_pages)

    # schema_version: stable hash over BOTH views — moving a field
    # between tiers invalidates both surfaces at once.
    blob = json.dumps(
        {
            "pages": [p.model_dump(mode="json") for p in simple_pages],
            "expert_fields": [f.model_dump(mode="json") for f in expert_fields],
        },
        sort_keys=True,
        default=str,
    )
    schema_version = hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]

    return PresentationSchema(
        schema_version=schema_version,
        pages=simple_pages,
        expert_fields=expert_fields,
    )
