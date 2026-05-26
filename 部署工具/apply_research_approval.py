"""Apply manual research approval records to the local research memory.

The review dashboard is static HTML, so it deliberately does not write files.
Paste or save one JSON / JSONL approval record from the dashboard, then run:

    python apply_research_approval.py --input approval.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name == "部署工具" else SCRIPT_DIR
MEMORY_DIR = ROOT / "research_memory"
CST = timezone(timedelta(hours=8))


def read_records(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return []
    if text.startswith("["):
        data = json.loads(text)
        return [row for row in data if isinstance(row, dict)]
    if text.startswith("{") and "\n{" not in text:
        data = json.loads(text)
        return [data] if isinstance(data, dict) else []
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if isinstance(row, dict):
            rows.append(row)
    return rows


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def candidate_status_for(action: str) -> tuple[str, str]:
    mapping = {
        "approve_shadow": ("approved_for_small_live", "approved"),
        "observe": ("observe", "active"),
        "reject": ("manual_reject", "rejected"),
        "archive_family": ("archived", "archived"),
    }
    return mapping.get(action, ("observe", "active"))


def apply_to_candidates(memory_dir: Path, records: list[dict[str, Any]]) -> int:
    path = memory_dir / "hypotheses" / "candidates_latest.jsonl"
    rows = read_jsonl(path)
    if not rows:
        return 0

    by_candidate = {str(r.get("candidate_id") or ""): r for r in records if r.get("candidate_id")}
    by_experiment = {str(r.get("experiment_id") or ""): r for r in records if r.get("experiment_id")}
    by_family = {str(r.get("family_id") or ""): r for r in records if r.get("family_id")}
    archive_families = {
        str(r.get("family_id") or "")
        for r in records
        if str(r.get("manual_action") or "") == "archive_family" and r.get("family_id")
    }
    changed = 0
    for row in rows:
        cid = str(row.get("candidate_id") or "")
        experiment_id = str(row.get("experiment_id") or "")
        family_id = str(row.get("family_id") or "")
        record = by_candidate.get(cid) or by_experiment.get(experiment_id) or by_family.get(family_id)
        if not record and family_id in archive_families:
            record = {"manual_action": "archive_family"}
        if not record:
            continue
        status, governance = candidate_status_for(str(record.get("manual_action") or "observe"))
        row["status"] = status
        row["governance_status"] = governance
        row["manual_action"] = record.get("manual_action")
        row["approved_scope"] = record.get("approved_scope")
        row["approval_updated_at"] = datetime.now(CST).isoformat(timespec="seconds")
        changed += 1
    if changed:
        write_jsonl(path, rows)
    return changed


def apply_to_reviews(memory_dir: Path, records: list[dict[str, Any]]) -> int:
    review_paths = sorted((memory_dir / "promotions").glob("reviews*.jsonl"))
    by_candidate = {str(r.get("candidate_id") or ""): r for r in records if r.get("candidate_id")}
    by_experiment = {str(r.get("experiment_id") or ""): r for r in records if r.get("experiment_id")}
    by_family = {str(r.get("family_id") or ""): r for r in records if r.get("family_id")}
    changed = 0
    for path in review_paths:
        rows = read_jsonl(path)
        path_changed = False
        for row in rows:
            cid = str(row.get("candidate_id") or "")
            experiment_id = str(row.get("experiment_id") or "")
            family_id = str(row.get("family_id") or "")
            record = by_candidate.get(cid) or by_experiment.get(experiment_id) or by_family.get(family_id)
            if not record:
                continue
            status, governance = candidate_status_for(str(record.get("manual_action") or "observe"))
            row["promotion_status"] = status
            row["decision"] = "manual_approved_small_live" if status == "approved_for_small_live" else status
            row["manual_review_required"] = False if status == "approved_for_small_live" else row.get("manual_review_required", True)
            row["manual_action"] = record.get("manual_action")
            row["approved_scope"] = record.get("approved_scope")
            row["approval_updated_at"] = record.get("applied_at") or datetime.now(CST).isoformat(timespec="seconds")
            row["governance_status"] = governance
            if record.get("risk_notes"):
                row["risk_notes"] = record.get("risk_notes")
            if record.get("next_step"):
                row["next_step"] = record.get("next_step")
            path_changed = True
            changed += 1
        if path_changed:
            write_jsonl(path, rows)
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="应用研究审阅台的人工审批记录")
    parser.add_argument("--input", required=True, help="审批 JSON 或 JSONL 文件")
    parser.add_argument("--memory-dir", default=str(MEMORY_DIR))
    args = parser.parse_args(argv)

    memory_dir = Path(args.memory_dir)
    records = read_records(Path(args.input))
    now = datetime.now(CST).isoformat(timespec="seconds")
    for record in records:
        record.setdefault("applied_at", now)
    append_jsonl(memory_dir / "approvals" / "manual_actions.jsonl", records)
    write_jsonl(memory_dir / "approvals" / "manual_actions_latest.jsonl", records)
    changed = apply_to_candidates(memory_dir, records)
    changed_reviews = apply_to_reviews(memory_dir, records)
    print(f"审批记录已写入: {memory_dir / 'approvals' / 'manual_actions.jsonl'}")
    print(f"records={len(records)} updated_candidates={changed} updated_reviews={changed_reviews}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
