"""Generate a concise online decision portal for the operator.

This is the first screen. It only reads existing local/runtime artifacts and
never calls Binance. The older full portal remains available as
``portal_latest.html`` for drilldown.
"""

from __future__ import annotations

import argparse
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
REPORTS_DIR = ROOT / "reports"
RUNTIME_DIR = ROOT / "runtime"
MIRROR_RUNTIME_DIR = ROOT / "server_logs_tencent" / "runtime"
ATTENTION_JSON = ROOT / "research_memory" / "attention" / "open_items.json"
LOCAL_DB = RUNTIME_DIR / "event_store.sqlite3"
MIRROR_DB = ROOT / "server_logs_tencent" / "runtime" / "event_store.sqlite3"
EVENT_DB = MIRROR_DB if MIRROR_DB.exists() else LOCAL_DB
CST = timezone(timedelta(hours=8))
DEFAULT_TAKER_FEE_RATE = 0.0004


def h(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def read_first_json(*paths: Path) -> dict[str, Any]:
    candidates: list[tuple[float, dict[str, Any]]] = []
    for path in paths:
        payload = read_json(path)
        if not payload:
            continue
        try:
            mtime = path.stat().st_mtime
        except Exception:
            mtime = 0.0
        candidates.append((mtime, payload))
    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]
    return {}


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST)
        return dt.astimezone(CST)
    except Exception:
        return None


def age_text(dt: datetime | None) -> str:
    if not dt:
        return "无记录"
    seconds = max(0, int((datetime.now(CST) - dt).total_seconds()))
    if seconds < 90:
        return f"{seconds}秒前"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes}分钟前"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}小时前"
    return f"{hours // 24}天前"


def number(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):+.{digits}f}"
    except Exception:
        return "0.00"


def fmt_plain(value: Any, digits: int = 6, default: str = "-") -> str:
    try:
        return f"{float(value):.{digits}f}".rstrip("0").rstrip(".")
    except Exception:
        return default


def plain_level(level: str) -> str:
    return level if level in {"good", "warn", "bad", "muted"} else "muted"


def plain_status(value: Any) -> str:
    text = str(value or "").strip()
    lower = text.lower()
    mapping = {
        "ok": "正常",
        "good": "正常",
        "ready": "已准备好",
        "missing": "缺少数据",
        "stale_mirror_unknown": "镜像过期，暂不判断",
        "blocked": "被挡住",
        "watch": "观察中",
        "pass": "通过",
        "fail": "未通过",
    }
    if lower in mapping:
        return mapping[lower]
    if not text:
        return "缺少数据"
    return text.replace("_", " ")


def plain_strategy_reason(reason: Any, kind: str = "skip") -> str:
    raw = str(reason or "").strip()
    lower = raw.lower()
    if not raw:
        if kind == "failed":
            return "有开仓执行失败，要看详情确认是不是账户状态、交易所规则或风控拦截。"
        if kind == "close_failed":
            return "有平仓或强平失败，要优先看详情确认仓位是否还在。"
        return "有候选，但被策略规则挡住；这通常不是系统故障。"
    checks = [
        (("15m", "确认"), "有候选，但15分钟确认没有跟上，所以策略按规则没开仓。"),
        (("open_submitted_unconfirmed",), "订单已提交到交易所，但还没有确认成交成仓；系统不会先建本地假仓，会等回执或下一轮核对。"),
        (("open_unfilled",), "交易所收到了开仓请求，但当前回包没有成交数量；系统先不当作已开仓。"),
        (("open_confirm_account_state_unavailable",), "订单已经提交，但成交后的账户回执还没回来；系统会等用户流或受控确认补证，不能把它当成策略没信号。"),
        (("close_confirm_account_state_unavailable",), "平仓已经提交，但账户回执还没确认仓位消失；系统会继续补证，不能把它当成普通失败。"),
        (("confirm_account_state_unavailable",), "交易请求已发出，但成交后账户回执还不够新；这是确认链路问题，不是策略没有机会。"),
        (("fresh central account state unavailable",), "账户资料太旧，系统先避免误判仓位；恢复期会用已验证账户状态和用户流补新，不该长期挡住开仓。"),
        (("scanner_order_disabled",), "当前是观察模式，只记录信号，不允许真开仓。"),
        (("cooldown",), "接口处在保护/冷却状态，先保护币安风控，不继续加压。"),
        (("-1003",), "币安提示请求过多，系统应先退避，不能硬冲。"),
        (("418",), "币安触发保护，系统应先等冷却清干净。"),
        (("429",), "请求频率被限制，系统应先降压等待。"),
        (("min_notional",), "订单金额不满足交易所最小下单规则，所以提前挡住。"),
        (("-4164",), "订单金额不满足交易所最小下单规则，所以提前挡住。"),
        (("same_symbol",), "同币种已有仓位，风控不允许重复叠仓。"),
        (("duplicate", "position"), "同币种已有仓位，风控不允许重复叠仓。"),
        (("insufficient", "balance"), "可用余额或保证金不够，系统没有强行开仓。"),
        (("risk",), "风险检查没通过，所以策略没有继续下单。"),
        (("kline",), "K线数据不够新或不完整，策略先跳过，避免用脏数据开仓。"),
        (("no data",), "行情数据不完整，策略先跳过，避免用脏数据开仓。"),
        (("score",), "分数还没到策略要求，属于正常筛选。"),
        (("threshold",), "还没达到策略阈值，属于正常筛选。"),
        (("can_trade=false",), "策略判断当前不适合交易，所以没有开仓。"),
        (("open_skipped",), "候选被策略门控挡住；这是筛选结果，不是服务挂了。"),
    ]
    for keys, message in checks:
        if all(key in lower for key in keys):
            return message
    if kind == "failed":
        return f"开仓执行失败，需看详情定位：{raw}"
    if kind == "close_failed":
        return f"平仓/强平执行失败，需看详情定位：{raw}"
    return f"候选被策略规则挡住：{raw}"


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("select 1 from sqlite_master where type='table' and name=?", (name,)).fetchone()
    return bool(row)


