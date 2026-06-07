"""Maintain a durable attention ledger for decision-critical items."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sqlite3
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
RUNTIME_DIR = ROOT / "runtime"
REPORTS_DIR = ROOT / "reports"
ATTENTION_DIR = ROOT / "research_memory" / "attention"
APPROVAL_DIR = ROOT / "research_memory" / "approvals"
ATTENTION_JSON = ATTENTION_DIR / "open_items.json"
ATTENTION_ACK_JSONL = ATTENTION_DIR / "acknowledgements.jsonl"
ATTENTION_MD = REPORTS_DIR / "decision_attention_latest.md"
ATTENTION_HTML = REPORTS_DIR / "decision_attention_latest.html"
ALERTS_JSON = RUNTIME_DIR / "alerts_latest.json"
STRATEGY_EVOLUTION_JSON = RUNTIME_DIR / "strategy_evolution_latest.json"
EVENT_STORE_DB = RUNTIME_DIR / "event_store.sqlite3"
CST = timezone(timedelta(hours=8))
PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
ACKNOWLEDGED_STATUSES = {
    "acknowledged",
    "archived",
    "resolved",
    "continue_observe",
    "narrow_b_v16_requested",
    "rollback_prepare_requested",
}
SUPPRESSED_ARCHIVED_KEYWORDS = (
    "crypto-portal-refresh.service",
    "crypto-market-review.timer",
    "crypto-counterfactual-open-skips.timer",
    "crypto-strategy-evolution-gate.timer",
    "polymarket",
)


def now_cst() -> datetime:
    return datetime.now(CST)


def h(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST)
        return dt.astimezone(CST)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text[:26], fmt).replace(tzinfo=CST)
        except Exception:
            continue
    return None


def age_text(dt: datetime | None) -> str:
    if not dt:
        return "无记录"
    seconds = max(0, int((now_cst() - dt).total_seconds()))
    if seconds < 90:
        return f"{seconds} 秒前"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes} 分钟前"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} 小时前"
    return f"{hours // 24} 天前"


def read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def read_jsonl_tail(path: Path, limit: int = 500) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    except Exception:
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def ensure_attention_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        create table if not exists attention_items (
            item_id text primary key,
            priority text,
            category text,
            title text,
            status text,
            first_seen text,
            last_seen text,
            last_confirmed_active text,
            cleared_at text,
            acknowledged_at text,
            acknowledged_reason text,
            evidence text,
            recommended_action text,
            source text,
            fingerprint text,
            payload_json text
        )
        """
    )
    conn.execute(
        """
        create table if not exists attention_acknowledgements (
            item_id text primary key,
            status text,
            fingerprint text,
            title text,
            priority text,
            category text,
            reason text,
            acknowledged_at text,
            payload_json text
        )
        """
    )
    conn.commit()


def connect_attention_db() -> sqlite3.Connection | None:
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(EVENT_STORE_DB)
        conn.row_factory = sqlite3.Row
        ensure_attention_tables(conn)
        return conn
    except Exception:
        return None


def slug(text: str, limit: int = 80) -> str:
    text = re.sub(r"\s+", "-", str(text).strip().lower())
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff._:-]+", "-", text)
    return text.strip("-")[:limit] or "item"


def priority_from_level(level: str) -> str:
    if level == "bad":
        return "P0"
    if level == "warn":
        return "P1"
    return "P2"


def is_suppressed_archived_item(item: dict[str, Any]) -> bool:
    text = " ".join(
        str(item.get(key) or "").lower()
        for key in ("item_id", "category", "title", "evidence", "source")
    )
    return any(keyword in text for keyword in SUPPRESSED_ARCHIVED_KEYWORDS)


def make_item(
    item_id: str,
    priority: str,
    category: str,
    title: str,
    evidence: str,
    recommended_action: str,
    source: str,
    status: str = "open",
) -> dict[str, Any]:
    ts = now_cst().isoformat()
    return {
        "item_id": item_id,
        "priority": priority,
        "category": category,
        "title": title,
        "status": status,
        "first_seen": ts,
        "last_seen": ts,
        "last_confirmed_active": ts,
        "evidence": evidence,
        "recommended_action": recommended_action,
        "source": source,
        "requires_user_confirmation": True,
    }


