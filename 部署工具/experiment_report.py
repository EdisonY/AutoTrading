"""Render shadow experiment results to Markdown and HTML."""

from __future__ import annotations

import argparse
import html
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
CST = timezone(timedelta(hours=8))
PORTAL_URL = "file:///F:/AutoTrading/reports/index.html"


STATUS_LABELS = {
    "approved_candidate": "候选通过",
    "observe": "继续观察",
    "reject": "拒绝",
    "archive": "归档",
    "archive_or_rework": "归档/重做",
}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def fix_text(value: Any) -> str:
    text = str(value if value is not None else "-")
    if not text:
        return "-"
    if any(ch in text for ch in ("鍘", "鐜", "绛", "瀹", "鏅", "杩", "闃", "褰")):
        try:
            repaired = text.encode("gbk").decode("utf-8")
            if repaired.count("�") <= text.count("?"):
                return repaired
        except Exception:
            pass
    return text


def h(value: Any) -> str:
    return html.escape(fix_text(value))


def num(value: Any) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def status_label(status: Any) -> str:
    key = str(status or "observe")
    return STATUS_LABELS.get(key, key)


def status_class(status: Any) -> str:
    key = str(status or "")
    if key == "approved_candidate":
        return "good"
    if key in {"reject", "archive", "archive_or_rework"}:
        return "bad"
    return "watch"


def signed(value: Any) -> str:
    return f"{num(value):+.2f}"


def money(value: Any) -> str:
    return f"{num(value):,.2f}"


