from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import PprEvent
from app.excel.import_service import (
    KEY_METHOD_FINGERPRINT,
    KEY_METHOD_ID,
    KEY_METHOD_SOURCE,
    ParsedExcelRow,
    build_import_preview,
    event_fingerprint_source_key,
    is_legacy_row_source_key,
    is_manual_event,
    parse_excel_content,
)


@dataclass
class SourceKeyPlanItem:
    excel_row_number: int
    ppr_event_id: int | None
    title: str
    project: str | None
    current_source_key: str | None
    new_source_key: str
    key_method: str
    match_method: str
    action: str
    reason: str
    candidate_ids: list[int]
    differing_fields: dict[str, dict[str, Any]]

    def as_dict(self) -> dict:
        return {
            "excel_row_number": self.excel_row_number,
            "ppr_event_id": self.ppr_event_id,
            "title": self.title,
            "project": self.project,
            "current_source_key": self.current_source_key,
            "new_source_key": self.new_source_key,
            "key_method": self.key_method,
            "match_method": self.match_method,
            "action": self.action,
            "reason": self.reason,
            "candidate_ids": self.candidate_ids,
            "differing_fields": self.differing_fields,
        }


def duplicate_value_count(db: Session, column) -> int:
    rows = (
        db.query(column)
        .filter(column.isnot(None))
        .group_by(column)
        .having(func.count() > 1)
        .all()
    )
    return len(rows)


def source_key_prefix_counts(db: Session) -> dict[str, int]:
    events = db.query(PprEvent.source_key).all()
    counts = {
        "source_key_null": 0,
        "source_key_row": 0,
        "source_key_id": 0,
        "source_key_source": 0,
        "source_key_fingerprint": 0,
        "source_key_other": 0,
    }
    for (source_key,) in events:
        if source_key is None:
            counts["source_key_null"] += 1
        elif source_key.startswith("row:"):
            counts["source_key_row"] += 1
        elif source_key.startswith("id:"):
            counts["source_key_id"] += 1
        elif source_key.startswith("source:"):
            counts["source_key_source"] += 1
        elif source_key.startswith("fingerprint:"):
            counts["source_key_fingerprint"] += 1
        else:
            counts["source_key_other"] += 1
    return counts


def compare_candidate(row: ParsedExcelRow, event: PprEvent) -> dict[str, dict[str, Any]]:
    result = {}
    for field in ("title", "project", "date", "start_time", "notification_type", "responsible_report"):
        old = getattr(event, field)
        new = row.values.get(field)
        if old != new:
            result[field] = {
                "db": old.isoformat() if hasattr(old, "isoformat") else old,
                "excel": new.isoformat() if hasattr(new, "isoformat") else new,
            }
    return result


def unique_or_conflict(candidates: list[PprEvent]) -> tuple[PprEvent | None, str]:
    by_id = {event.id: event for event in candidates if not is_manual_event(event)}
    if len(by_id) == 1:
        return next(iter(by_id.values())), "ok"
    if len(by_id) == 0:
        return None, "not_found"
    return None, "ambiguous"


def fingerprint_candidates(db: Session, source_key: str) -> list[PprEvent]:
    candidates = []
    events = db.query(PprEvent).filter(PprEvent.external_id.notlike("MANUAL-%")).all()
    for event in events:
        if event.source_key == source_key:
            candidates.append(event)
        elif event.source_key is None and event_fingerprint_source_key(event) == source_key:
            candidates.append(event)
        elif is_legacy_row_source_key(event.source_key) and event_fingerprint_source_key(event) == source_key:
            candidates.append(event)
    return candidates