def item_fingerprint(item: dict[str, Any]) -> str:
    text = "\n".join(
        str(item.get(key) or "")
        for key in ("item_id", "priority", "category", "title", "evidence", "source")
    )
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def load_full_live_approved_candidate_ids() -> set[str]:
    approved: set[str] = set()
    paths = [
        APPROVAL_DIR / "manual_actions.jsonl",
        APPROVAL_DIR / "manual_actions_latest.jsonl",
    ]
    rows: list[dict[str, Any]] = []
    for path in paths:
        rows.extend(read_jsonl(path))
    if APPROVAL_DIR.exists():
        for path in APPROVAL_DIR.glob("approve_full_live_*.json"):
            payload = read_json(path)
            if isinstance(payload, dict):
                rows.append(payload)
    for row in rows:
        if str(row.get("manual_action") or "") != "approve_full_live":
            continue
        if str(row.get("approved_scope") or "") != "full_live":
            continue
        ids: list[Any] = []
        ids.extend(row.get("candidate_ids") or [])
        ids.extend(row.get("experiment_ids") or [])
        for key in ("candidate_id", "experiment_id"):
            value = row.get(key)
            if value:
                ids.append(value)
        for candidate_id in ids:
            text = str(candidate_id or "").strip()
            if text:
                approved.add(text)
    return approved


def is_full_live_approved_attention_item(item_id: str, approved_ids: set[str]) -> bool:
    return any(item_id == f"evolution:{slug(candidate_id)}" for candidate_id in approved_ids)


def load_acknowledgements() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    conn = connect_attention_db()
    if conn is not None:
        try:
            for row in conn.execute("select * from attention_acknowledgements"):
                item = dict(row)
                out[str(item.get("item_id") or "")] = item
        except Exception:
            pass
        finally:
            conn.close()
    for row in read_jsonl(ATTENTION_ACK_JSONL):
        item_id = str(row.get("item_id") or "")
        if item_id and item_id not in out:
            out[item_id] = row
    return out


def is_acknowledged(item: dict[str, Any], acknowledgements: dict[str, dict[str, Any]]) -> bool:
    row = acknowledgements.get(str(item.get("item_id") or ""))
    if not row:
        return False
    if str(row.get("status") or "") not in ACKNOWLEDGED_STATUSES:
        return False
    expected = str(row.get("fingerprint") or "")
    return not expected or expected == item_fingerprint(item)


def detect_alert_items() -> list[dict[str, Any]]:
    payload = read_json(ALERTS_JSON)
    if not isinstance(payload, dict):
        return []
    out = []
    for alert in payload.get("alerts") or []:
        if not isinstance(alert, dict):
            continue
        title = str(alert.get("title") or "系统告警")
        body = str(alert.get("body") or "")
        level = str(alert.get("level") or "warn")
        if is_suppressed_archived_item({"title": title, "evidence": body}):
            continue
        out.append(
            make_item(
                item_id=f"alert:{slug(title)}",
                priority=priority_from_level(level),
                category="系统告警",
                title=title,
                evidence=body,
                recommended_action="优先处理或明确标记为已接受风险；未确认前保留在总入口。",
                source=str(ALERTS_JSON),
            )
        )
    return out


def detect_strategy_evolution_items() -> list[dict[str, Any]]:
    payload = read_json(STRATEGY_EVOLUTION_JSON)
    if not isinstance(payload, dict):
        return []
    full_live_approved = load_full_live_approved_candidate_ids()
    out = []
    for decision in payload.get("decisions") or []:
        if not isinstance(decision, dict):
            continue
        priority = str(decision.get("priority") or "P3")
        if priority not in {"P0", "P1", "P2"}:
            continue
        candidate_id = str(decision.get("candidate_id") or decision.get("experiment_id") or decision.get("status") or "unknown")
        status = str(decision.get("status") or "")
        is_rollback = status in {"rollback_required", "rollback_watch"}
        if candidate_id in full_live_approved and not is_rollback:
            continue
        strategy = str(decision.get("strategy") or "-")
        blockers = "; ".join(str(x) for x in (decision.get("blockers") or []) if x)
        evidence = (
            f"状态 {decision.get('status') or '-'}；证据分 {int(decision.get('evidence_score') or 0)}；"
            f"风险分 {int(decision.get('risk_score') or 0)}；阻塞 {blockers or '-'}"
        )
        action = str(
            decision.get("recommended_action")
            or ("检查已放开参数，必要时回滚到上一个稳定版本。" if is_rollback else "继续收集样本，未达 P0/P1 前不自动升级。")
        )
        item_prefix = "rollback" if is_rollback else "evolution"
        category = "策略回滚" if is_rollback else "策略进化"
        title_prefix = "回滚观察" if is_rollback else priority
        out.append(
            make_item(
                item_id=f"{item_prefix}:{slug(candidate_id)}",
                priority=priority,
                category=category,
                title=f"{priority} {strategy} {title_prefix} {candidate_id}",
                evidence=evidence,
                recommended_action=action,
                source=str(STRATEGY_EVOLUTION_JSON),
            )
        )
    return out