def render_markdown(results: list[dict[str, Any]]) -> str:
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    approved = sum(1 for r in results if r.get("promotion_status") == "approved_candidate")
    observe = sum(1 for r in results if r.get("promotion_status") == "observe")
    rejected = sum(1 for r in results if status_class(r.get("promotion_status")) == "bad")
    lines = [
        "# 影子实验回放与晋级判定",
        "",
        f"生成时间: {now}",
        f"实验数: {len(results)} / 候选通过: {approved} / 继续观察: {observe} / 拒绝或归档: {rejected}",
        "",
        "## 实验总览",
        "",
        "| 实验 | 策略 | 样本窗口 | 样本数 | 原始PnL | 影子PnL | PnL差值 | 过滤数 | 避免亏损 | 错过盈利 | 硬顶变化 | 晋级状态 |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for r in results:
        delta = num(r.get("shadow_pnl")) - num(r.get("original_pnl"))
        hard = f"{r.get('hard_stop_before', 0)} -> {r.get('hard_stop_after', 0)}"
        lines.append(
            f"| {fix_text(r.get('experiment_id'))} | {fix_text(r.get('base_strategy'))} | {fix_text(r.get('sample_window'))} | "
            f"{r.get('sample_trades', 0)} | {signed(r.get('original_pnl'))} | {signed(r.get('shadow_pnl'))} | {delta:+.2f} | "
            f"{r.get('filtered_trades', 0)} | {money(r.get('avoided_loss'))} | {money(r.get('missed_profit'))} | {hard} | {status_label(r.get('promotion_status'))} |"
        )
    lines += ["", "## 执行原则", ""]
    lines += [
        "- 候选通过: 只表示可以进入人工审批，不直接改实盘。",
        "- 继续观察: 需要更多样本或纸面撮合验证。",
        "- 拒绝/归档: 当前样本下不应晋级。",
        "",
        "*由 experiment_report.py 自动生成。*",
    ]
    return "\n".join(lines)


def render_html(results: list[dict[str, Any]], title: str) -> str:
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    approved = sum(1 for r in results if r.get("promotion_status") == "approved_candidate")
    observe = sum(1 for r in results if r.get("promotion_status") == "observe")
    rejected = sum(1 for r in results if status_class(r.get("promotion_status")) == "bad")
    original_total = sum(num(r.get("original_pnl")) for r in results)
    shadow_total = sum(num(r.get("shadow_pnl")) for r in results)

    metrics = [
        ("实验数", len(results), "latest.jsonl 当前批次"),
        ("候选通过", approved, "进入人工审批队列"),
        ("继续观察", observe, "需要更多样本"),
        ("拒绝/归档", rejected, "当前不晋级"),
        ("原始PnL", signed(original_total), "样本合计"),
        ("影子PnL", signed(shadow_total), f"差值 {shadow_total - original_total:+.2f}"),
    ]
    metric_html = "".join(
        f"<article><span>{h(k)}</span><b>{h(v)}</b><em>{h(desc)}</em></article>" for k, v, desc in metrics
    )

    rows = []
    cards = []
    for r in results:
        delta = num(r.get("shadow_pnl")) - num(r.get("original_pnl"))
        delta_class = "pos" if delta >= 0 else "neg"
        status = r.get("promotion_status")
        hard = f"{r.get('hard_stop_before', 0)} -> {r.get('hard_stop_after', 0)}"
        rows.append(
            "<tr>"
            f"<td class=\"experiment-id\">{h(r.get('experiment_id'))}</td>"
            f"<td>{h(r.get('base_strategy'))}</td>"
            f"<td>{h(r.get('sample_window'))}</td>"
            f"<td>{h(r.get('sample_trades', 0))}</td>"
            f"<td>{signed(r.get('original_pnl'))}</td>"
            f"<td>{signed(r.get('shadow_pnl'))}</td>"
            f"<td class=\"{delta_class}\">{delta:+.2f}</td>"
            f"<td>{h(r.get('filtered_trades', 0))}</td>"
            f"<td>{money(r.get('avoided_loss'))}</td>"
            f"<td>{money(r.get('missed_profit'))}</td>"
            f"<td>{h(hard)}</td>"
            f"<td><span class=\"badge {status_class(status)}\">{h(status_label(status))}</span></td>"
            "</tr>"
        )
        notes = "".join(f"<li>{h(note)}</li>" for note in (r.get("notes") or []))
        cases = r.get("source_cases") or []
        case_text = ", ".join(str(case) for case in cases[:6]) if cases else "无源案例"
        cards.append(
            f"""
<article class="experiment-card {status_class(status)}">
  <header>
    <div>
      <span>{h(r.get('base_strategy'))} / {h(r.get('change_type') or 'experiment')}</span>
      <h3>{h(r.get('experiment_id'))}</h3>
    </div>
    <b>{h(status_label(status))}</b>
  </header>
  <div class="card-grid">
    <div><span>原始PnL</span><strong>{signed(r.get('original_pnl'))}</strong></div>
    <div><span>影子PnL</span><strong>{signed(r.get('shadow_pnl'))}</strong></div>
    <div><span>PnL差值</span><strong class="{delta_class}">{delta:+.2f}</strong></div>
    <div><span>过滤数</span><strong>{h(r.get('filtered_trades', 0))}</strong></div>
    <div><span>避免亏损</span><strong>{money(r.get('avoided_loss'))}</strong></div>
    <div><span>错过盈利</span><strong>{money(r.get('missed_profit'))}</strong></div>
    <div><span>硬顶变化</span><strong>{h(hard)}</strong></div>
    <div><span>门禁</span><strong>{'通过' if r.get('gate_passed') else '未通过'}</strong></div>
  </div>
  <p><b>候选:</b> {h(r.get('candidate_id') or '无')}</p>
  <p><b>源案例:</b> {h(case_text)}</p>
  <ul>{notes or '<li>无备注</li>'}</ul>
</article>
""".strip()
        )

    table_html = f"""
<table>
  <thead><tr><th>实验</th><th>策略</th><th>样本窗口</th><th>样本</th><th>原始PnL</th><th>影子PnL</th><th>差值</th><th>过滤</th><th>避免亏损</th><th>错过盈利</th><th>硬顶</th><th>状态</th></tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table>
""".strip()

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{h(title)}</title>
<style>
:root {{
  --bg:#eef3f9; --panel:#ffffff; --text:#172033; --muted:#64748b; --line:#d7e0ec;
  --good:#15803d; --bad:#b91c1c; --watch:#b45309; --blue:#2563eb;
  --good-bg:#ecfdf3; --bad-bg:#fff1f2; --watch-bg:#fffbeb;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,"Microsoft YaHei",sans-serif; }}
main {{ max-width:1280px; margin:0 auto; padding:24px; }}
.topbar {{ display:flex; justify-content:space-between; align-items:center; gap:12px; margin-bottom:18px; }}
.back-link {{ display:inline-flex; align-items:center; text-decoration:none; color:#0f172a; background:#fff; border:1px solid var(--line); border-radius:8px; padding:10px 12px; font-weight:700; }}
.back-link:hover {{ border-color:#94a3b8; background:#f8fbff; }}
.hero {{ display:flex; justify-content:space-between; gap:18px; align-items:flex-end; padding-bottom:18px; border-bottom:1px solid var(--line); }}
h1 {{ margin:0; font-size:30px; letter-spacing:0; }}
.hero p {{ margin:8px 0 0; color:var(--muted); line-height:1.6; }}
.time {{ color:var(--muted); font-size:13px; white-space:nowrap; }}
.metrics {{ display:grid; grid-template-columns:repeat(6,1fr); gap:10px; margin:18px 0; }}
.metrics article,.panel,.experiment-card {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; }}
.metrics article {{ padding:14px; min-height:86px; }}
.metrics span,.metrics em {{ display:block; color:var(--muted); font-size:12px; font-style:normal; }}
.metrics b {{ display:block; margin:8px 0; font-size:22px; }}
.panel {{ padding:16px; margin:18px 0; overflow:auto; }}
.panel h2,.cards h2,.rules h2 {{ margin:0 0 12px; font-size:18px; }}
table {{ width:100%; border-collapse:collapse; min-width:1050px; }}
th,td {{ padding:10px 11px; border-bottom:1px solid #e7edf6; text-align:left; font-size:13px; vertical-align:top; }}
th {{ color:#334155; background:#f1f5f9; font-weight:800; }}
tr:hover td {{ background:#f8fbff; }}
.experiment-id {{ max-width:290px; overflow-wrap:anywhere; }}
.pos {{ color:var(--good); font-weight:800; }}
.neg {{ color:var(--bad); font-weight:800; }}
.badge {{ display:inline-flex; border-radius:999px; padding:4px 8px; font-size:12px; font-weight:800; }}
.badge.good,.experiment-card.good > header b {{ color:var(--good); background:var(--good-bg); }}
.badge.bad,.experiment-card.bad > header b {{ color:var(--bad); background:var(--bad-bg); }}
.badge.watch,.experiment-card.watch > header b {{ color:var(--watch); background:var(--watch-bg); }}
.cards {{ margin-top:20px; }}
.experiment-card {{ padding:16px; margin:12px 0; border-top:4px solid var(--blue); }}
.experiment-card.good {{ border-top-color:var(--good); }}
.experiment-card.bad {{ border-top-color:var(--bad); }}
.experiment-card.watch {{ border-top-color:var(--watch); }}
.experiment-card header {{ display:flex; justify-content:space-between; gap:14px; align-items:flex-start; margin-bottom:14px; }}
.experiment-card header span {{ color:var(--muted); font-size:12px; }}
.experiment-card h3 {{ margin:5px 0 0; font-size:17px; overflow-wrap:anywhere; }}
.experiment-card header b {{ border-radius:999px; padding:6px 10px; white-space:nowrap; }}
.card-grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:8px; margin-bottom:12px; }}
.card-grid div {{ border:1px solid var(--line); border-radius:8px; padding:10px; background:#fbfdff; }}
.card-grid span {{ display:block; color:var(--muted); font-size:12px; margin-bottom:5px; }}
.card-grid strong {{ font-size:16px; }}
.experiment-card p {{ margin:8px 0; color:#334155; line-height:1.55; overflow-wrap:anywhere; }}
.experiment-card ul {{ margin:10px 0 0; padding-left:20px; color:#40516c; line-height:1.7; }}
.rules {{ margin-top:18px; background:#0f172a; color:#e5e7eb; border-radius:8px; padding:16px; }}
.rules h2 {{ color:#fff; }}
.rules p {{ margin:8px 0; color:#cbd5e1; line-height:1.65; }}
@media (max-width:1000px) {{ .metrics,.card-grid {{ grid-template-columns:repeat(2,1fr); }} .hero {{ display:block; }} .time {{ margin-top:8px; }} }}
@media (max-width:640px) {{ main {{ padding:14px; }} .metrics,.card-grid {{ grid-template-columns:1fr; }} .experiment-card header {{ flex-direction:column; }} }}
</style>
</head>
<body>
<main>
  <div class="topbar"><a class="back-link" href="{PORTAL_URL}">返回总入口</a></div>
  <section class="hero">
    <div>
      <h1>影子实验回放与晋级判定</h1>
      <p>只读取同步日志做离线验证，不新增实盘进程，不直接访问币安。先看总览，再看单个实验是否值得进入人工审批。</p>
    </div>
    <div class="time">生成时间 {h(now)}</div>
  </section>
  <section class="metrics">{metric_html}</section>
  <section class="panel"><h2>实验总览</h2>{table_html}</section>
  <section class="cards"><h2>逐实验说明</h2>{''.join(cards)}</section>
  <section class="rules">
    <h2>执行原则</h2>
    <p>候选通过只表示可以进入人工审批，不直接改实盘；继续观察需要更多样本或纸面撮合验证；拒绝/归档表示当前样本下不应晋级。</p>
  </section>
</main>
</body>
</html>"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render shadow experiment report")
    parser.add_argument("--results", default=str(ROOT / "experiments" / "results" / "latest.jsonl"))
    parser.add_argument("--out-dir", default=str(ROOT / "reports"))
    args = parser.parse_args(argv)
    results = read_jsonl(Path(args.results))
    content = render_markdown(results)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(CST).strftime("%Y-%m-%d")
    md_path = out_dir / f"shadow_experiments_{date_str}.md"
    html_path = out_dir / f"shadow_experiments_{date_str}.html"
    md_path.write_text(content, encoding="utf-8")
    html_text = render_html(results, f"影子实验回放与晋级判定 - {date_str}")
    html_path.write_text(html_text, encoding="utf-8")
    (out_dir / "shadow_experiments_latest.html").write_text(html_text, encoding="utf-8")
    print(f"报告已生成: {md_path}")
    print(f"HTML已生成: {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
