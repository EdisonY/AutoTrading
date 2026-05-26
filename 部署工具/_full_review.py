"""全项目详细复盘"""
import json, sys, subprocess
from pathlib import Path
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, '/opt/crypto-auto-trader')

def load_trades(f):
    trades = []
    if Path(f).exists():
        with open(f) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    trades.append(json.loads(line))
    return trades

def get_balance_info(client_module):
    try:
        mod = __import__(client_module)
        c = mod.get_client()
        bal = c.get_balance()
        wallet = avail = 0
        if isinstance(bal, dict):
            for item in bal.get("assets", []):
                if item.get("asset") == "USDT":
                    wallet = float(item.get("walletBalance", 0))
                    avail = float(item.get("availableBalance", 0))
        elif isinstance(bal, list):
            for item in bal:
                if item.get("asset") == "USDT":
                    wallet = float(item.get("walletBalance", 0))
                    avail = float(item.get("availableBalance", 0))
        positions = c.get_positions()
        pos = 0
        if isinstance(positions, list):
            for p in positions:
                if float(p.get("positionAmt", 0)) != 0:
                    pos += 1
        return {"wallet": wallet, "avail": avail, "pos": pos}
    except Exception as e:
        return {"wallet": 0, "avail": 0, "pos": 0}

def review(label, trades_file, balance_info):
    trades = load_trades(trades_file)
    print("")
    print("=" * 70)
    print("  " + label)
    print("=" * 70)

    if balance_info:
        print("  余额: wallet=%.2f avail=%.2f" % (balance_info["wallet"], balance_info["avail"]))
        print("  持仓: %d个" % balance_info["pos"])

    total = sum(t.get("pnl_usd", 0) for t in trades)
    wins = [t for t in trades if t.get("pnl_pct", 0) > 0]
    losses = [t for t in trades if t.get("pnl_pct", 0) <= 0]
    wr = len(wins) / max(len(trades), 1) * 100
    avg_win = sum(t.get("pnl_usd", 0) for t in wins) / max(len(wins), 1)
    avg_loss = sum(t.get("pnl_usd", 0) for t in losses) / max(len(losses), 1)
    rr = abs(avg_win / avg_loss) if avg_loss else 0

    first_time = min((t.get("entry_time", "9999") for t in trades), default="?")
    days_running = 0
    try:
        ft = datetime.strptime(str(first_time)[:19], "%Y-%m-%d %H:%M:%S")
        days_running = (datetime.now() - ft).days
    except:
        pass

    print("")
    print("  【历史累计】")
    print("  运行天数: %d天 | 首笔: %s" % (days_running, str(first_time)[:10]))
    print("  总交易: %d笔 | 日均: %.0f笔" % (len(trades), len(trades) / max(days_running, 1)))
    print("  胜率: %.1f%% | 盈亏比: %.2f" % (wr, rr))
    print("  盈利笔: %d | 亏损笔: %d" % (len(wins), len(losses)))
    print("  累计PnL: $%.2f | 盈利合计: $%.2f | 亏损合计: $%.2f" % (
        total, sum(t.get("pnl_usd", 0) for t in wins), sum(t.get("pnl_usd", 0) for t in losses)))
    print("  盈利笔均: $%.2f | 亏损笔均: $%.2f" % (avg_win, avg_loss))

    print("")
    print("  【近7天逐日】")
    daily = defaultdict(lambda: {"count": 0, "pnl": 0, "wins": 0})
    for t in trades:
        day = str(t.get("exit_time", ""))[:10]
        if day >= "2026-05-04":
            daily[day]["count"] += 1
            daily[day]["pnl"] += t.get("pnl_usd", 0)
            if t.get("pnl_pct", 0) > 0:
                daily[day]["wins"] += 1

    for day in sorted(daily.keys()):
        d = daily[day]
        dwr = d["wins"] / max(d["count"], 1) * 100
        day_trades = [t for t in trades if day in str(t.get("exit_time", ""))]
        top_win = max(day_trades, key=lambda t: t.get("pnl_usd", 0), default={})
        top_loss = min(day_trades, key=lambda t: t.get("pnl_usd", 0), default={})
        print("    %s: %3d笔 胜率%5.1f%% PnL=$%+.2f | 最佳:%s($%+.0f) 最差:%s($%+.0f)" % (
            day, d["count"], dwr, d["pnl"],
            top_win.get("symbol", "?"), top_win.get("pnl_usd", 0),
            top_loss.get("symbol", "?"), top_loss.get("pnl_usd", 0)))

    print("")
    print("  【退出原因分布】")
    reasons = defaultdict(lambda: {"count": 0, "pnl": 0})
    for t in trades:
        r = t.get("exit_reason", "?")
        reasons[r]["count"] += 1
        reasons[r]["pnl"] += t.get("pnl_usd", 0)
    for r, d in sorted(reasons.items(), key=lambda x: -x[1]["pnl"]):
        print("    %-20s: %4d笔 PnL=$%+.2f" % (r, d["count"], d["pnl"]))

    print("")
    print("  【币种表现TOP5盈利】")
    sym_stats = defaultdict(lambda: {"count": 0, "pnl": 0, "wins": 0})
    for t in trades:
        sym = t.get("symbol", "")
        sym_stats[sym]["count"] += 1
        sym_stats[sym]["pnl"] += t.get("pnl_usd", 0)
        if t.get("pnl_pct", 0) > 0:
            sym_stats[sym]["wins"] += 1

    sorted_syms = sorted(sym_stats.items(), key=lambda x: x[1]["pnl"])
    for sym, s in sorted_syms[-5:][::-1]:
        swr = s["wins"] / max(s["count"], 1) * 100
        print("    %-25s %3d笔 胜率%5.1f%% PnL=$%+.2f" % (sym, s["count"], swr, s["pnl"]))

    print("  【币种表现TOP5亏损】")
    for sym, s in sorted_syms[:5]:
        swr = s["wins"] / max(s["count"], 1) * 100
        print("    %-25s %3d笔 胜率%5.1f%% PnL=$%+.2f" % (sym, s["count"], swr, s["pnl"]))

    print("")
    print("  【月度表现】")
    monthly = defaultdict(lambda: {"count": 0, "pnl": 0, "wins": 0})
    for t in trades:
        month = str(t.get("exit_time", ""))[:7]
        monthly[month]["count"] += 1
        monthly[month]["pnl"] += t.get("pnl_usd", 0)
        if t.get("pnl_pct", 0) > 0:
            monthly[month]["wins"] += 1
    for month in sorted(monthly.keys()):
        d = monthly[month]
        mwr = d["wins"] / max(d["count"], 1) * 100
        print("    %s: %3d笔 胜率%5.1f%% PnL=$%+.2f" % (month, d["count"], mwr, d["pnl"]))