def queue_summary(db_path: Path = RUNTIME_DIR / "binance_api_queue.sqlite3") -> dict[str, Any]:
    candidates = [db_path]
    mirror_db = MIRROR_RUNTIME_DIR / "binance_api_queue.sqlite3"
    if mirror_db not in candidates:
        candidates.append(mirror_db)
    summary_candidates = [
        RUNTIME_DIR / "binance_api_queue_summary_latest.json",
        MIRROR_RUNTIME_DIR / "binance_api_queue_summary_latest.json",
    ]
    db_path = next((path for path in candidates if path.exists()), db_path)
    if not db_path.exists():
        summary = read_first_json(*summary_candidates)
        if summary:
            return {
                "available": True,
                "active": int(summary.get("active") or 0),
                "cooldowns": int(summary.get("cooldowns") or 0),
                "last": summary.get("last") if isinstance(summary.get("last"), list) else [],
                "counts": summary.get("counts") if isinstance(summary.get("counts"), dict) else {},
                "source": "summary",
            }
        return {"available": False, "active": 0, "cooldowns": 0, "last": [], "counts": {}}
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
        active = conn.execute(
            "select count(*) from api_requests where status in ('queued','deferred','leased')"
        ).fetchone()[0]
        cooldowns = conn.execute(
            "select count(*) from api_cooldowns where until_ms > ?",
            (now_ms,),
        ).fetchone()[0]
        counts = {
            row["status"]: int(row["n"])
            for row in conn.execute("select status, count(*) n from api_requests group by status")
        }
        last = [dict(row) for row in conn.execute(
            "select rowid,label,scope,account,path,status,result_status,error from api_requests order by rowid desc limit 6"
        )]
        conn.close()
        return {"available": True, "active": int(active), "cooldowns": int(cooldowns), "counts": counts, "last": last}
    except Exception as exc:
        return {"available": False, "error": str(exc), "active": 0, "cooldowns": 0, "last": [], "counts": {}}


def event_summary(db_path: Path = EVENT_DB) -> dict[str, Any]:
    empty = {
        "available": False,
        "events": 0,
        "sentinel_scans": 0,
        "account_snapshots": 0,
        "latest_ts": None,
        "strategies": [],
        "open_close": {},
    }
    if not db_path.exists():
        return empty
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        out = dict(empty)
        out["available"] = True
        if table_exists(conn, "events"):
            out["events"] = int(conn.execute("select count(*) from events").fetchone()[0])
            row = conn.execute("select ts from events order by id desc limit 1").fetchone()
            out["latest_ts"] = parse_dt(row["ts"]) if row else None
            since = (datetime.now(CST) - timedelta(hours=24)).strftime("%Y-%m-%d")
            strategies = []
            for name in ("A/v11", "B/v16", "C/v14"):
                latest = conn.execute(
                    "select ts,event_type,category,payload_json from events where strategy=? order by id desc limit 1",
                    (name,),
                ).fetchone()
                counts = conn.execute(
                    """
                    select
                      sum(case when event_type='OPEN' or category='opened' then 1 else 0 end) as opens,
                      sum(case when event_type='OPEN' and (source like '%paper_sample%' or payload_json like '%"paper_sample":true%') then 1 else 0 end) as paper_sample_opens,
                      sum(case when event_type in ('CLOSE','FORCED_CLOSE') or category in ('closed','forced_close') then 1 else 0 end) as closes,
                      sum(case when event_type='OPEN_FAILED' or category='open_failed' then 1 else 0 end) as open_failed,
                      sum(case when event_type='OPEN_SKIPPED' or category='open_skipped' then 1 else 0 end) as open_skipped,
                      sum(case when event_type like '%CLOSE_FAILED%' or category like '%close_failed%' then 1 else 0 end) as close_failed
                    from events
                    where strategy=? and substr(ts,1,10) >= ?
                    """,
                    (name, since),
                ).fetchone()
                skip_reason = conn.execute(
                    """
                    select coalesce(nullif(reason,''), stage, category, event_type) as reason, count(*) n
                    from events
                    where strategy=? and event_type='OPEN_SKIPPED' and substr(ts,1,10) >= ?
                    group by coalesce(nullif(reason,''), stage, category, event_type)
                    order by n desc
                    limit 1
                    """,
                    (name, since),
                ).fetchone()
                failed_reason = conn.execute(
                    """
                    select coalesce(nullif(reason,''), stage, category, event_type) as reason, count(*) n
                    from events
                    where strategy=? and event_type='OPEN_FAILED' and substr(ts,1,10) >= ?
                    group by coalesce(nullif(reason,''), stage, category, event_type)
                    order by n desc
                    limit 1
                    """,
                    (name, since),
                ).fetchone()
                strategies.append({
                    "name": name,
                    "latest": parse_dt(latest["ts"]) if latest else None,
                    "opens": int(counts["opens"] or 0) if counts else 0,
                    "paper_sample_opens": int(counts["paper_sample_opens"] or 0) if counts else 0,
                    "closes": int(counts["closes"] or 0) if counts else 0,
                    "open_failed": int(counts["open_failed"] or 0) if counts else 0,
                    "open_skipped": int(counts["open_skipped"] or 0) if counts else 0,
                    "close_failed": int(counts["close_failed"] or 0) if counts else 0,
                    "skip_reason": str(skip_reason["reason"] or "") if skip_reason else "",
                    "failed_reason": str(failed_reason["reason"] or "") if failed_reason else "",
                })
            out["strategies"] = strategies
        if table_exists(conn, "sentinel_scans"):
            out["sentinel_scans"] = int(conn.execute("select count(*) from sentinel_scans").fetchone()[0])
        if table_exists(conn, "account_snapshots"):
            out["account_snapshots"] = int(conn.execute("select count(*) from account_snapshots").fetchone()[0])
        conn.close()
        return out
    except Exception as exc:
        out = dict(empty)
        out["error"] = str(exc)
        return out


def account_for_strategy(account: dict[str, Any], strategy: str) -> dict[str, Any]:
    accounts = account.get("accounts") if isinstance(account.get("accounts"), list) else []
    for row in accounts:
        if isinstance(row, dict) and row.get("strategy") == strategy:
            return row
    return {}


def fee_estimate(notional: Any) -> tuple[str, str]:
    try:
        value = abs(float(notional))
    except Exception:
        value = 0.0
    if value <= 0:
        return "-", "没有名义价值，暂不能估手续费。"
    one_way = value * DEFAULT_TAKER_FEE_RATE
    return f"单边约 {fmt_plain(one_way, 4)} / 往返约 {fmt_plain(one_way * 2, 4)} USDT", "估算：按 taker 0.04%，不是交易所逐笔扣费流水。"


def latest_paper_open_rows(strategy: str, db_path: Path = EVENT_DB, limit: int = 6) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        if not table_exists(conn, "events"):
            conn.close()
            return []
        rows = conn.execute(
            """
            select ts,symbol,side,source,payload_json
            from events
            where strategy=?
              and event_type='OPEN'
              and (source like '%paper_sample%' or payload_json like '%"paper_sample":true%')
            order by id desc
            limit ?
            """,
            (strategy, limit),
        ).fetchall()
        conn.close()
        out = []
        for row in rows:
            payload: dict[str, Any] = {}
            try:
                parsed = json.loads(row["payload_json"] or "{}")
                payload = parsed if isinstance(parsed, dict) else {}
            except Exception:
                payload = {}
            out.append({
                "ts": row["ts"],
                "symbol": row["symbol"],
                "side": row["side"],
                "source": row["source"],
                "payload": payload,
            })
        return out
    except Exception:
        return []


def position_upnl_class(value: Any) -> str:
    try:
        return "up" if float(value) >= 0 else "down"
    except Exception:
        return "muted"


