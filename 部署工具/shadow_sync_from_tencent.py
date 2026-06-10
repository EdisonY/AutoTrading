"""Read-only log sync from Tencent live server to the shadow lab node."""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import tarfile
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import paramiko

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

TENCENT_HOST = os.environ.get("TENCENT_HOST", "129.226.151.144")
TENCENT_USER = os.environ.get("TENCENT_USER", "ubuntu")
TENCENT_PASS = os.environ.get("TENCENT_SSH_PASSWORD")
REMOTE_DIR = "/opt/crypto-auto-trader"
CST = timezone(timedelta(hours=8))

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR if (SCRIPT_DIR / "core").exists() else SCRIPT_DIR.parent
LOCAL_DIR = ROOT / "server_logs_tencent"

JSONL_FILES = [
    "scanner_data/events.jsonl",
    "scanner_data/trades.jsonl",
    "scanner_data_v14/events.jsonl",
    "scanner_data_v14/trades.jsonl",
    "scanner_data_v16/events.jsonl",
    "scanner_data_v16/trades.jsonl",
    "logs/decisions.jsonl",
    "logs/signals.jsonl",
    "logs/operations.jsonl",
    "logs/system.jsonl",
    "logs_v14/decisions.jsonl",
    "logs_v14/signals.jsonl",
    "logs_v14/system.jsonl",
    "logs_v16/decisions.jsonl",
    "logs_v16/signals.jsonl",
    "logs_v16/system.jsonl",
]

ALWAYS_JSONL_FILES = [
    "runtime/market_mover_watchlist_history.jsonl",
]

TEXT_FILES = [
    "logs/scanner_stdout.log",
    "logs_v14/scanner_stdout.log",
    "logs_v16/stdout.log",
]

REPORT_FILES = [
    "reports/market_snapshot_latest.json",
    "reports/alerts_latest.md",
    "reports/historical_kline_backfill_latest.md",
    "reports/historical_kline_incremental_latest.md",
    "reports/backtest_module_latest.md",
    "runtime/account_snapshot_latest.json",
    "runtime/alerts_latest.json",
    "runtime/binance_api_queue_summary_latest.json",
    "runtime/historical_kline_backfill_latest.json",
    "runtime/historical_kline_incremental_latest.json",
    "runtime/backtest_module_latest.json",
    "runtime/market_data_cache.json",
    "runtime/market_microstructure_latest.json",
    "runtime/market_mover_watchlist.json",
    "runtime/paper_exchange_latest.json",
    "runtime/testnet_data_reset_latest.json",
]

SQLITE_FILES = [
    "runtime/event_store.sqlite3",
]

KLINE_CACHE_DIR = "runtime/kline_cache"


def ssh_client() -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    configured_key = os.environ.get("TENCENT_SSH_KEY")
    default_key = Path.home() / ".ssh" / "autotrading_tencent_sync"
    key_path = Path(configured_key).expanduser() if configured_key else default_key
    key_args = {"key_filename": str(key_path)} if key_path.exists() else {}
    client.connect(
        TENCENT_HOST,
        22,
        TENCENT_USER,
        password=TENCENT_PASS or None,
        timeout=20,
        look_for_keys=True,
        allow_agent=True,
        **key_args,
    )
    return client


def recent_days(days_back: int) -> list[str]:
    today = datetime.now(CST).date()
    return [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in reversed(range(days_back))]


def grep_recent(client: paramiko.SSHClient, rel: str, days: list[str]) -> bytes:
    cmd = f"cd {REMOTE_DIR} && {remote_jsonl_filter_command(rel, days)}"
    stdin, stdout, stderr = client.exec_command(cmd, timeout=240)
    data = stdout.read()
    stderr.read()
    return data


