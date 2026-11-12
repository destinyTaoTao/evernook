"""Optional pinned C distance over existing vectors; no derived storage or downloader."""
from __future__ import annotations

import importlib
import sqlite3
import struct
from collections.abc import Callable

from .embedding import unit_vector


def distance_function(db: sqlite3.Connection, vector: tuple[float, ...], *,
                      check: Callable[[], float] | None = None) -> tuple[str, bytes, str]:
    encoded = struct.pack(f"<{len(vector)}f", *vector)
    try:
        extension = importlib.import_module("sqlite_vec")
        # Only the packaged local extension is considered; callers cannot supply a library path.
        db.enable_load_extension(True)
        try:
            extension.load(db)
        finally:
            db.enable_load_extension(False)
        if db.execute("SELECT vec_version()").fetchone()[0] == "v0.1.9":
            return "vec_distance_cosine", encoded, "sqlite-vec-0.1.9-exact"
    except (ImportError, OSError, AttributeError, sqlite3.Error):
        pass

    def cosine(raw: bytes, _: bytes) -> float:
        # The connection's SQLite progress handler preserves the original cancellation error.
        # Raising it inside a UDF would instead be wrapped as a generic SQLite callback failure.
        values = unit_vector(struct.unpack(f"<{len(vector)}f", raw), len(vector))
        return 1 - sum(a * b for a, b in zip(vector, values))

    db.create_function("evernook_cosine", 2, cosine)
    return "evernook_cosine", encoded, "python-exact"