def strategy_detail_html(strategy: str, account: dict[str, Any]) -> str:
    account_row = account_for_strategy(account, strategy)
    positions = account_row.get("positions") if isinstance(account_row.get("positions"), list) else []
    paper_rows = latest_paper_open_rows(strategy)
    rows: list[str] = []
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        notional = pos.get("notional")
        fee_text, fee_note = fee_estimate(notional)
        mark = pos.get("mark")
        quality = "交易所快照"
        if mark in {0, 0.0, "0", "0.0", None, ""}:
            quality = "账户快照缺 mark，浮盈亏按中心账户状态展示，需等下一次行情/账户回执补新。"
        rows.append(
            f"""
<tr>
  <td>真实持仓</td>
  <td>{h(pos.get('symbol'))}</td>
  <td>{h(pos.get('side'))}</td>
  <td>{h(fmt_plain(pos.get('qty')))}</td>
  <td>{h(fmt_plain(pos.get('entry')))}</td>
  <td>{h(fmt_plain(mark))}</td>
  <td class="{position_upnl_class(pos.get('upnl'))}">{h(number(pos.get('upnl'), 4))}</td>
  <td>{h(fmt_plain(notional, 4))}</td>
  <td>{h(fmt_plain(pos.get('margin'), 4))}</td>
  <td>{h(fee_text)}<small>{h(fee_note)}</small></td>
  <td>待补资金费率流水<small>当前 report 还没有逐笔 funding income，不能硬写精确数。</small></td>
  <td>{h(quality)}</td>
</tr>
""".strip()
        )
    for paper in paper_rows:
        payload = paper.get("payload") if isinstance(paper.get("payload"), dict) else {}
        notional = payload.get("expected_notional_usdt") or payload.get("notional") or payload.get("target_notional_usdt")
        fee_text, fee_note = fee_estimate(notional)
        rows.append(
            f"""
<tr>
  <td>模拟采样</td>
  <td>{h(paper.get('symbol'))}</td>
  <td>{h(paper.get('side'))}</td>
  <td>{h(fmt_plain(payload.get('exchange_qty') or payload.get('qty')))}</td>
  <td>{h(fmt_plain(payload.get('price')))}</td>
  <td>待盯市</td>
  <td class="muted">不计入实盘</td>
  <td>{h(fmt_plain(notional, 4))}</td>
  <td>{h(fmt_plain(payload.get('target_margin_usdt'), 4))}</td>
  <td>{h(fee_text)}<small>{h(fee_note)}</small></td>
  <td>模拟未结算<small>只验证数据链路，不是真实成交。</small></td>
  <td>paper_sample，不当作实盘盈亏。</td>
</tr>
""".strip()
        )
    if not rows:
        rows.append(
            """
<tr>
  <td colspan="12">当前没有可展示持仓或模拟采样。若策略有信号但没开仓，先看“主要原因”和候选被挡住。</td>
</tr>
""".strip()
        )
    summary = (
        f"账户 {h(account_row.get('account') or '-')}: "
        f"持仓 {h(account_row.get('open_positions') or 0)}，"
        f"浮盈亏 {h(number(account_row.get('unrealized_pnl_usdt'), 4))} USDT，"
        f"可用 {h(number(account_row.get('available_usdt'), 2))} USDT"
        if account_row
        else "未找到该策略账户快照"
    )
    return f"""
<details class="strategy-detail">
  <summary>查看持仓盈亏 / 手续费 / 资金费率</summary>
  <p class="detail-note">{summary}。浮盈亏优先用账户快照；手续费无成交流水时只估算；资金费率无流水时明确标缺口。</p>
  <div class="table-scroll"><table class="position-table">
    <thead><tr><th>类型</th><th>币种</th><th>方向</th><th>数量</th><th>开仓价</th><th>标记价</th><th>浮盈亏</th><th>名义价值</th><th>保证金</th><th>手续费</th><th>资金费率</th><th>可信度</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table></div>
</details>
"""


def strategy_rows(event: dict[str, Any], alerts: dict[str, Any], account: dict[str, Any] | None = None, *, include_details: bool = False) -> list[dict[str, str]]:
    services = alerts.get("services") if isinstance(alerts.get("services"), dict) else {}
    service_map = {
        "A/v11": "crypto-scanner.service",
        "B/v16": "crypto-scanner-v16.service",
        "C/v14": "crypto-scanner-v14.service",
    }
    by_name = {row["name"]: row for row in event.get("strategies") or [] if isinstance(row, dict)}
    rows = []
    for name in ("A/v11", "B/v16", "C/v14"):
        item = by_name.get(name, {})
        service = services.get(service_map[name], "unknown")
        open_failed = int(item.get("open_failed") or 0)
        close_failed = int(item.get("close_failed") or 0)
        open_skipped = int(item.get("open_skipped") or 0)
        paper_sample_opens = int(item.get("paper_sample_opens") or 0)
        level = "bad" if service != "active" else "good"
        note = "正常运行"
        raw_note = ""
        note_kind = "normal"
        if paper_sample_opens:
            note = "模拟盘已产生采样开仓。注意：这是 paper sample，用来打通 report/回测数据链，不是真实交易所成交，也不是放宽真实策略。"
            note_kind = "paper_sample"
        elif open_failed:
            raw_note = str(item.get("failed_reason") or "")
            note = plain_strategy_reason(raw_note, "failed")
            note_kind = "failed"
        elif close_failed:
            note = plain_strategy_reason("", "close_failed")
            note_kind = "close_failed"
        elif open_skipped:
            raw_note = str(item.get("skip_reason") or "")
            note = plain_strategy_reason(raw_note, "skip")
            note_kind = "skip"
        rows.append({
            "level": level,
            "name": name,
            "service": "运行中" if service == "active" else f"异常({service})",
            "age": age_text(item.get("latest")),
            "opens": str(item.get("opens", 0)),
            "paper_sample_opens": str(paper_sample_opens),
            "closes": str(item.get("closes", 0)),
            "open_failed": str(open_failed),
            "close_failed": str(close_failed),
            "open_skipped": str(open_skipped),
            "note": note,
            "raw_note": raw_note,
            "note_kind": note_kind,
            "detail_html": strategy_detail_html(name, account or {}) if include_details else "",
        })
    return rows


def attention_items(limit: int = 8) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = read_json(ATTENTION_JSON)
    items = [
        item for item in payload.get("items", [])
        if (
            isinstance(item, dict)
            and item.get("status") == "open"
            and str(item.get("priority") or "") in {"P0", "P1"}
        )
    ]
    rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    items.sort(key=lambda item: (rank.get(str(item.get("priority") or "P3"), 9), str(item.get("title") or "")))
    return payload.get("summary") or {}, items[:limit]