def fetch_tail(client: paramiko.SSHClient, rel: str, lines: int = 300) -> bytes:
    cmd = f"cd {REMOTE_DIR} && if [ -f {rel} ]; then tail -n {lines} {rel}; fi"
    stdin, stdout, stderr = client.exec_command(cmd, timeout=60)
    data = stdout.read()
    stderr.read()
    return data


def run_remote(client: paramiko.SSHClient, cmd: str, timeout: int = 300) -> tuple[int, str, str]:
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    rc = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    return rc, out.strip(), err.strip()


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def remote_shadow_sync_stale_cleanup_command(max_age_minutes: int = 15) -> str:
    minutes = max(1, int(max_age_minutes))
    return (
        "find /tmp -xdev -maxdepth 1 "
        "\\( -type d -name 'autotrading_shadow_sync_*' -o -type f -name 'autotrading_shadow_sync_*.tgz' \\) "
        f"-mmin +{minutes} -exec rm -rf -- {{}} + || true"
    )


def remote_shadow_sync_exit_trap(tmp_dir: str) -> str:
    return f"trap \"rm -rf {shell_quote(tmp_dir)}\" EXIT"


def jsonl_shard_dir(rel: str) -> str:
    return Path(rel).with_suffix("").as_posix()


def remote_jsonl_filter_command(rel: str, days: list[str], output: str | None = None) -> str:
    patterns = " ".join(f"-e {shell_quote(day)}" for day in days)
    target = f" > {shell_quote(output)}" if output else ""
    fallback = []
    if output:
        fallback.append(f": > {shell_quote(output)}")
    for day in days:
        shard = f"{jsonl_shard_dir(rel)}/{day}.jsonl"
        if output:
            fallback.append(f"if [ -f {shell_quote(shard)} ]; then cat {shell_quote(shard)} >> {shell_quote(output)}; fi")
        else:
            fallback.append(f"if [ -f {shell_quote(shard)} ]; then cat {shell_quote(shard)}; fi")
    fallback_script = "; ".join(fallback)
    return (
        f"if [ -f {shell_quote(rel)} ]; then grep {patterns} {shell_quote(rel)}{target} || true; "
        f"else {fallback_script}; fi"
    )


def remote_sqlite_backup_command(
    rel: str,
    output: str,
    sqlite_days: int,
    sentinel_limit: int,
    account_limit: int,
) -> str:
    code = (
        "import pathlib, sqlite3\n"
        "from datetime import datetime, timedelta, timezone\n"
        f"src = pathlib.Path({rel!r})\n"
        f"dst = pathlib.Path({output!r})\n"
        f"sqlite_days = {int(sqlite_days)!r}\n"
        f"sentinel_limit = {int(sentinel_limit)!r}\n"
        f"account_limit = {int(account_limit)!r}\n"
        "if not src.exists():\n"
        "    raise SystemExit(0)\n"
        "dst.parent.mkdir(parents=True, exist_ok=True)\n"
        "tmp = dst.with_suffix(dst.suffix + '.tmp')\n"
        "tmp.unlink(missing_ok=True)\n"
        "cst = timezone(timedelta(hours=8))\n"
        "cutoff = (datetime.now(cst) - timedelta(days=sqlite_days)).isoformat(timespec='seconds')\n"
        "snapshot_rules = {\n"
        "    'events': (\"ts >= ? and event_type not in ('EVENT','SENTINEL_SCANNED')\", (cutoff,)),\n"
        "    'sentinel_scans': (f'id in (select id from sentinel_scans order by id desc limit {sentinel_limit})', ()),\n"
        "    'account_snapshots': (f'id in (select id from account_snapshots order by id desc limit {account_limit})', ()),\n"
        "    'baseline_runs': ('ts >= ?', (cutoff,)),\n"
        "}\n"
        "src_con = sqlite3.connect(f'file:{src}?mode=ro', uri=True, timeout=30)\n"
        "dst_con = sqlite3.connect(str(tmp), timeout=30)\n"
        "try:\n"
        "    objects = src_con.execute(\"select type, name, sql from sqlite_master where sql is not null and type in ('table','index') order by case type when 'table' then 0 else 1 end\").fetchall()\n"
        "    for obj_type, name, sql in objects:\n"
        "        if name.startswith('sqlite_'):\n"
        "            continue\n"
        "        dst_con.execute(sql)\n"
        "    tables = {row[0] for row in src_con.execute(\"select name from sqlite_master where type='table'\")}\n"
        "    for table in tables:\n"
        "        cols = [row[1] for row in src_con.execute(f'pragma table_info({table})')]\n"
        "        col_list = ','.join('\"' + col.replace('\"', '\"\"') + '\"' for col in cols)\n"
        "        if table == 'meta' or table.startswith('attention_'):\n"
        "            rows = src_con.execute(f'select {col_list} from {table}').fetchall()\n"
        "        elif table in snapshot_rules:\n"
        "            where, params = snapshot_rules[table]\n"
        "            rows = src_con.execute(f'select {col_list} from {table} where {where}', params).fetchall()\n"
        "        else:\n"
        "            rows = []\n"
        "        if not rows:\n"
        "            continue\n"
        "        placeholders = ','.join('?' for _ in cols)\n"
        "        dst_con.executemany(f'insert into {table}({col_list}) values({placeholders})', rows)\n"
        "    dst_con.commit()\n"
        "    check = dst_con.execute('pragma quick_check').fetchone()[0]\n"
        "    if check != 'ok':\n"
        "        raise RuntimeError(f'quick_check failed: {check}')\n"
        "finally:\n"
        "    dst_con.close()\n"
        "    src_con.close()\n"
        "tmp.replace(dst)\n"
    )
    return f"python3 -c {shell_quote(code)}"


