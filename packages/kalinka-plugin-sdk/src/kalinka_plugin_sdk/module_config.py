from pydantic import BaseModel, Field, model_validator
import uuid


class ModuleConfig(BaseModel):
    """
    This base class provides essential configuration fields for all input modules in the Kalinka system.

    It ensures that every module configuration inherits a unique name (generated automatically if not provided)
    and an enabled flag, promoting consistency and reusability across the SDK.

    All additional module configurations must inherit from this class to guarantee these core fields are available
    and properly managed, preventing configuration inconsistencies and ensuring seamless integration.
    """

    name: str = Field(
        default_factory=lambda: f"module_{uuid.uuid4().hex[:8]}",
        title="Kalinka Module Name",
        frozen=True,
        exclude=True,
    )
    # Tagged SIMPLE so it appears on the structured settings page by
    # default. The enable toggle is the one control a user must always
    # reach to wire a module on or off — burying it behind the
    # about:config search would be hostile. Plugins that override this
    # field should keep the simple tag.
    enabled: bool = Field(
        default=True,
        title="Module enabled",
        json_schema_extra={"importance": "simple"},
    )

    def reconcile(self, written: frozenset[str]) -> None:
        """Bring the fields that follow others back in line with them.

        One field can mirror another in a shape an older app still edits.
        Whichever of the two a client writes, this makes the other agree, so
        every client reads them in agreement.

        @param written The dotted paths just set: by the server after it
            applies the overrides or a client's changes, or the fields given
            on construction.
        @note Runs on the copy a dry run judges as well, so it computes only.
            The server stores what it changes as it stores what was written.
        """

    @model_validator(mode="after")
    def _reconcile_given(self) -> "ModuleConfig":
        self.reconcile(frozenset(self.model_fields_set))
        return self