def attention_level_label(priority: Any) -> str:
    return {
        "P0": "马上处理",
        "P1": "需要你决定",
        "P2": "观察项",
        "P3": "记录",
    }.get(str(priority or ""), "事项")


def item_strategy(item: dict[str, Any]) -> str:
    text = " ".join(str(item.get(key) or "") for key in ("title", "evidence", "item_id"))
    match = re.search(r"\b(A/v11|B/v16|C/v14)\b", text)
    return match.group(1) if match else "策略"


def plain_attention_title(item: dict[str, Any]) -> str:
    category = str(item.get("category") or "")
    title = str(item.get("title") or "需要确认")
    priority = str(item.get("priority") or "")
    strategy = item_strategy(item)
    item_id = str(item.get("item_id") or "")
    if category == "策略回滚" or item_id.startswith("rollback:"):
        return f"{strategy} 已上线改动需要复核"
    if category == "策略进化":
        if priority in {"P0", "P1"}:
            return f"{strategy} 有策略改动需要你决定"
        return f"{strategy} 有策略改进在观察"
    return title


def plain_attention_evidence(item: dict[str, Any]) -> str:
    text = str(item.get("evidence") or "")
    category = str(item.get("category") or "")
    item_id = str(item.get("item_id") or "")
    if "HTTP 418" in text or "-1003" in text:
        return "以前触发过币安接口保护；如果再次出现，要先停扩量，等冷却清干净。"
    if category == "策略回滚" or item_id.startswith("rollback:"):
        return "这项已经上线过，现在要看真实表现是否继续、收窄，还是准备回滚。"
    if category == "策略进化":
        if "small_live_monitoring" in text:
            return "现在只是小仓观察，不能当成已经验证好的正式升级。"
        if "shadow_validating" in text:
            return "还在影子验证，缺少真实成交或纸面撮合盈利证据。"
        if "ready_for_review" in text:
            return "已有一些证据，但还需要人工决定下一步。"
        if "样本不足" in text:
            return "样本还不够，先继续观察，不急着改实盘。"
    if text:
        return text[:180]
    return "没有更多说明。"


def plain_attention_action(item: dict[str, Any]) -> str:
    action = str(item.get("recommended_action") or "")
    category = str(item.get("category") or "")
    priority = str(item.get("priority") or "")
    if category in {"策略进化", "策略回滚"}:
        if "rollback" in action or category == "策略回滚":
            return "打开详情看盈亏和失败原因，决定继续观察、收窄，或准备回滚。"
        if "shadow" in action:
            return "不用现在上线，继续收样；等有真实/纸面盈利证据再说。"
        return "先看详情，再决定继续观察还是暂停扩样。"
    if priority == "P0":
        return "先处理这个风险；确认已经解决或接受风险后，再点确认。"
    if priority == "P1":
        return "看一眼是否接受这个风险；接受或处理完后点确认。"
    return "不用现在处理，继续观察。"


def cleanup_summary() -> dict[str, Any]:
    # Read-only coarse sizing. Data maintenance/retention performs actual moves.
    paths = [
        ("runtime", ROOT / "runtime"),
        ("logs", ROOT / "logs"),
        ("reports", ROOT / "reports"),
        ("archive", ROOT / "archive"),
        ("server mirror", ROOT / "server_logs_tencent"),
    ]
    rows = []
    total = 0
    for label, path in paths:
        size = 0
        if path.exists():
            try:
                size = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
            except Exception:
                size = 0
        total += size
        rows.append({"label": label, "bytes": size, "mb": round(size / 1024 / 1024, 1)})
    return {"total_mb": round(total / 1024 / 1024, 1), "rows": rows}


def build_state() -> dict[str, Any]:
    alerts = read_first_json(RUNTIME_DIR / "alerts_latest.json", MIRROR_RUNTIME_DIR / "alerts_latest.json")
    account = read_first_json(RUNTIME_DIR / "account_snapshot_latest.json", MIRROR_RUNTIME_DIR / "account_snapshot_latest.json")
    evolution = read_first_json(RUNTIME_DIR / "strategy_evolution_latest.json", MIRROR_RUNTIME_DIR / "strategy_evolution_latest.json")
    replay = read_first_json(RUNTIME_DIR / "replay_readiness_latest.json", MIRROR_RUNTIME_DIR / "replay_readiness_latest.json")
    waiting = read_first_json(RUNTIME_DIR / "waiting_period_optimization_latest.json", MIRROR_RUNTIME_DIR / "waiting_period_optimization_latest.json")
    parity = read_first_json(RUNTIME_DIR / "replay_live_parity_latest.json", MIRROR_RUNTIME_DIR / "replay_live_parity_latest.json")
    skeleton = read_first_json(RUNTIME_DIR / "long_term_skeleton_latest.json", MIRROR_RUNTIME_DIR / "long_term_skeleton_latest.json")
    research = read_first_json(RUNTIME_DIR / "research_store_summary_latest.json", MIRROR_RUNTIME_DIR / "research_store_summary_latest.json")
    kline = read_first_json(RUNTIME_DIR / "research_kline_backfill_latest.json", MIRROR_RUNTIME_DIR / "research_kline_backfill_latest.json")
    depth = read_first_json(RUNTIME_DIR / "research_depth_backfill_latest.json", MIRROR_RUNTIME_DIR / "research_depth_backfill_latest.json")
    paper_exchange = read_first_json(RUNTIME_DIR / "paper_exchange_latest.json", MIRROR_RUNTIME_DIR / "paper_exchange_latest.json")
    reset = read_first_json(RUNTIME_DIR / "testnet_data_reset_latest.json", MIRROR_RUNTIME_DIR / "testnet_data_reset_latest.json")
    q = queue_summary()
    ev = event_summary()
    att_summary, att_items = attention_items()
    account_summary = account.get("summary") if isinstance(account.get("summary"), dict) else {}
    stale = account_summary.get("stale_accounts") if isinstance(account_summary.get("stale_accounts"), list) else []
    active_alerts = [
        a for a in alerts.get("alerts", [])
        if isinstance(a, dict) and a.get("level") in {"bad", "warn"}
    ]
    bad_alerts = [a for a in active_alerts if a.get("level") == "bad"]
    overall = "good"
    if bad_alerts or q.get("cooldowns") or q.get("active"):
        overall = "bad"
    elif active_alerts or stale:
        overall = "warn"
    return {
        "generated_at": datetime.now(CST),
        "overall": overall,
        "alerts": alerts,
        "account": account,
        "account_summary": account_summary,
        "evolution": evolution,
        "replay": replay,
        "waiting": waiting,
        "parity": parity,
        "skeleton": skeleton,
        "research": research,
        "kline": kline,
        "depth": depth,
        "paper_exchange": paper_exchange,
        "reset": reset,
        "queue": q,
        "event": ev,
        "attention_summary": att_summary,
        "attention_items": att_items,
        "cleanup": cleanup_summary(),
    }


