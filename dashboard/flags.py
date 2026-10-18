"""Marker files: the dashboard's one way to read and write a flag on disk.

The pipeline talks to its helpers through empty (or one-line) files -- the
pause flag, `.push-off`, `.push-hold`, `.transfers-paused`, a title's
`.push-failed-<title>`. Each used to carry its own copy of the same three
moves; this is the one copy. Dashboard only: `pipeline/core.py` keeps its own
`paused()` because the decision path must not import the dashboard.
"""

import os
from typing import Callable, Optional, Union


class Flag:
    """A marker file. `path` may be a callable so a patched `core.X9` is read
    at call time rather than frozen at import."""

    def __init__(self, path: Union[str, Callable[[], str]]):
        self._path = path

    @property
    def path(self) -> str:
        return self._path() if callable(self._path) else self._path

    def is_set(self) -> bool:
        return os.path.exists(self.path)

    def set(self, on: bool = True) -> None:
        """Idempotent both ways; raises OSError only if the write itself fails."""
        if on:
            with open(self.path, "a", encoding="utf-8"):
                pass
        else:
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass

    def note(self) -> Optional[str]:
        """First line of the file, or None when it is absent or unreadable.
        An empty file answers "" -- present, with nothing to say."""
        try:
            with open(self.path, encoding="utf-8") as fh:
                return fh.readline().strip()
        except OSError:
            return None
