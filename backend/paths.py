from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping


class PathLocationError(ValueError):
    """Raised when a configured root cannot safely represent a path."""


@dataclass(frozen=True, slots=True)
class RootLocation:
    root_key: str
    relative_path: str


def _is_within(child: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath(
            (os.path.normcase(str(child)), os.path.normcase(str(parent)))
        ) == os.path.normcase(str(parent))
    except ValueError:
        return False


def _clean_relative(value: str | Path) -> PurePosixPath:
    text = str(value).replace("\\", "/")
    relative = PurePosixPath(text)
    if relative.is_absolute() or not relative.parts:
        raise PathLocationError("relative path must be non-empty and relative")
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise PathLocationError("relative path may not contain '.', '..', or empty segments")
    return relative


class RootMapper:
    """Resolve persisted root keys without trusting arbitrary absolute paths."""

    def __init__(self, roots: Mapping[str, str | Path]):
        normalized: dict[str, Path] = {}
        for key, value in roots.items():
            name = str(key).strip().lower()
            if not name:
                raise PathLocationError("root key may not be empty")
            normalized[name] = Path(value).expanduser().resolve(strict=False)
        self._roots = normalized

    @property
    def roots(self) -> dict[str, Path]:
        return dict(self._roots)

    def resolve(self, root_key: str, relative_path: str | Path, *, must_exist: bool = False) -> Path:
        key = str(root_key).strip().lower()
        if key not in self._roots:
            raise PathLocationError(f"unknown root key: {root_key}")
        relative = _clean_relative(relative_path)
        root = self._roots[key]
        candidate = root.joinpath(*relative.parts).resolve(strict=must_exist)
        if not _is_within(candidate, root):
            raise PathLocationError(f"resolved path escapes root '{key}'")
        return candidate

    def relativize(
        self,
        path: str | Path,
        *,
        allowed_keys: set[str] | None = None,
        must_exist: bool = False,
    ) -> RootLocation:
        candidate = Path(path).expanduser().resolve(strict=must_exist)
        allowed = {item.lower() for item in allowed_keys} if allowed_keys else None
        matches: list[tuple[int, str, Path]] = []
        for key, root in self._roots.items():
            if allowed is not None and key not in allowed:
                continue
            if _is_within(candidate, root):
                matches.append((len(str(root)), key, root))
        if not matches:
            raise PathLocationError(f"path is outside configured roots: {candidate}")
        _, key, root = max(matches)
        relative_text = os.path.relpath(candidate, root).replace("\\", "/")
        relative = _clean_relative(relative_text)
        resolved = self.resolve(key, relative.as_posix(), must_exist=must_exist)
        if os.path.normcase(str(resolved)) != os.path.normcase(str(candidate)):
            raise PathLocationError("root mapping round-trip changed the resolved path")
        return RootLocation(key, relative.as_posix())


__all__ = ["PathLocationError", "RootLocation", "RootMapper"]