def status_text(state: dict[str, Any]) -> str:
    if state["overall"] == "bad":
        return "先别扩张：有红灯要处理"
    if state["overall"] == "warn":
        return "可以运行：有黄灯要观察"
    return "可以运行：当前没有红灯"


def waiting_top100_text(waiting: dict[str, Any]) -> tuple[str, str]:
    summary = waiting.get("summary") if isinstance(waiting.get("summary"), dict) else {}
    coverage = waiting.get("scan_coverage") if isinstance(waiting.get("scan_coverage"), dict) else {}
    if coverage.get("coverage_status") == "stale_mirror_unknown":
        return "未知", "warn"
    try:
        pct = float(summary.get("top100_pct") or 0.0)
    except Exception:
        pct = 0.0
    level = "good" if pct >= 80 else "warn" if pct >= 35 else "bad"
    return f"{summary.get('top100_scanned', 0)}/{coverage.get('target_count', 100)}", level


def render_badges(state: dict[str, Any]) -> str:
    q = state["queue"]
    account = state["account_summary"]
    paper = state.get("paper_exchange") or {}
    alerts = state["alerts"]
    waiting = state.get("waiting") or {}
    top100_value, top100_level = waiting_top100_text(waiting)
    skeleton_summary = (state["skeleton"].get("summary") or {}) if isinstance(state["skeleton"], dict) else {}
    items = [
        ("三策略", "运行中", "good"),
        ("API队列", f"等待{q.get('active', 0)} / 冷却{q.get('cooldowns', 0)}", "bad" if q.get("active") or q.get("cooldowns") else "good"),
        ("Top100实扫", top100_value, top100_level),
        ("模拟持仓", str(paper.get("open_positions", account.get("open_positions", 0))), "good"),
        ("模拟浮盈亏", f"{number(paper.get('total_unrealized_pnl', account.get('unrealized_pnl_usdt')))} USDT", "good"),
        ("告警", str(alerts.get("alert_count", 0)), "bad" if alerts.get("status") == "bad" else "warn" if alerts.get("status") == "warn" else "good"),
        ("长期骨架", f"{skeleton_summary.get('ready_bones', 0)}/{skeleton_summary.get('total_bones', 0)}", "good"),
    ]
    return "".join(
        f'<article class="metric {plain_level(level)}"><span>{h(label)}</span><b>{h(value)}</b></article>'
        for label, value, level in items
    )


def render_paper_exchange(state: dict[str, Any]) -> str:
    paper = state.get("paper_exchange") or {}
    if not paper:
        return '<p class="empty">模拟交易所账本还没生成。系统会先跑 paper_exchange_runner 生成持仓、盯市盈亏、手续费和资金费率。</p>'
    by_strategy = paper.get("by_strategy") if isinstance(paper.get("by_strategy"), dict) else {}
    cards = []
    for name in ("A/v11", "B/v16", "C/v14"):
        row = by_strategy.get(name) if isinstance(by_strategy.get(name), dict) else {}
        cards.append(
            f"""
<article class="paper-card">
  <span>{h(name)}</span>
  <b>{h(row.get('positions', 0))} 仓 / {h(number(row.get('unrealized_pnl'), 4))} USDT</b>
  <p>权益 {h(number(row.get('equity'), 2))}，已实现 {h(number(row.get('realized_pnl'), 4))}，手续费 {h(number(row.get('fees_paid'), 4))}，资金费 {h(number(row.get('funding_paid'), 4))}</p>
</article>
""".strip()
        )
    positions = paper.get("positions") if isinstance(paper.get("positions"), list) else []
    rows = []
    for pos in positions[:80]:
        if not isinstance(pos, dict):
            continue
        upnl = pos.get("unrealized_pnl")
        rows.append(
            f"""
<tr>
  <td>{h(pos.get('strategy'))}</td>
  <td>{h(pos.get('symbol'))}</td>
  <td>{h(pos.get('side'))}</td>
  <td>{h(fmt_plain(pos.get('qty')))}</td>
  <td>{h(fmt_plain(pos.get('entry_price')))}</td>
  <td>{h(fmt_plain(pos.get('mark_price')))}</td>
  <td class="{position_upnl_class(upnl)}">{h(number(upnl, 4))}</td>
  <td>{h(fmt_plain(pos.get('notional'), 4))}</td>
  <td>{h(fmt_plain(pos.get('margin'), 4))}</td>
  <td>{h(number(pos.get('fees_paid'), 4))}</td>
  <td>{h(number(pos.get('funding_paid'), 4))}<small>{h(pos.get('funding_source') or '')}</small></td>
  <td>{h(pos.get('mark_source'))}</td>
</tr>
""".strip()
        )
    if not rows:
        rows.append('<tr><td colspan="12">当前 paper exchange 没有持仓。</td></tr>')
    return f"""
<div class="paper-summary">
  <div><span>总权益</span><b>{h(number(paper.get('total_equity'), 2))} USDT</b></div>
  <div><span>总浮盈亏</span><b class="{position_upnl_class(paper.get('total_unrealized_pnl'))}">{h(number(paper.get('total_unrealized_pnl'), 4))} USDT</b></div>
  <div><span>开仓数</span><b>{h(paper.get('open_positions', 0))}</b></div>
  <div><span>模式</span><b>paper exchange</b></div>
</div>
<div class="paper-cards">{''.join(cards)}</div>
<div class="table-scroll"><table class="paper-table">
  <thead><tr><th>策略</th><th>币种</th><th>方向</th><th>数量</th><th>开仓价</th><th>盯市价</th><th>浮盈亏</th><th>名义价值</th><th>保证金</th><th>手续费</th><th>资金费率</th><th>价格源</th></tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table></div>
<p class="empty">这是模拟交易所账本：不开真实 Binance 单。价格来自本地 K线/OKX 公开行情；手续费按账本费率扣；资金费率按可得公开 funding rate 结算，缺数据时记 0 并标来源。</p>
"""


def render_fresh_start(state: dict[str, Any]) -> str:
    reset = state.get("reset") if isinstance(state.get("reset"), dict) else {}
    db_reset = reset.get("db_reset") if isinstance(reset.get("db_reset"), dict) else {}
    after = db_reset.get("counts_after") if isinstance(db_reset.get("counts_after"), dict) else {}
    paper = state.get("paper_exchange") if isinstance(state.get("paper_exchange"), dict) else {}
    event = state.get("event") if isinstance(state.get("event"), dict) else {}
    archive_root = reset.get("archive_root") or "暂无本轮归档"
    reset_at = parse_dt(reset.get("generated_at"))
    status = "已从零开始" if reset.get("apply") and after else "等待清理"
    rows = [
        ("清理状态", status, f"归档位置：{archive_root}"),
        ("事件库", f"{event.get('events', 0)} 条", "清零后这里只应出现新 paper/open/close/scan 事件。"),
        ("模拟账本", f"{paper.get('open_positions', 0)} 仓", "主 PnL 只看新 paper exchange，不混旧真实残留。"),
        ("清理时间", age_text(reset_at), "归档保留证据，当前运行从 reset receipt 之后重新算。"),
    ]
    return "".join(
        f'<article class="info"><span>{h(title)}</span><b>{h(value)}</b><p>{h(body)}</p></article>'
        for title, value, body in rows
    )


