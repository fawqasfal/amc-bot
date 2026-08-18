"""Opt-in, private local storage for the saved-card CVV."""

from __future__ import annotations

import os
import platform
import tempfile
from pathlib import Path


def default_cvv_path() -> Path:
    """Return the per-user CVV path, with an environment override for tests."""

    override = os.environ.get("AMC_WATCHER_DATA_DIR")
    if override:
        return Path(override).expanduser() / "payment_cvv"
    if platform.system() == "Darwin":
        root = Path.home() / "Library" / "Application Support" / "AMC Ticket Watcher"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        root /= "amc-ticket-watcher"
    return root / "payment_cvv"


class LocalCvvStore:
    """Store one CVV in a user-only file when the user explicitly opts in."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_cvv_path()

    def load(self) -> str | None:
        if not self.path.exists():
            return None
        if self.path.is_symlink():
            raise OSError("refusing to read a symlinked CVV file")
        value = self.path.read_text(encoding="ascii").strip()
        if not value.isdigit() or len(value) not in {3, 4}:
            raise OSError("saved CVV file is invalid")
        return value

    def save(self, cvv: str) -> None:
        if not cvv.isdigit() or len(cvv) not in {3, 4}:
            raise ValueError("CVV must contain 3 or 4 digits")
        directory = self.path.parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        if self.path.exists() and self.path.is_symlink():
            raise OSError("refusing to replace a symlinked CVV file")

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=directory
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                stream.write(cvv)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o600)
            temporary.replace(self.path)
            self.path.chmod(0o600)
        finally:
            if temporary.exists():
                temporary.unlink()

    def delete(self) -> None:
        if not self.path.exists():
            return
        if self.path.is_symlink():
            raise OSError("refusing to delete a symlinked CVV file")
        self.path.unlink()
