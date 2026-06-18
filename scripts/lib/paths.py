"""Filesystem path resolution for Orbit (per-user, never hardcoded).

Orbit is per-user from day one (brief §1 / conventions.md §Config & secrets), so
the SQLite state file lives under the user's XDG data directory rather than a
single hardcoded absolute path. This module centralizes that resolution so every
module agrees on where the DB lives.
"""

from __future__ import annotations

import os
from pathlib import Path

# Subdirectory + filename for Orbit's state DB, appended to whichever base
# directory the resolution order below selects.
ORBIT_DB_SUBDIR: str = "orbit"
ORBIT_DB_FILENAME: str = "orbit.db"

# Environment variable that, if set, overrides all other resolution (used by
# tests and power users to point Orbit at an alternate DB file).
ORBIT_DB_PATH_ENV_VAR: str = "ORBIT_DB_PATH"

# Environment variable for the XDG base data directory (freedesktop spec).
XDG_DATA_HOME_ENV_VAR: str = "XDG_DATA_HOME"


def resolve_db_path() -> Path:
    """Resolve the absolute path to Orbit's SQLite state file, creating its parent dir.

    Resolution order (first match wins):
        1. ``ORBIT_DB_PATH`` env var, used verbatim if set (test / power-user override).
        2. ``$XDG_DATA_HOME/orbit/orbit.db`` if ``XDG_DATA_HOME`` is set.
        3. ``~/.local/share/orbit/orbit.db`` (XDG default).

    The parent directory is created (``parents=True, exist_ok=True``) so callers
    can open the DB immediately. This keeps Orbit per-user — the path is derived
    from the environment / home directory, never a hardcoded single location.

    Returns:
        The resolved absolute path to ``orbit.db``.

    Example:
        >>> import os
        >>> os.environ["ORBIT_DB_PATH"] = "/tmp/orbit_test/orbit.db"
        >>> str(resolve_db_path())
        '/tmp/orbit_test/orbit.db'
    """
    db_path_override = os.environ.get(ORBIT_DB_PATH_ENV_VAR)
    if db_path_override:
        resolved_db_path = Path(db_path_override).expanduser()
    else:
        xdg_data_home = os.environ.get(XDG_DATA_HOME_ENV_VAR)
        if xdg_data_home:
            base_data_dir = Path(xdg_data_home).expanduser()
        else:
            base_data_dir = Path.home() / ".local" / "share"
        resolved_db_path = base_data_dir / ORBIT_DB_SUBDIR / ORBIT_DB_FILENAME

    resolved_db_path.parent.mkdir(parents=True, exist_ok=True)
    return resolved_db_path
