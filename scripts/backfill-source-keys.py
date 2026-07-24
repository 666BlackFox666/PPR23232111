from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import get_settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.excel.source_key_backfill import apply_backfill_plan, build_backfill_plan  # noqa: E402


def write_report(payload: dict) -> Path:
    runtime_dir = ROOT_DIR / ".runtime"
    runtime_dir.mkdir(exist_ok=True)
    path = runtime_dir / f"source-key-backfill-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Preview or apply safe source_key backfill.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", action="store_true", help="Show planned source_key changes without writing.")
    mode.add_argument("--apply", action="store_true", help="Apply planned source_key changes.")
    parser.add_argument("--confirm", action="store_true", help="Required with --apply.")
    parser.add_argument("--excel", default=get_settings().schedule_xlsx_path, help="Excel file to match against ppr_events.")
    args = parser.parse_args()

    if args.apply and not args.confirm:
        print("ERROR: --apply requires --confirm", file=sys.stderr)
        return 2

    excel_path = Path(args.excel)
    if not excel_path.exists():
        print(f"ERROR: Excel file not found: {excel_path}", file=sys.stderr)
        return 2
    content = excel_path.read_bytes()

    with SessionLocal() as db:
        plan = build_backfill_plan(db, content)
        print(json.dumps({"summary": plan["summary"]}, ensure_ascii=False, indent=2))
        if args.preview:
            report = write_report({"mode": "preview", **plan})
            print(f"Report written: {report}")
            return 0

        try:
            result = apply_backfill_plan(db, plan)
            db.commit()
        except Exception:
            db.rollback()
            raise
        report = write_report({"mode": "apply", "result": result, **plan})
        print(json.dumps({"result": result, "report": str(report)}, ensure_ascii=False, indent=2))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