def render_evolution_readiness(state: dict[str, Any]) -> str:
    paper = state.get("paper_exchange") if isinstance(state.get("paper_exchange"), dict) else {}
    fills = paper.get("recent_fills") if isinstance(paper.get("recent_fills"), list) else []
    opens = sum(1 for row in fills if isinstance(row, dict) and row.get("action") == "OPEN")
    closes = sum(1 for row in fills if isinstance(row, dict) and row.get("action") == "CLOSE")
    positions = int(paper.get("open_positions") or 0)
    verdict = "能复盘骨架，暂不能升级策略"
    if closes >= 30:
        verdict = "可以开始小样本进化复核"
    elif positions >= 15:
        verdict = "正在收持仓样本，等平仓闭环"
    rows = [
        ("当前判断", verdict, "不是只等持仓数量；要等开仓、持仓、平仓、费用、行情上下文成套闭环。"),
        ("开仓样本", str(opens), "足够看执行和持仓展示，但还不足以证明策略优劣。"),
        ("平仓样本", str(closes), "进化需要 CLOSED 样本。只有浮盈亏还不能算胜率、PF、回撤。"),
        ("下一步", "继续收完整交易", "优先自然产生 CLOSE；满 30 笔闭环后再看参数升级，满 100 笔更可靠。"),
    ]
    return "".join(
        f'<article class="info"><span>{h(title)}</span><b>{h(value)}</b><p>{h(body)}</p></article>'
        for title, value, body in rows
    )


def render_strategy_table(rows: list[dict[str, str]]) -> str:
    body = "".join(
        f"""
<tr>
  <td><span class="dot {plain_level(row['level'])}"></span>{h(row['name'])}</td>
  <td>{h(row['service'])}</td>
  <td>{h(row['age'])}</td>
  <td>{h(row['opens'])}</td>
  <td>{h(row.get('paper_sample_opens', '0'))}</td>
  <td>{h(row['closes'])}</td>
  <td>{h(row['open_failed'])}</td>
  <td>{h(row['close_failed'])}</td>
  <td>{h(row['open_skipped'])}</td>
  <td class="reason">{h(row.get('note') or plain_strategy_reason(row.get('raw_note') or '', row.get('note_kind') or 'skip'))}{('<small>原始原因：' + h(row['raw_note']) + '</small>') if row.get('raw_note') else ''}{row.get('detail_html', '')}</td>
</tr>
""".strip()
        for row in rows
    )
    return f"""
<div class="table-scroll"><table class="strategy-table">
  <thead><tr><th>策略</th><th>服务</th><th>最新数据</th><th>24h开仓</th><th>其中模拟采样</th><th>24h平仓</th><th>开仓执行失败</th><th>平仓/强平失败</th><th>候选被挡住</th><th>主要原因</th></tr></thead>
  <tbody>{body}</tbody>
</table></div>
"""


def render_attention(items: list[dict[str, Any]]) -> str:
    if not items:
        return '<p class="empty">暂无需要你确认的 P0/P1 事项。P2 观察项在完整详情里，不占用首页确认区。</p>'
    rows = []
    for item in items:
        item_id = str(item.get("item_id") or "")
        priority = str(item.get("priority") or "")
        title = plain_attention_title(item)
        evidence = plain_attention_evidence(item)
        action = plain_attention_action(item)
        rows.append(
            f"""
<tr data-item="{h(item_id)}">
  <td><b>{h(attention_level_label(priority))}</b><small>{h(priority)}</small></td>
  <td>{h(title)}<small>{h(evidence)}</small></td>
  <td>{h(action)}</td>
  <td><button class="icon-btn" onclick="ackItem('{h(item_id)}', this)">确认</button></td>
</tr>
""".strip()
        )
    return f"""
<table>
  <thead><tr><th>级别</th><th>事项</th><th>建议</th><th></th></tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table>
"""


def render_cards(state: dict[str, Any]) -> str:
    account = state["account_summary"]
    evolution = state["evolution"].get("summary") or {}
    expansion = evolution.get("expansion_readiness") if isinstance(evolution.get("expansion_readiness"), dict) else {}
    replay = state["replay"]
    waiting = state.get("waiting") or {}
    waiting_summary = waiting.get("summary") if isinstance(waiting.get("summary"), dict) else {}
    waiting_readiness = waiting.get("readiness") if isinstance(waiting.get("readiness"), dict) else {}
    replay_summary = replay.get("summary") if isinstance(replay.get("summary"), dict) else {}
    parity_summary = state["parity"].get("summary") if isinstance(state["parity"].get("summary"), dict) else {}
    research = state["research"]
    kline_acceptance = research.get("kline_acceptance") if isinstance(research.get("kline_acceptance"), dict) else {}
    stale = account.get("stale_accounts") if isinstance(account.get("stale_accounts"), list) else []
    rows = [
        ("账户资金", f"总额 {number(account.get('wallet_usdt'))} / 可用 {number(account.get('available_usdt'))}", "看账户是否还有可用保证金。B/C 若显示等待推送，不代表要手动强拉余额。"),
        ("策略升级样本", f"可考虑 {expansion.get('ready_count', 0)} / 继续收样 {expansion.get('maturing_count', 0)}", f"过去24小时还缺 {expansion.get('missing_samples_24h', 0)} 个样本。先让系统自然交易，不靠拍脑袋放大。"),
        ("回放验收", plain_status(replay.get("status")), f"已准备 {replay_summary.get('ready_components', 0)}/{replay_summary.get('total_components', 0)} 块；下一步：{plain_status(replay.get('next_action'))}。"),
        ("放开闸门", plain_status(waiting_readiness.get("decision") or waiting.get("status")), f"Top100 实扫 {waiting_summary.get('top100_scanned', 0)} 个；队列中 {waiting_summary.get('active_requests', 0)}；冷却 {waiting_summary.get('active_cooldowns', 0)}。"),
        ("同输入审计", f"{float(parity_summary.get('pass_rate_pct') or 0):.1f}% 通过", f"同一批输入下，已验 {parity_summary.get('gate_cases', 0)} 个策略判断，不一致 {parity_summary.get('mismatched', 0)} 个。"),
        ("K线/深度", plain_status(kline_acceptance.get("status")), "这是以后回测和升级策略的燃料。第一版先看是否在稳定积累，不急着一次补满。"),
        ("服务器清理", f"{state['cleanup']['total_mb']} MB", "这里只列可清理候选。真正删除由维护任务做，回滚证据和重置记录要保留。"),
    ]
    if stale:
        rows.insert(1, ("账户刷新", "等交易所推送", "部分账户不主动查余额，是为了少碰 signed REST，降低币安风控风险。"))
    return "".join(
        f'<article class="info"><span>{h(title)}</span><b>{h(value)}</b><p>{h(body)}</p></article>'
        for title, value, body in rows
    )


