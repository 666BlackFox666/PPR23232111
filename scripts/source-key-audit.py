from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import get_settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.excel.source_key_backfill import source_key_audit  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only source_key audit for PPR events.")
    parser.add_argument("--excel", default=get_settings().schedule_xlsx_path, help="Excel file used for match diagnostics.")
    args = parser.parse_args()

    excel_path = Path(args.excel)
    content = excel_path.read_bytes() if excel_path.exists() else None
    with SessionLocal() as db:
        result = source_key_audit(db, content)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