def remote_queue_summary_command(output: str) -> str:
    code = (
        "import json, pathlib, sqlite3, time\n"
        "db = pathlib.Path('runtime/binance_api_queue.sqlite3')\n"
        f"out = pathlib.Path({output!r})\n"
        "out.parent.mkdir(parents=True, exist_ok=True)\n"
        "payload = {'available': False, 'active': 0, 'cooldowns': 0, 'counts': {}, 'last': []}\n"
        "if db.exists():\n"
        "    con = sqlite3.connect(str(db))\n"
        "    con.row_factory = sqlite3.Row\n"
        "    try:\n"
        "        now_ms = int(time.time() * 1000)\n"
        "        payload = {\n"
        "            'available': True,\n"
        "            'generated_at_ms': now_ms,\n"
        "            'active': con.execute(\"select count(*) from api_requests where status in ('queued','deferred','leased')\").fetchone()[0],\n"
        "            'cooldowns': con.execute('select count(*) from api_cooldowns where until_ms > ?', (now_ms,)).fetchone()[0],\n"
        "            'counts': {r['status']: int(r['n']) for r in con.execute('select status, count(*) n from api_requests group by status')},\n"
        "            'last': [dict(r) for r in con.execute(\"select rowid,label,scope,account,path,status,result_status,error from api_requests order by rowid desc limit 8\")],\n"
        "        }\n"
        "    finally:\n"
        "        con.close()\n"
        "out.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding='utf-8')\n"
    )
    return f"python3 -c {shell_quote(code)}"


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00").split(" [")[0]
    for candidate in (text, text[:26], text[:19]):
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=CST)
            return dt.astimezone(CST)
        except Exception:
            continue
    return None


