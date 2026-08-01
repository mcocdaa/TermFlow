"""Validated tmux Session, Window, and Pane snapshots."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

SessionId = Annotated[str, StringConstraints(pattern=r"^\$[0-9]+$")]
WindowId = Annotated[str, StringConstraints(pattern=r"^@[0-9]+$")]
PaneId = Annotated[str, StringConstraints(pattern=r"^%[0-9]+$")]


class PaneSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pane_id: PaneId
    window_id: WindowId
    index: int = Field(ge=0)
    title: str
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    left: int = Field(default=0, ge=0)
    top: int = Field(default=0, ge=0)
    current_command: str | None = None
    active: bool
    dead: bool


class WindowSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    window_id: WindowId
    index: int = Field(ge=0)
    name: str
    active: bool
    panes: list[PaneSnapshot]

    @model_validator(mode="after")
    def pane_windows_match(self) -> "WindowSnapshot":
        if any(pane.window_id != self.window_id for pane in self.panes):
            raise ValueError("every Pane must belong to this Window")
        if len({pane.pane_id for pane in self.panes}) != len(self.panes):
            raise ValueError("Pane IDs must be unique within a Window")
        return self


class TopologySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: SessionId
    session_name: str
    revision: int = Field(ge=0)
    windows: list[WindowSnapshot]

    @model_validator(mode="after")
    def window_ids_are_unique(self) -> "TopologySnapshot":
        if len({window.window_id for window in self.windows}) != len(self.windows):
            raise ValueError("Window IDs must be unique")
        pane_ids = [pane.pane_id for window in self.windows for pane in window.panes]
        if len(set(pane_ids)) != len(pane_ids):
            raise ValueError("Pane IDs must be unique")
        return self

    def contains_pane(self, pane_id: str) -> bool:
        return any(pane.pane_id == pane_id for window in self.windows for pane in window.panes)