info_a = get_balance_info("binance_client")
info_b = get_balance_info("binance_client_v2")
info_c = get_balance_info("binance_client_v3")

review("Account A (v11)", "scanner_data/trades.jsonl", info_a)
review("Account B (v13-v2)", "scanner_data_v13/trades.jsonl", info_b)
review("Account C (v14)", "scanner_data_v14/trades.jsonl", info_c)

# 汇总
print("")
print("=" * 70)
print("  三账号汇总")
print("=" * 70)
all_trades = []
for f in ["scanner_data/trades.jsonl", "scanner_data_v13/trades.jsonl", "scanner_data_v14/trades.jsonl"]:
    all_trades.extend(load_trades(f))

total = sum(t.get("pnl_usd", 0) for t in all_trades)
total_wallet = info_a["wallet"] + info_b["wallet"] + info_c["wallet"]
total_avail = info_a["avail"] + info_b["avail"] + info_c["avail"]
total_pos = info_a["pos"] + info_b["pos"] + info_c["pos"]
wins = len([t for t in all_trades if t.get("pnl_pct", 0) > 0])

print("  总交易: %d笔 | 胜率: %.1f%%" % (len(all_trades), wins / max(len(all_trades), 1) * 100))
print("  三账号累计PnL: $%.2f" % total)
print("  总余额: wallet=$%.2f avail=$%.2f 持仓=%d个" % (total_wallet, total_avail, total_pos))

for day_str in ["2026-05-11", "2026-05-10", "2026-05-09", "2026-05-08"]:
    d = [t for t in all_trades if day_str in str(t.get("exit_time", ""))]
    if d:
        dp = sum(t.get("pnl_usd", 0) for t in d)
        dw = len([t for t in d if t.get("pnl_pct", 0) > 0])
        print("  %s: %d笔 胜率%.0f%% PnL=$%.2f" % (day_str, len(d), dw / max(len(d), 1) * 100, dp))

for svc, label in [("crypto-scanner", "v11"), ("crypto-scanner-v13", "v13"), ("crypto-scanner-v14", "v14")]:
    try:
        r = subprocess.run(["systemctl", "is-active", svc], capture_output=True, text=True)
        print("  %s: %s" % (label, r.stdout.strip()))
    except:
        print("  %s: unknown" % label)