def reset_allows_empty_table(table: str) -> bool:
    receipt = LOCAL_DIR / "runtime" / "testnet_data_reset_latest.json"
    if not receipt.exists():
        return False
    try:
        import json

        data = json.loads(receipt.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return False
    if not data.get("apply"):
        return False
    after = data.get("db_reset", {}).get("counts_after", {})
    return isinstance(after, dict) and int(after.get(table, -1) or 0) == 0


def validate_local_sqlite(db_path: Path, max_age_hours: float) -> None:
    if not db_path.exists():
        raise RuntimeError(f"SQLite mirror missing: {db_path}")
    with sqlite3.connect(db_path) as conn:
        check = conn.execute("pragma quick_check").fetchone()[0]
        if check != "ok":
            raise RuntimeError(f"SQLite mirror quick_check failed: {check}")
        rows: list[tuple[str, int, str | None]] = []
        for table in ("events", "sentinel_scans", "account_snapshots"):
            exists = conn.execute(
                "select 1 from sqlite_master where type='table' and name=?",
                (table,),
            ).fetchone()
            if not exists:
                raise RuntimeError(f"SQLite mirror missing table: {table}")
            count, latest = conn.execute(f"select count(*), max(ts) from {table}").fetchone()
            rows.append((table, int(count or 0), latest))
    now = datetime.now(CST)
    print(f"[SQLITE] quick_check ok: {db_path}")
    for table, count, latest in rows:
        latest_dt = parse_ts(latest)
        age_hours = (now - latest_dt).total_seconds() / 3600 if latest_dt else None
        age_text = f"{age_hours:.2f}h" if age_hours is not None else "unknown"
        print(f"[SQLITE] {table}: rows={count} latest={latest or '-'} age={age_text}")
        if count <= 0:
            if reset_allows_empty_table(table):
                print(f"[SQLITE] {table}: empty accepted by reset receipt")
                continue
            raise RuntimeError(f"SQLite mirror table empty: {table}")
        if age_hours is None or age_hours > max_age_hours:
            raise RuntimeError(
                f"SQLite mirror stale: {table} latest={latest or '-'} age={age_text}, max={max_age_hours}h"
            )


def fetch_archive_with_scp(remote_archive: str, local_archive: Path, timeout: int = 180) -> None:
    key = os.environ.get("TENCENT_SSH_KEY")
    default_key = Path.home() / ".ssh" / "autotrading_tencent_sync"
    key_path = Path(key).expanduser() if key else default_key
    cmd = [
        "scp",
        "-q",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "ConnectTimeout=20",
    ]
    if key_path.exists():
        cmd.extend(["-i", str(key_path)])
    cmd.extend([f"{TENCENT_USER}@{TENCENT_HOST}:{remote_archive}", str(local_archive)])
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"scp archive fetch failed rc={proc.returncode}: {proc.stderr[-1200:] or proc.stdout[-1200:]}")