def detect_open_staleness_items() -> list[dict[str, Any]]:
    if not EVENT_STORE_DB.exists():
        return []
    strategies = ("A/v11", "B/v16", "C/v14")
    out = []
    try:
        conn = sqlite3.connect(EVENT_STORE_DB)
        conn.row_factory = sqlite3.Row
        for strategy in strategies:
            system_row = conn.execute(
                "select ts from events where strategy=? and source like '%/system' order by id desc limit 1",
                (strategy,),
            ).fetchone()
            system_ts = parse_dt(system_row["ts"]) if system_row else None
            if not system_ts or (now_cst() - system_ts).total_seconds() > 90 * 60:
                continue
            open_row = conn.execute(
                """
                select ts, symbol, side from events
                where strategy=? and source like '%/decisions'
                  and (event_type='OPEN' or category='opened')
                order by id desc limit 1
                """,
                (strategy,),
            ).fetchone()
            open_ts = parse_dt(open_row["ts"]) if open_row else None
            if open_ts and (now_cst() - open_ts).total_seconds() <= 48 * 3600:
                continue
            detail = f"最近系统心跳 {age_text(system_ts)}；最近开仓 {age_text(open_ts)}"
            if open_row:
                detail += f"（{open_row['symbol']} {open_row['side']}）"
            out.append(
                make_item(
                    item_id=f"strategy-open-stale:{slug(strategy)}",
                    priority="P2",
                    category="策略开仓节奏",
                    title=f"{strategy} 连续 48h 无新开仓",
                    evidence=detail,
                    recommended_action="结合反事实评估与过滤层分布确认是否过严；若只是行情不匹配，可标记为观察。",
                    source=str(EVENT_STORE_DB),
                )
            )
    except Exception:
        return out
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out


def load_existing() -> dict[str, dict[str, Any]]:
    conn = connect_attention_db()
    if conn is not None:
        try:
            rows = conn.execute("select payload_json from attention_items").fetchall()
            out: dict[str, dict[str, Any]] = {}
            for row in rows:
                try:
                    item = json.loads(row["payload_json"])
                except Exception:
                    continue
                if isinstance(item, dict) and item.get("item_id"):
                    out[str(item["item_id"])] = item
            if out:
                return out
        except Exception:
            pass
        finally:
            conn.close()
    payload = read_json(ATTENTION_JSON)
    items = payload.get("items") if isinstance(payload, dict) else []
    out: dict[str, dict[str, Any]] = {}
    for item in items or []:
        if isinstance(item, dict) and item.get("item_id"):
            out[str(item["item_id"])] = item
    return out