def build_backfill_plan(db: Session, content: bytes) -> dict:
    rows, invalid_rows, total_rows = parse_excel_content(content)
    excel_source_key_counts = Counter(row.source_key for row in rows)
    details: list[SourceKeyPlanItem] = []

    for row in rows:
        candidates: list[PprEvent] = []
        match_method = "none"

        if row.key_method == KEY_METHOD_ID:
            candidates = db.query(PprEvent).filter(PprEvent.external_id == row.external_id).all()
            match_method = "external_id"
        elif row.key_method == KEY_METHOD_SOURCE:
            candidates = db.query(PprEvent).filter(PprEvent.source_row == row.values["source_row"]).all()
            match_method = "source_row"
        elif row.key_method == KEY_METHOD_FINGERPRINT:
            if excel_source_key_counts[row.source_key] > 1:
                candidates = fingerprint_candidates(db, row.source_key)
                details.append(
                    SourceKeyPlanItem(
                        excel_row_number=row.excel_row_number,
                        ppr_event_id=None,
                        title=row.title,
                        project=row.project,
                        current_source_key=None,
                        new_source_key=row.source_key,
                        key_method=row.key_method,
                        match_method="ambiguous",
                        action="skip",
                        reason="fingerprint не уникален внутри Excel",
                        candidate_ids=[item.id for item in candidates],
                        differing_fields={
                            str(item.id): compare_candidate(row, item)
                            for item in candidates
                        },
                    )
                )
                continue
            candidates = fingerprint_candidates(db, row.source_key)
            match_method = "fingerprint"

        event, status = unique_or_conflict(candidates)
        if status != "ok" or event is None:
            details.append(
                SourceKeyPlanItem(
                    excel_row_number=row.excel_row_number,
                    ppr_event_id=None,
                    title=row.title,
                    project=row.project,
                    current_source_key=None,
                    new_source_key=row.source_key,
                    key_method=row.key_method,
                    match_method="ambiguous" if status == "ambiguous" else match_method,
                    action="skip",
                    reason="нет однозначного совпадения" if status == "not_found" else "найдено несколько кандидатов",
                    candidate_ids=[item.id for item in candidates],
                    differing_fields={},
                )
            )
            continue

        action = "noop" if event.source_key == row.source_key else "update_source_key"
        details.append(
            SourceKeyPlanItem(
                excel_row_number=row.excel_row_number,
                ppr_event_id=event.id,
                title=row.title,
                project=row.project,
                current_source_key=event.source_key,
                new_source_key=row.source_key,
                key_method=row.key_method,
                match_method=match_method,
                action=action,
                reason="source_key уже актуален" if action == "noop" else "source_key можно безопасно обновить",
                candidate_ids=[event.id],
                differing_fields=compare_candidate(row, event),
            )
        )

    summary = {
        "total_rows": total_rows,
        "invalid_rows": len(invalid_rows),
        "updates": sum(1 for item in details if item.action == "update_source_key"),
        "noop": sum(1 for item in details if item.action == "noop"),
        "skipped": sum(1 for item in details if item.action == "skip"),
        "matched_by_external_id": sum(1 for item in details if item.match_method == "external_id" and item.ppr_event_id),
        "matched_by_source_row": sum(1 for item in details if item.match_method == "source_row" and item.ppr_event_id),
        "matched_by_fingerprint": sum(1 for item in details if item.match_method == "fingerprint" and item.ppr_event_id),
        "ambiguous": sum(1 for item in details if item.match_method == "ambiguous"),
    }
    return {
        "summary": summary,
        "details": [item.as_dict() for item in details],
        "invalid_rows": invalid_rows,
    }


def source_key_audit(db: Session, content: bytes | None = None) -> dict:
    total = db.query(PprEvent).count()
    manual_no_source_key = (
        db.query(PprEvent)
        .filter(PprEvent.external_id.like("MANUAL-%"), PprEvent.source_key.is_(None))
        .count()
    )
    counts = source_key_prefix_counts(db)
    duplicate_external_id = duplicate_value_count(db, PprEvent.external_id)
    duplicate_source_key = duplicate_value_count(db, PprEvent.source_key)

    result = {
        "total_ppr_events": total,
        "manual_ppr_without_source_key": manual_no_source_key,
        **counts,
        "duplicate_external_id": duplicate_external_id,
        "duplicate_source_key": duplicate_source_key,
        "conflicting_fingerprint": 0,
        "safe_excel_matches": None,
    }
    if content is not None:
        plan = build_backfill_plan(db, content)
        result["safe_excel_matches"] = plan["summary"]["updates"] + plan["summary"]["noop"]
        result["conflicting_fingerprint"] = sum(
            1 for item in plan["details"] if item["key_method"] == KEY_METHOD_FINGERPRINT and item["action"] == "skip"
        )
        result["backfill_summary"] = plan["summary"]
    return result


def apply_backfill_plan(db: Session, plan: dict) -> dict:
    updated = 0
    skipped = 0
    for item in plan["details"]:
        if item["action"] != "update_source_key" or not item["ppr_event_id"]:
            skipped += 1
            continue
        event = db.get(PprEvent, item["ppr_event_id"])
        if event is None or is_manual_event(event):
            skipped += 1
            continue
        if event.source_key == item["new_source_key"]:
            skipped += 1
            continue
        event.source_key = item["new_source_key"]
        updated += 1
    return {"updated": updated, "skipped": skipped}