def fetch_archive_with_cat(client: paramiko.SSHClient, remote_archive: str, local_archive: Path, timeout: int = 300) -> None:
    stdin, stdout, stderr = client.exec_command(f"cat {shell_quote(remote_archive)}", timeout=timeout)
    try:
        with local_archive.open("wb") as f:
            while True:
                chunk = stdout.channel.recv(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
        rc = stdout.channel.recv_exit_status()
        err = stderr.read().decode("utf-8", errors="replace")
        if rc != 0:
            raise RuntimeError(f"remote archive cat failed rc={rc}: {err[-1200:]}")
    except Exception:
        local_archive.unlink(missing_ok=True)
        raise


def sync_bundle(
    days_back: int,
    log_tail: int = 300,
    include_jsonl: bool = False,
    sqlite_days: int | None = None,
    sentinel_limit: int = 20000,
    account_limit: int = 1500,
    max_age_hours: float = 6.0,
    kline_limit: int = 500,
) -> None:
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    days = recent_days(days_back)
    sqlite_days = sqlite_days or days_back
    stamp = int(time.time())
    tmp_dir = f"/tmp/autotrading_shadow_sync_{stamp}"
    archive = f"{tmp_dir}.tgz"

    commands = [
        "set -e",
        remote_shadow_sync_stale_cleanup_command(),
        f"rm -rf {shell_quote(tmp_dir)} {shell_quote(archive)}",
        remote_shadow_sync_exit_trap(tmp_dir),
        f"mkdir -p {shell_quote(tmp_dir)}",
        f"cd {shell_quote(REMOTE_DIR)}",
    ]
    jsonl_files = ALWAYS_JSONL_FILES + (JSONL_FILES if include_jsonl else [])
    for rel in jsonl_files:
        parent = Path(rel).parent.as_posix()
        commands.append(f"mkdir -p {shell_quote(f'{tmp_dir}/{parent}')} ")
        commands.append(remote_jsonl_filter_command(rel, days, f"{tmp_dir}/{rel}"))
    for rel in TEXT_FILES:
        parent = Path(rel).parent.as_posix()
        commands.append(f"mkdir -p {shell_quote(f'{tmp_dir}/{parent}')} ")
        commands.append(
            f"if [ -f {shell_quote(rel)} ]; then tail -n {int(log_tail)} {shell_quote(rel)} > {shell_quote(f'{tmp_dir}/{rel}')} ; fi"
        )
    for rel in REPORT_FILES:
        parent = Path(rel).parent.as_posix()
        commands.append(f"mkdir -p {shell_quote(f'{tmp_dir}/{parent}')} ")
        if rel == "runtime/binance_api_queue_summary_latest.json":
            commands.append(remote_queue_summary_command(f"{tmp_dir}/{rel}"))
        else:
            commands.append(
                f"if [ -f {shell_quote(rel)} ]; then cp {shell_quote(rel)} {shell_quote(f'{tmp_dir}/{rel}')} ; fi"
            )
    for rel in SQLITE_FILES:
        parent = Path(rel).parent.as_posix()
        commands.append(f"mkdir -p {shell_quote(f'{tmp_dir}/{parent}')} ")
        commands.append(
            remote_sqlite_backup_command(
                rel,
                f"{tmp_dir}/{rel}",
                sqlite_days=sqlite_days,
                sentinel_limit=sentinel_limit,
                account_limit=account_limit,
            )
        )
    commands.append(f"mkdir -p {shell_quote(f'{tmp_dir}/{KLINE_CACHE_DIR}')} ")
    commands.append(
        "if [ -d runtime/kline_cache ]; then "
        "find runtime/kline_cache -maxdepth 1 -type f -name '*.json' -printf '%T@ %p\\n' 2>/dev/null "
        f"| sort -nr | head -n {int(kline_limit)} | awk '{{print $2}}' "
        f"| xargs -r -I{{}} cp {{}} {shell_quote(f'{tmp_dir}/{KLINE_CACHE_DIR}/')} ; "
        "fi"
    )
    commands.extend(
        [
            f"tar -czf {shell_quote(archive)} -C {shell_quote(tmp_dir)} .",
            f"rm -rf {shell_quote(tmp_dir)}",
            f"du -h {shell_quote(archive)} | awk '{{print $1}}'",
        ]
    )

    local_archive = LOCAL_DIR / f"_sync_{stamp}.tgz"
    client = ssh_client()
    try:
        print(f"Connected to Tencent {TENCENT_HOST}")
        print(
            "Fast bundle mode: "
            f"{', '.join(days)}; sqlite_days={sqlite_days}; "
            f"sentinel_limit={sentinel_limit}; account_limit={account_limit}; "
            f"kline_limit={kline_limit}; log_tail={log_tail}"
        )
        rc, out, err = run_remote(client, " && ".join(commands), timeout=300)
        if rc != 0:
            raise RuntimeError(f"remote bundle failed rc={rc}: {err or out}")
        if out:
            print(f"Remote bundle size: {out.splitlines()[-1]}")
        try:
            fetch_archive_with_cat(client, archive, local_archive)
        except Exception as exc:
            local_archive.unlink(missing_ok=True)
            print(f"[WARN] SSH archive stream failed: {exc}; trying scp fallback")
            client.close()
            client = None
            fetch_archive_with_scp(archive, local_archive, timeout=420)
    finally:
        try:
            cleanup_client = client or ssh_client()
            try:
                run_remote(cleanup_client, f"rm -rf {shell_quote(tmp_dir)} {shell_quote(archive)}", timeout=30)
            finally:
                cleanup_client.close()
        except Exception:
            pass

    with tarfile.open(local_archive, "r:gz") as tar:
        tar.extractall(LOCAL_DIR)
    local_archive.unlink(missing_ok=True)

    validate_local_sqlite(LOCAL_DIR / "runtime" / "event_store.sqlite3", max_age_hours=max_age_hours)

    expected_files = jsonl_files + TEXT_FILES + REPORT_FILES + SQLITE_FILES
    for rel in expected_files:
        path = LOCAL_DIR / rel
        if not path.exists():
            print(f"[SKIP] {rel}")
            continue
        print(f"[OK] {rel}")
    kline_count = len(list((LOCAL_DIR / KLINE_CACHE_DIR).glob("*.json")))
    print(f"[OK] {KLINE_CACHE_DIR}: {kline_count} files")


def sync(days_back: int) -> None:
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    days = recent_days(days_back)
    client = ssh_client()
    try:
        print(f"Connected to Tencent {TENCENT_HOST}; days={','.join(days)}")
        for rel in ALWAYS_JSONL_FILES + JSONL_FILES:
            local = LOCAL_DIR / rel
            local.parent.mkdir(parents=True, exist_ok=True)
            data = grep_recent(client, rel, days)
            local.write_bytes(data)
            rows = data.count(b"\n")
            print(f"[JSONL] {rel}: {rows} rows")
        for rel in TEXT_FILES:
            local = LOCAL_DIR / rel
            local.parent.mkdir(parents=True, exist_ok=True)
            data = fetch_tail(client, rel)
            local.write_bytes(data)
            print(f"[LOG] {rel}: {len(data)} bytes")
        for rel in REPORT_FILES:
            local = LOCAL_DIR / rel
            local.parent.mkdir(parents=True, exist_ok=True)
            cmd = f"cd {REMOTE_DIR} && if [ -f {rel} ]; then cat {rel}; fi"
            stdin, stdout, stderr = client.exec_command(cmd, timeout=60)
            data = stdout.read()
            stderr.read()
            local.write_bytes(data)
            print(f"[REPORT] {rel}: {len(data)} bytes")
    finally:
        client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync Tencent logs into shadow lab")
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--full", action="store_true", help="Use the older file-by-file sync mode")
    parser.add_argument("--include-jsonl", action="store_true", help="Include recent legacy JSONL shards in fast bundle mode")
    parser.add_argument("--log-tail", type=int, default=300, help="Lines to keep from large text logs in fast mode")
    parser.add_argument("--sqlite-days", type=int, help="Days of SQLite events/baseline rows to keep in the shadow mirror; defaults to --days")
    parser.add_argument("--sentinel-limit", type=int, default=20000, help="Newest sentinel_scans rows to keep in the SQLite mirror")
    parser.add_argument("--account-limit", type=int, default=1500, help="Newest account_snapshots rows to keep in the SQLite mirror")
    parser.add_argument("--max-age-hours", type=float, default=6.0, help="Fail if synced events/sentinel/account tables are older than this")
    parser.add_argument("--kline-limit", type=int, default=500, help="Newest runtime/kline_cache JSON files to include in the bundle")
    args = parser.parse_args(argv)
    if not args.full:
        sync_bundle(
            args.days,
            log_tail=args.log_tail,
            include_jsonl=args.include_jsonl,
            sqlite_days=args.sqlite_days,
            sentinel_limit=args.sentinel_limit,
            account_limit=args.account_limit,
            max_age_hours=args.max_age_hours,
            kline_limit=args.kline_limit,
        )
    else:
        sync(args.days)
    print(f"Local shadow mirror updated at {LOCAL_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