def persist_attention_items(items: list[dict[str, Any]]) -> None:
    conn = connect_attention_db()
    if conn is None:
        return
    try:
        for item in items:
            conn.execute(
                """
                insert into attention_items (
                    item_id, priority, category, title, status, first_seen, last_seen,
                    last_confirmed_active, cleared_at, acknowledged_at, acknowledged_reason,
                    evidence, recommended_action, source, fingerprint, payload_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(item_id) do update set
                    priority=excluded.priority,
                    category=excluded.category,
                    title=excluded.title,
                    status=excluded.status,
                    first_seen=excluded.first_seen,
                    last_seen=excluded.last_seen,
                    last_confirmed_active=excluded.last_confirmed_active,
                    cleared_at=excluded.cleared_at,
                    acknowledged_at=excluded.acknowledged_at,
                    acknowledged_reason=excluded.acknowledged_reason,
                    evidence=excluded.evidence,
                    recommended_action=excluded.recommended_action,
                    source=excluded.source,
                    fingerprint=excluded.fingerprint,
                    payload_json=excluded.payload_json
                """,
                (
                    item.get("item_id"),
                    item.get("priority"),
                    item.get("category"),
                    item.get("title"),
                    item.get("status"),
                    item.get("first_seen"),
                    item.get("last_seen"),
                    item.get("last_confirmed_active"),
                    item.get("cleared_at"),
                    item.get("acknowledged_at"),
                    item.get("acknowledged_reason"),
                    item.get("evidence"),
                    item.get("recommended_action"),
                    item.get("source"),
                    item_fingerprint(item),
                    json.dumps(item, ensure_ascii=False, default=str),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def merge_items(existing: dict[str, dict[str, Any]], detected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now = now_cst().isoformat()
    acknowledgements = load_acknowledgements()
    full_live_approved = load_full_live_approved_candidate_ids()
    detected_by_id = {str(item["item_id"]): item for item in detected if item.get("item_id")}
    merged: dict[str, dict[str, Any]] = {}
    for item_id, item in detected_by_id.items():
        if is_suppressed_archived_item(item):
            continue
        if is_acknowledged(item, acknowledgements):
            continue
        prior = existing.get(item_id, {})
        current = dict(prior)
        current.update(item)
        current["first_seen"] = prior.get("first_seen") or item.get("first_seen") or now
        current["last_seen"] = now
        current["last_confirmed_active"] = now
        current["status"] = "open"
        merged[item_id] = current
    for item_id, prior in existing.items():
        if item_id in merged:
            continue
        if is_full_live_approved_attention_item(item_id, full_live_approved):
            current = dict(prior)
            current["status"] = "resolved"
            current["acknowledged_at"] = current.get("acknowledged_at") or now
            current["acknowledged_reason"] = (
                current.get("acknowledged_reason")
                or "Manual full-live approval recorded in research_memory/approvals"
            )
            current["last_seen"] = current.get("last_seen") or now
            merged[item_id] = current
            continue
        if is_suppressed_archived_item(prior):
            current = dict(prior)
            current["status"] = "archived"
            current["acknowledged_at"] = current.get("acknowledged_at") or now
            current["acknowledged_reason"] = (
                current.get("acknowledged_reason")
                or "Component intentionally migrated or decommissioned"
            )
            current["last_seen"] = current.get("last_seen") or now
            merged[item_id] = current
            continue
        ack = acknowledgements.get(item_id)
        if ack and str(ack.get("status") or "") in ACKNOWLEDGED_STATUSES:
            current = dict(prior)
            current["status"] = str(ack.get("status") or "archived")
            current["acknowledged_at"] = ack.get("acknowledged_at") or now
            current["acknowledged_reason"] = ack.get("reason") or ""
            current["last_seen"] = current.get("last_seen") or now
            merged[item_id] = current
            continue
        current = dict(prior)
        status = str(current.get("status") or "open")
        if status == "open":
            current["status"] = "cleared_pending_review"
            current["cleared_at"] = now
            current["recommended_action"] = current.get("recommended_action") or "检测已不再触发，但保留到你确认后再关闭。"
        current["last_seen"] = current.get("last_seen") or now
        merged[item_id] = current
    return sorted(
        merged.values(),
        key=lambda x: (
            10 if x.get("status") == "resolved" else 0,
            PRIORITY_ORDER.get(str(x.get("priority") or "P3"), 9),
            str(x.get("category") or ""),
            str(x.get("title") or ""),
        ),
    )


def build_payload() -> dict[str, Any]:
    detected: list[dict[str, Any]] = []
    detected.extend(detect_alert_items())
    detected.extend(detect_strategy_evolution_items())
    detected.extend(detect_open_staleness_items())
    items = merge_items(load_existing(), detected)
    persist_attention_items(items)
    visible = [i for i in items if i.get("status") in {"open", "cleared_pending_review"}]
    open_items = [i for i in visible if i.get("status") == "open"]
    counts: dict[str, int] = {}
    for item in open_items:
        key = str(item.get("priority") or "P3")
        counts[key] = counts.get(key, 0) + 1
    return {
        "generated_at": now_cst().isoformat(),
        "summary": {
            "total_visible": len(visible),
            "open": len(open_items),
            "cleared_pending_review": sum(1 for i in visible if i.get("status") == "cleared_pending_review"),
            "counts": {k: counts.get(k, 0) for k in ("P0", "P1", "P2", "P3")},
        },
        "items": items,
    }


def render_md(payload: dict[str, Any]) -> str:
    lines = [
        "# 决策关注台账",
        "",
        f"- 生成时间: {payload.get('generated_at')}",
        f"- 可见事项: {payload.get('summary', {}).get('total_visible', 0)}",
        "",
        "| 优先级 | 状态 | 分类 | 标题 | 证据 | 建议 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in payload.get("items") or []:
        if item.get("status") not in {"open", "cleared_pending_review", "acknowledged"}:
            continue
        lines.append(
            "| {priority} | {status} | {category} | {title} | {evidence} | {action} |".format(
                priority=item.get("priority", ""),
                status=item.get("status", ""),
                category=str(item.get("category", "")).replace("|", "/"),
                title=str(item.get("title", "")).replace("|", "/"),
                evidence=str(item.get("evidence", "")).replace("|", "/"),
                action=str(item.get("recommended_action", "")).replace("|", "/"),
            )
        )
    return "\n".join(lines) + "\n"


def render_html(payload: dict[str, Any]) -> str:
    rows = []
    for item in payload.get("items") or []:
        if item.get("status") not in {"open", "cleared_pending_review", "acknowledged"}:
            continue
        rows.append(
            "<tr>"
            f"<td>{h(item.get('priority'))}</td>"
            f"<td>{h(item.get('status'))}</td>"
            f"<td>{h(item.get('category'))}</td>"
            f"<td>{h(item.get('title'))}</td>"
            f"<td>{h(item.get('evidence'))}</td>"
            f"<td>{h(item.get('recommended_action'))}</td>"
            f"<td>{h(item.get('first_seen'))}</td>"
            f"<td>{h(item.get('last_seen'))}</td>"
            "</tr>"
        )
    body_rows = "\n".join(rows) or '<tr><td colspan="8">暂无需要保留的关注事项</td></tr>'
    summary = payload.get("summary") or {}
    counts = summary.get("counts") or {}
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>决策关注台账</title>
<style>
body {{ margin:0; background:#f3f6fb; color:#172033; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,"Microsoft YaHei",sans-serif; }}
main {{ max-width:1280px; margin:0 auto; padding:24px; }}
h1 {{ margin:0 0 8px; font-size:28px; }}
p {{ color:#475467; line-height:1.7; }}
.panel {{ background:#fff; border:1px solid #d7e0ec; border-radius:8px; padding:16px; overflow:auto; }}
table {{ width:100%; border-collapse:collapse; min-width:1000px; }}
th,td {{ padding:10px 11px; border-bottom:1px solid #e7edf6; text-align:left; font-size:13px; vertical-align:top; }}
th {{ background:#f1f5f9; color:#334155; }}
.meta {{ display:flex; flex-wrap:wrap; gap:10px; margin:14px 0; }}
.meta span {{ background:#fff; border:1px solid #d7e0ec; border-radius:8px; padding:9px 12px; font-weight:700; }}
</style>
</head>
<body>
<main>
  <h1>决策关注台账</h1>
  <p>这里不是日报。只要事项没有被确认关闭，即使日报滚动、报告重生成，也会继续保留。</p>
  <div class="meta">
    <span>生成 {h(payload.get('generated_at'))}</span>
    <span>open {h(summary.get('open', 0))}</span>
    <span>待复核 {h(summary.get('cleared_pending_review', 0))}</span>
    <span>P0 {h(counts.get('P0', 0))}</span>
    <span>P1 {h(counts.get('P1', 0))}</span>
    <span>P2 {h(counts.get('P2', 0))}</span>
  </div>
  <section class="panel">
    <table>
      <thead><tr><th>优先级</th><th>状态</th><th>分类</th><th>标题</th><th>证据</th><th>建议</th><th>首次发现</th><th>最近触发</th></tr></thead>
      <tbody>{body_rows}</tbody>
    </table>
  </section>
</main>
</body>
</html>
"""


def write_outputs(payload: dict[str, Any]) -> None:
    ATTENTION_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ATTENTION_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    ATTENTION_MD.write_text(render_md(payload), encoding="utf-8")
    ATTENTION_HTML.write_text(render_html(payload), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成持久决策关注台账")
    parser.parse_args(argv)
    payload = build_payload()
    write_outputs(payload)
    summary = payload.get("summary") or {}
    print(
        json.dumps(
            {
                "generated_at": payload.get("generated_at"),
                "visible": summary.get("total_visible"),
                "open": summary.get("open"),
                "counts": summary.get("counts"),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