def render_cleanup(state: dict[str, Any]) -> str:
    rows = "".join(
        f"<tr><td>{h(row['label'])}</td><td>{h(row['mb'])} MB</td><td>{'保留/按维护计划归档'}</td></tr>"
        for row in state["cleanup"]["rows"]
    )
    return f"""
<table>
  <thead><tr><th>目录</th><th>大小</th><th>处理原则</th></tr></thead>
  <tbody>{rows}</tbody>
</table>
"""


def render_release_gate(state: dict[str, Any]) -> str:
    waiting = state.get("waiting") or {}
    summary = waiting.get("summary") if isinstance(waiting.get("summary"), dict) else {}
    readiness = waiting.get("readiness") if isinstance(waiting.get("readiness"), dict) else {}
    checks = [
        ("队列清空", int(summary.get("active_requests") or 0) == 0, f"当前 {summary.get('active_requests', 0)}"),
        ("无冷却", int(summary.get("active_cooldowns") or 0) == 0, f"当前 {summary.get('active_cooldowns', 0)}"),
        ("无坏请求", int(summary.get("recent_bad") or 0) == 0, f"当前 {summary.get('recent_bad', 0)}"),
        ("不提频", not bool(readiness.get("can_raise_frequency")), "保持保护"),
    ]
    rows = "".join(
        f'<div><span class="dot {"good" if ok else "warn"}"></span><b>{h(label)}</b><small>{h(detail)}</small></div>'
        for label, ok, detail in checks
    )
    return f"""
<div class="gate-grid">{rows}</div>
<p class="empty">判断：{h(readiness.get("decision") or waiting.get("status") or "缺少等待期报表")}。{h(readiness.get("reason") or "")}</p>
"""


