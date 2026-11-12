from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TypeVar, dataclass_transform

type PathLike = str | Path

_T = TypeVar("_T")


@dataclass_transform()
def model(*, frozen: bool = True) -> Callable[[type[_T]], type[_T]]:
    """Dataclass-style decorator with type-checker support."""

    def decorate(cls: type[_T]) -> type[_T]:
        return dataclass(frozen=frozen)(cls)  # type: ignore[return-value]

    return decorate
