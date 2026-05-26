"""生成三账号当前盈亏 HTML 快照。"""

from __future__ import annotations

import html
import sys
from datetime import datetime
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
if not (ROOT / "binance_client.py").exists() and (ROOT / "交易客户端").exists():
    ROOT = ROOT
elif not (ROOT / "binance_client.py").exists() and (ROOT.parent / "binance_client.py").exists():
    ROOT = ROOT.parent
elif not (ROOT / "binance_client.py").exists() and (ROOT.parent / "交易客户端").exists():
    ROOT = ROOT.parent
REPORT_DIR = ROOT / "复盘报告"
REPORT_DIR.mkdir(exist_ok=True)
PORTAL_URL = "file:///F:/AutoTrading/reports/index.html"

if (ROOT / "交易客户端").exists():
    sys.path.insert(0, str(ROOT / "交易客户端"))
else:
    sys.path.insert(0, str(ROOT))

ACCOUNTS = [
    ("A", "v11", "半木夏", "binance_client", "BinanceClient", 30.0),
    ("B", "v16", "订单流 CVD/OFI", "binance_client_v2", "BinanceClientV2", 10.0),
    ("C", "v14", "四维度评分", "binance_client_v3", "BinanceClientV3", 12.0),
]


def parse_balance(bal):
    if isinstance(bal, dict) and "assets" in bal:
        usdt = next((x for x in bal["assets"] if x.get("asset") == "USDT"), {})
        return (
            float(usdt.get("walletBalance") or usdt.get("balance") or 0),
            float(usdt.get("availableBalance") or 0),
            float(usdt.get("marginBalance") or usdt.get("crossWalletBalance") or usdt.get("walletBalance") or 0),
        )
    if isinstance(bal, list):
        usdt = next((x for x in bal if x.get("asset") == "USDT"), {})
        return (
            float(usdt.get("walletBalance") or usdt.get("balance") or 0),
            float(usdt.get("availableBalance") or 0),
            float(usdt.get("marginBalance") or usdt.get("crossWalletBalance") or usdt.get("balance") or 0),
        )
    if isinstance(bal, dict):
        return (
            float(bal.get("totalWalletBalance") or 0),
            float(bal.get("availableBalance") or 0),
            float(bal.get("totalMarginBalance") or 0),
        )
    return 0.0, 0.0, 0.0


def position_row(p):
    amt = float(p.get("positionAmt", 0) or 0)
    side = (p.get("positionSide") or ("LONG" if amt > 0 else "SHORT")).upper()
    entry = float(p.get("entryPrice", 0) or 0)
    mark = float(p.get("markPrice", 0) or 0)
    lev = float(p.get("leverage", 4) or 4)
    upnl = float(p.get("unRealizedProfit", p.get("unrealizedProfit", 0)) or 0)
    loss = 0.0
    if entry > 0 and mark > 0:
        loss = max(0.0, ((entry - mark) / entry * 100 * lev) if side == "LONG" else ((mark - entry) / entry * 100 * lev))
    notional = abs(amt) * mark
    return {
        "symbol": p.get("symbol", ""),
        "side": side,
        "qty": abs(amt),
        "entry": entry,
        "mark": mark,
        "upnl": upnl,
        "loss": loss,
        "lev": lev,
        "notional": notional,
        "margin": notional / lev if lev else 0.0,
    }


def collect():
    accounts = []
    for key, version, desc, module_name, class_name, hard in ACCOUNTS:
        module = __import__(module_name)
        client = getattr(module, class_name)()
        wallet, available, margin = parse_balance(client.get_balance())
        rows = [position_row(p) for p in client.get_positions() if abs(float(p.get("positionAmt", 0) or 0)) > 0.0001]
        rows.sort(key=lambda x: x["upnl"])
        accounts.append({
            "key": key,
            "version": version,
            "desc": desc,
            "hard": hard,
            "wallet": wallet,
            "available": available,
            "margin": margin,
            "positions": rows,
            "upnl": sum(r["upnl"] for r in rows),
            "longs": sum(1 for r in rows if r["side"] == "LONG"),
            "shorts": sum(1 for r in rows if r["side"] == "SHORT"),
            "notional": sum(r["notional"] for r in rows),
            "used_margin": sum(r["margin"] for r in rows),
            "over_hard": sum(1 for r in rows if r["loss"] >= hard),
        })
    return accounts


def money(v):
    return f"{v:,.2f}"


def css_class(v):
    return "up" if v >= 0 else "down"