def render_html() -> str:
    state = build_state()
    strategies = strategy_rows(state["event"], state["alerts"], state["account"])
    generated = state["generated_at"].strftime("%Y-%m-%d %H:%M:%S")
    alerts = state["alerts"].get("alerts") if isinstance(state["alerts"].get("alerts"), list) else []
    alert_list = "".join(
        f'<li class="{plain_level(a.get("level"))}"><b>{h(a.get("title"))}</b><span>{h(a.get("body"))}</span></li>'
        for a in alerts[:6] if isinstance(a, dict)
    ) or '<li class="good"><b>无红灯</b><span>当前没有需要立即停机的告警。</span></li>'
    event = state["event"]
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>AutoTrading 决策入口</title>
<style>
:root {{
  --bg:#eef2f7; --panel:#ffffff; --ink:#182033; --muted:#667085;
  --line:#d9e0ea; --good:#16a34a; --warn:#d97706; --bad:#dc2626;
  --up:#22c55e; --down:#ef4444;
  --cyan:#0891b2; --blue:#2563eb; --night:#101820; --night2:#17212c;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:linear-gradient(180deg,var(--night) 0,var(--night2) 300px,var(--bg) 301px); color:var(--ink); font:15px/1.58 "Segoe UI", Arial, sans-serif; }}
.wrap {{ max-width:1560px; margin:0 auto; padding:26px 30px; }}
header {{ display:grid; grid-template-columns:1fr auto; gap:16px; align-items:end; margin-bottom:18px; color:#fff; }}
h1 {{ margin:0; font-size:34px; letter-spacing:0; }}
.sub {{ color:#cbd5e1; margin-top:6px; }}
.status {{ padding:10px 14px; border-radius:8px; font-weight:800; color:#fff; border:1px solid rgba(255,255,255,.22); }}
.status.good {{ background:var(--good); }} .status.warn {{ background:var(--warn); }} .status.bad {{ background:var(--bad); }}
.metrics {{ display:grid; grid-template-columns:repeat(7, minmax(0, 1fr)); gap:10px; margin:14px 0 18px; }}
.metric,.panel,.info {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; box-shadow:0 1px 2px rgba(16,24,40,.04); }}
.metric {{ padding:14px; min-height:88px; box-shadow:0 14px 34px rgba(15,23,42,.10); }}
.metric span,.info span {{ color:var(--muted); display:block; font-size:12px; }}
.metric b {{ display:block; font-size:22px; margin-top:8px; }}
.metric.good {{ border-top:4px solid var(--good); }} .metric.warn {{ border-top:4px solid var(--warn); }} .metric.bad {{ border-top:4px solid var(--bad); }}
.paper-summary {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; margin-bottom:12px; }}
.paper-summary div,.paper-card {{ border:1px solid var(--line); border-radius:8px; background:#f8fafc; padding:12px; }}
.paper-summary span,.paper-card span {{ color:var(--muted); display:block; font-size:12px; }}
.paper-summary b,.paper-card b {{ display:block; font-size:20px; margin-top:4px; }}
.paper-cards {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; margin-bottom:12px; }}
.paper-card p {{ margin:6px 0 0; color:var(--muted); }}
.paper-table {{ min-width:1240px; background:#fff; }}
.grid {{ display:grid; grid-template-columns:minmax(0, 1.55fr) minmax(340px, .45fr); gap:16px; align-items:start; }}
.panel {{ padding:16px; margin-bottom:14px; box-shadow:0 10px 28px rgba(15,23,42,.06); }}
.panel h2 {{ margin:0 0 10px; font-size:18px; }}
.cards {{ display:grid; grid-template-columns:repeat(4, minmax(0, 1fr)); gap:12px; }}
.info {{ padding:14px; min-height:112px; }}
.info b {{ display:block; font-size:18px; margin:6px 0; }}
.info p {{ margin:0; color:var(--muted); }}
.table-scroll {{ width:100%; overflow-x:auto; }}
table {{ width:100%; border-collapse:collapse; }}
th,td {{ padding:10px 8px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }}
th {{ color:var(--muted); font-size:12px; font-weight:700; }}
td small {{ display:block; color:var(--muted); margin-top:4px; }}
.strategy-table {{ min-width:1120px; }}
.strategy-table th:last-child,.strategy-table td.reason {{ width:34%; min-width:360px; }}
.strategy-table td.reason {{ color:#263247; }}
.strategy-detail {{ margin-top:10px; border:1px solid #cbd5e1; border-radius:8px; background:#f8fafc; }}
.strategy-detail summary {{ cursor:pointer; padding:8px 10px; font-weight:800; color:#1d4ed8; }}
.detail-note {{ margin:0; padding:0 10px 8px; color:var(--muted); }}
.position-table {{ min-width:1320px; font-size:13px; background:#fff; }}
.position-table th,.position-table td {{ padding:8px 7px; }}
.up {{ color:var(--up); font-weight:800; }}
.down {{ color:var(--down); font-weight:800; }}
.muted {{ color:var(--muted); }}
.dot {{ display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:8px; background:var(--muted); }}
.dot.good {{ background:var(--good); }} .dot.warn {{ background:var(--warn); }} .dot.bad {{ background:var(--bad); }}
.alerts {{ list-style:none; padding:0; margin:0; display:grid; gap:8px; }}
.alerts li {{ border-left:4px solid var(--line); padding:9px 10px; background:#f8fafc; border-radius:6px; }}
.alerts li.good {{ border-color:var(--good); }} .alerts li.warn {{ border-color:var(--warn); }} .alerts li.bad {{ border-color:var(--bad); }}
.alerts b,.alerts span {{ display:block; }} .alerts span {{ color:var(--muted); margin-top:2px; }}
.icon-btn {{ border:0; background:var(--blue); color:white; border-radius:6px; padding:8px 12px; cursor:pointer; font-weight:700; }}
.icon-btn:disabled {{ opacity:.65; cursor:default; }}
.gate-grid {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; }}
.gate-grid div {{ border:1px solid var(--line); border-radius:8px; background:#f8fafc; padding:12px; min-height:76px; }}
.gate-grid b {{ display:block; margin:2px 0 4px; }}
.links {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:8px; }}
.links a {{ color:var(--blue); background:#eff6ff; border:1px solid #bfdbfe; padding:7px 10px; border-radius:6px; text-decoration:none; }}
.top-actions {{ display:flex; flex-wrap:wrap; align-items:center; gap:10px; margin-top:10px; }}
.refresh-btn {{ background:var(--cyan); }}
.refresh-status {{ color:#cbd5e1; font-size:12px; }}
.empty {{ color:var(--muted); }}
@media (max-width: 1280px) {{ .cards {{ grid-template-columns:repeat(2, minmax(0, 1fr)); }} }}
@media (max-width: 980px) {{ .metrics,.cards,.grid {{ grid-template-columns:1fr; }} header {{ grid-template-columns:1fr; }} .wrap {{ padding:18px; }} }}
</style>
</head>
<body>
<main class="wrap">
  <header>
    <div>
      <h1>AutoTrading 决策入口</h1>
      <div class="sub">更新 {h(generated)}。线上首页 5 分钟自动刷新；这里只读现有数据，不请求 Binance。</div>
      <div class="top-actions">
        <button class="icon-btn refresh-btn" onclick="refreshReport(this)" title="同步服务器镜像并重新生成报表，不提交币安队列">刷新报表</button>
        <span id="refreshStatus" class="refresh-status">安全刷新：只更新报表和镜像，不下单，不强拉账户。</span>
      </div>
    </div>
    <div class="status {plain_level(state['overall'])}">{h(status_text(state))}</div>
  </header>
  <section class="metrics">{render_badges(state)}</section>
  <section class="panel">
    <h2>今日重点</h2>
    <div class="cards">{render_cards(state)}</div>
  </section>
  <section class="panel">
    <h2>从零运行状态</h2>
    <div class="cards">{render_fresh_start(state)}</div>
  </section>
  <section class="panel">
    <h2>三策略模拟交易所运行总览</h2>
    {render_paper_exchange(state)}
  </section>
  <section class="panel">
    <h2>复盘 / 进化成熟度</h2>
    <div class="cards">{render_evolution_readiness(state)}</div>
  </section>
  <section class="grid">
    <div>
      <section class="panel">
        <h2>三策略现在是否正常</h2>
        {render_strategy_table(strategies)}
      </section>
      <section class="panel">
        <h2>小放开闸门</h2>
        {render_release_gate(state)}
      </section>
      <section class="panel">
        <h2>你需要确认的事项</h2>
        {render_attention(state['attention_items'])}
      </section>
      <section class="panel">
        <h2>服务器清理计划</h2>
        <p class="empty">先列候选，不直接删除。目标是清掉旧噪音，保留回滚、审计、重置 receipt。</p>
        {render_cleanup(state)}
      </section>
    </div>
    <aside>
      <section class="panel">
        <h2>红黄灯</h2>
        <ul class="alerts">{alert_list}</ul>
      </section>
      <section class="panel">
        <h2>下钻</h2>
        <div class="links">
          <a href="/reports/portal_latest.html">完整旧版详情</a>
          <a href="/reports/replay_readiness_latest.md">Replay 验收</a>
          <a href="/reports/waiting_period_optimization_latest.html">等待期优化</a>
          <a href="/reports/long_term_skeleton_latest.md">长期目标骨架</a>
          <a href="/reports/strategy_evolution_latest.html">策略进化</a>
          <a href="/reports/research_store_summary_latest.md">研究仓</a>
          <a href="/api/attention">确认事项 API</a>
        </div>
      </section>
    </aside>
  </section>
</main>
<script>
async function ackItem(itemId, btn) {{
  if (!itemId || !btn) return;
  btn.disabled = true;
  btn.textContent = '确认中';
  try {{
    const resp = await fetch('/api/attention/ack', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{item_id: itemId, user: 'decision_portal'}})
    }});
    const data = await resp.json();
    if (data.ok) {{
      btn.textContent = '已确认';
      const row = btn.closest('tr');
      if (row) row.style.opacity = '0.45';
    }} else {{
      btn.textContent = '失败';
      btn.disabled = false;
      alert(data.error || '确认失败');
    }}
  }} catch (err) {{
    btn.textContent = '网络错误';
    btn.disabled = false;
  }}
}}
async function refreshReport(btn) {{
  const status = document.getElementById('refreshStatus');
  if (btn) {{
    btn.disabled = true;
    btn.textContent = '刷新中';
  }}
  if (status) status.textContent = '正在刷新报表。不会下单，不会强拉 Binance 账户。';
  try {{
    const resp = await fetch('/api/report/refresh', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{user: 'decision_portal'}})
    }});
    const data = await resp.json();
    if (!data.ok) throw new Error(data.error || '刷新失败');
    if (status) status.textContent = data.action === 'already_running'
      ? '已有刷新任务在跑，等它完成后页面会自动变新。'
      : '刷新任务已启动，稍等 30-90 秒后自动重载。';
    setTimeout(() => window.location.reload(), 45000);
  }} catch (err) {{
    if (status) status.textContent = '刷新启动失败：' + (err.message || err);
    if (btn) {{
      btn.disabled = false;
      btn.textContent = '刷新报表';
    }}
  }}
}}
</script>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate concise decision portal")
    parser.add_argument("--out-dir", default=str(REPORTS_DIR))
    args = parser.parse_args(argv)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    html_text = render_html()
    (out_dir / "index.html").write_text(html_text, encoding="utf-8")
    (out_dir / "decision_portal_latest.html").write_text(html_text, encoding="utf-8")
    print(json.dumps({"status": "ok", "index": str(out_dir / "index.html")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
