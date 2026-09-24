"""Copy Railway SQLite accounts into Appwrite without deleting the source."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db


def main() -> int:
    print(__import__('json').dumps(db.migrate_sqlite_to_appwrite(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