def render_table(rows, hard):
    if not rows:
        return '<div class="empty">当前无持仓</div>'
    out = ['<table><thead><tr><th>币种</th><th>方向</th><th>数量</th><th>入场价</th><th>标记价</th><th>浮盈亏</th><th>亏损%</th><th>名义价值</th></tr></thead><tbody>']
    for r in rows:
        risk = " risk" if r["loss"] >= hard else ""
        side_cls = "long" if r["side"] == "LONG" else "short"
        out.append(
            f'<tr class="{risk}">'
            f'<td class="sym">{html.escape(r["symbol"])}</td>'
            f'<td><span class="pill {side_cls}">{r["side"]}</span></td>'
            f'<td>{r["qty"]:,.6g}</td>'
            f'<td>{r["entry"]:,.8g}</td>'
            f'<td>{r["mark"]:,.8g}</td>'
            f'<td class="{css_class(r["upnl"])}">{r["upnl"]:+,.2f}</td>'
            f'<td class="{"down" if r["loss"] > 0 else "muted"}">{r["loss"]:,.2f}%</td>'
            f'<td>{r["notional"]:,.2f}</td>'
            '</tr>'
        )
    out.append("</tbody></table>")
    return "\n".join(out)


def build_html(accounts):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total_wallet = sum(a["wallet"] for a in accounts)
    total_available = sum(a["available"] for a in accounts)
    total_margin = sum(a["margin"] for a in accounts)
    total_upnl = sum(a["upnl"] for a in accounts)
    total_pos = sum(len(a["positions"]) for a in accounts)
    total_over = sum(a["over_hard"] for a in accounts)
    all_positions = []
    for a in accounts:
        for p in a["positions"]:
            all_positions.append((a, p))
    top_profit = sorted(all_positions, key=lambda x: x[1]["upnl"], reverse=True)[:12]
    top_loss = sorted(all_positions, key=lambda x: x[1]["upnl"])[:12]

    cards = []
    for a in accounts:
        cards.append(f"""
        <section class="account">
          <div class="account-head">
            <div>
              <h2>账号 {a['key']} · {a['version']}</h2>
              <p>{html.escape(a['desc'])}</p>
            </div>
            <div class="status {'bad' if a['over_hard'] else 'ok'}">{'有硬顶风险' if a['over_hard'] else '风控正常'}</div>
          </div>
          <div class="metric-grid compact">
            <div><span>wallet</span><strong>{money(a['wallet'])}</strong></div>
            <div><span>available</span><strong>{money(a['available'])}</strong></div>
            <div><span>equity</span><strong>{money(a['margin'])}</strong></div>
            <div><span>浮盈亏</span><strong class="{css_class(a['upnl'])}">{a['upnl']:+,.2f}</strong></div>
            <div><span>持仓</span><strong>{len(a['positions'])}</strong><small>{a['longs']} 多 / {a['shorts']} 空</small></div>
            <div><span>名义仓位</span><strong>{money(a['notional'])}</strong></div>
          </div>
          {render_table(a['positions'], a['hard'])}
        </section>
        """)

    def rank_block(title, items):
        rows = []
        for a, p in items:
            rows.append(
                f'<tr><td>{a["key"]}/{a["version"]}</td><td class="sym">{html.escape(p["symbol"])}</td>'
                f'<td><span class="pill {"long" if p["side"]=="LONG" else "short"}">{p["side"]}</span></td>'
                f'<td class="{css_class(p["upnl"])}">{p["upnl"]:+,.2f}</td><td>{p["loss"]:.2f}%</td><td>{p["notional"]:,.2f}</td></tr>'
            )
        return f"""
        <section class="rank">
          <h2>{title}</h2>
          <table><thead><tr><th>账号</th><th>币种</th><th>方向</th><th>浮盈亏</th><th>亏损%</th><th>名义价值</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
        </section>
        """

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>三账号盈亏快照</title>
<style>
:root {{
  --bg:#0b1220; --panel:#111827; --panel2:#0f172a; --line:#263244;
  --text:#e5e7eb; --muted:#94a3b8; --up:#22c55e; --down:#ef4444;
  --cyan:#06b6d4; --yellow:#eab308;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; font-family:Inter, "Microsoft YaHei", Arial, sans-serif; background:var(--bg); color:var(--text); }}
.wrap {{ max-width:1440px; margin:0 auto; padding:28px; }}
.topbar {{ display:flex; justify-content:flex-start; gap:12px; margin-bottom:16px; }}
.back-link {{ display:inline-flex; align-items:center; gap:8px; text-decoration:none; color:#e5e7eb; background:#0f172a; border:1px solid #263244; border-radius:8px; padding:10px 12px; font-weight:700; }}
.back-link:hover {{ border-color:#38bdf8; }}
.hero {{ display:flex; justify-content:space-between; gap:24px; align-items:flex-end; margin-bottom:22px; }}
h1 {{ margin:0 0 8px; font-size:30px; letter-spacing:0; }}
h2 {{ margin:0; font-size:18px; }}
p {{ margin:0; color:var(--muted); }}
.metric-grid {{ display:grid; grid-template-columns:repeat(6, minmax(0,1fr)); gap:10px; margin:18px 0; }}
.metric-grid > div {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px; min-height:76px; }}
.metric-grid span {{ display:block; color:var(--muted); font-size:12px; margin-bottom:8px; text-transform:uppercase; }}
.metric-grid strong {{ font-size:22px; }}
.metric-grid small {{ display:block; color:var(--muted); margin-top:4px; }}
.compact {{ grid-template-columns:repeat(6, minmax(0,1fr)); }}
.account, .rank {{ background:var(--panel2); border:1px solid var(--line); border-radius:8px; padding:16px; margin:16px 0; }}
.account-head {{ display:flex; justify-content:space-between; gap:16px; align-items:center; margin-bottom:12px; }}
.status {{ border-radius:999px; padding:6px 10px; font-size:12px; border:1px solid var(--line); }}
.status.ok {{ color:var(--up); border-color:rgba(34,197,94,.35); }}
.status.bad {{ color:var(--down); border-color:rgba(239,68,68,.45); }}
table {{ width:100%; border-collapse:collapse; overflow:hidden; border-radius:8px; }}
th, td {{ padding:9px 10px; border-bottom:1px solid var(--line); text-align:right; font-size:13px; white-space:nowrap; }}
th:first-child, td:first-child, td.sym {{ text-align:left; }}
th {{ color:var(--muted); font-weight:600; background:rgba(148,163,184,.06); }}
tr:hover td {{ background:rgba(255,255,255,.03); }}
tr.risk td {{ background:rgba(239,68,68,.08); }}
.up {{ color:var(--up); }} .down {{ color:var(--down); }} .muted {{ color:var(--muted); }}
.pill {{ display:inline-flex; min-width:54px; justify-content:center; padding:3px 7px; border-radius:999px; font-weight:700; font-size:11px; }}
.pill.long {{ color:var(--up); background:rgba(34,197,94,.12); }}
.pill.short {{ color:var(--down); background:rgba(239,68,68,.12); }}
.summary {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
.empty {{ color:var(--muted); padding:24px; border:1px dashed var(--line); border-radius:8px; }}
@media (max-width: 980px) {{
  .wrap {{ padding:16px; }}
  .hero {{ display:block; }}
  .metric-grid, .compact, .summary {{ grid-template-columns:1fr 1fr; }}
  table {{ display:block; overflow-x:auto; }}
}}
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar"><a class="back-link" href="{PORTAL_URL}">返回总入口</a></div>
  <header class="hero">
    <div>
      <h1>三账号实时盈亏快照</h1>
      <p>生成时间：{ts} · 腾讯云新加坡主节点 · USDT 永续模拟盘</p>
    </div>
  </header>
  <section class="metric-grid">
    <div><span>Total Wallet</span><strong>{money(total_wallet)}</strong></div>
    <div><span>Available</span><strong>{money(total_available)}</strong></div>
    <div><span>Equity</span><strong>{money(total_margin)}</strong></div>
    <div><span>Unrealized PnL</span><strong class="{css_class(total_upnl)}">{total_upnl:+,.2f}</strong></div>
    <div><span>Positions</span><strong>{total_pos}</strong></div>
    <div><span>Over Hard Stop</span><strong class="{'down' if total_over else 'up'}">{total_over}</strong></div>
  </section>
  <div class="summary">
    {rank_block('全局浮盈榜', top_profit)}
    {rank_block('全局风险/浮亏榜', top_loss)}
  </div>
  {''.join(cards)}
</div>
</body>
</html>"""


def main():
    accounts = collect()
    html_text = build_html(accounts)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = REPORT_DIR / f"account_snapshot_{ts}.html"
    latest = REPORT_DIR / "account_snapshot_latest.html"
    out.write_text(html_text, encoding="utf-8")
    latest.write_text(html_text, encoding="utf-8")
    print(out)
    print(latest)


if __name__ == "__main__":
    main()
