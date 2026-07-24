from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.db.session import SessionLocal
from app.excel.import_service import read_file_bytes
from app.services.schedule_replacement_service import (
    REPLACE_CONFIRMATION,
    ScheduleReplacementError,
    ScheduleValidationError,
    build_schedule_replacement_preview,
    export_validation_report,
    replace_schedule_from_excel,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Safely replace all Excel-managed PPR schedule data.")
    result.add_argument("--file", required=True, help="Path to the complete future schedule Excel file")
    action = result.add_mutually_exclusive_group(required=False)
    action.add_argument("--preview", action="store_true", help="Validate and show the replacement plan without changes")
    action.add_argument("--apply", action="store_true", help="Create backup and replace the schedule")
    result.add_argument("--export-validation-report", help="Write an XLSX validation report without changing DB or creating backup")
    result.add_argument("--confirm", default="", help=f"Required with --apply: {REPLACE_CONFIRMATION}")
    return result


def print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not args.preview and not args.apply and not args.export_validation_report:
        print_json({"error": "Specify --preview, --apply, or --export-validation-report"})
        return 2
    if args.apply and args.export_validation_report:
        print_json({"error": "Validation report export cannot be combined with --apply"})
        return 2
    path = Path(args.file)
    if not path.is_file():
        print_json({"error": f"Excel file was not found: {path}"})
        return 2
    try:
        filename, content = read_file_bytes(path)
    except OSError as exc:
        print_json({"error": f"Cannot read Excel file: {exc}"})
        return 2

    with SessionLocal() as db:
        try:
            if args.export_validation_report:
                preview = build_schedule_replacement_preview(db, content, filename)
                report_path = export_validation_report(content, args.export_validation_report, preview)
                print_json({"report_path": str(report_path), "preview": preview})
                return 0
            if args.preview:
                preview = build_schedule_replacement_preview(db, content, filename)
                print_json(preview)
                return 0 if not preview["errors"] else 2
            result = replace_schedule_from_excel(db, content, filename, confirmation=args.confirm)
            print_json(result)
            return 0
        except ScheduleValidationError as exc:
            print_json({"error": str(exc), "preview": exc.preview})
            return 2
        except ScheduleReplacementError as exc:
            print_json({"error": str(exc)})
            return 2
        except Exception as exc:
            print_json({"error": f"Unexpected replacement failure: {exc.__class__.__name__}: {exc}"})
            return 1


if __name__ == "__main__":
    sys.exit(main())
