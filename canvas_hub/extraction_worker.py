"""Disposable local document converter. No browser or network client is created."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .db import Database
from .paths import AppPaths
from .services.resources import ResourceService, validate_resource_id


def main():
    request_path, result_path = map(Path, sys.argv[1:])
    request = json.loads(request_path.read_text(encoding="utf-8"))
    db = Database(request["db"])
    try:
        service = ResourceService(db, AppPaths(Path(request["home"])))
        row = db.one("SELECT * FROM resources WHERE id=?", (validate_resource_id(request["sha"]),))
        if row is None:
            raise ValueError("resource not found")
        result = service._convert(row, request["filename"])
        result_path.write_text(json.dumps(result), encoding="utf-8")
    finally:
        db.close()


if __name__ == "__main__":
    main()
