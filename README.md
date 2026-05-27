# AutoTrading

AutoTrading is a live crypto strategy system with three Binance futures strategies, a market-mover sentinel, a command-center report, and a strategy-evolution gate.

This repository is designed for multi-machine development. It contains code, configs, deployment scripts, compact research memory, and project state. It intentionally does not contain live secrets or bulky runtime artifacts.

## Read First

When opening this project on a new computer, read these files in order:

1. `AGENTS.md` - operating rules for any coding model that receives this repo.
2. `PROJECT_STATE.md` - current architecture, what is done, what is not done, and GitHub/server rules.
3. `记忆文档/MEMORY.md` - chronological decision memory.
4. `research_memory/attention/open_items.json` - durable open attention items that should not be forgotten after daily reports roll over.
5. `CHANGELOG.md` - reason, result, validation, and unfinished work for each material change.
6. This `README.md` - how to work from a fresh clone.

## Repository Layout

- `策略文件/` - live strategy scanners and sentinel-related strategy code.
- `交易客户端/` - Binance account clients. API keys come from environment variables, not source code.
- `core/` - shared event store, risk, research, experiment, sentinel, and paper-broker utilities.
- `部署工具/` - report generation, deployment, sync, alerts, strategy evolution, and maintenance scripts.
- `config/` - strategy config files safe to version.
- `research_memory/` - compact research memory, approvals, hypotheses, lessons, promotions, and durable attention ledger.
- `experiments/` - experiment registry and compact family metadata. Large generated results are ignored.
- `记忆文档/` - human-readable long-term memory.
- `CHANGELOG.md` - required Git-synchronized ledger of material changes and live operational actions.

## Fresh Clone Checklist

```powershell
git clone git@github.com:EdisonY/AutoTrading.git
cd AutoTrading
git status --short --branch
```

Then:

1. Confirm you have Python available.
2. Install local dependencies:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

3. Compile the current core scripts before editing:

```powershell
python -m py_compile 部署工具\decision_attention.py 部署工具\portal_dashboard.py 部署工具\portal_refresh_service.py 部署工具\system_alerts.py 部署工具\pull_live_context.py 部署工具\release_manager.py
```

4. Read `PROJECT_STATE.md` and `research_memory/attention/open_items.json` before changing strategy behavior.
5. Do not run deployment or restart live services unless you have explicit authority for that machine and task.

The scanner dependency `cloud/analyzer/auxiliary.py` is included in Git. If a future import refers to another missing `cloud/analyzer` module, recover it from the live server before changing strategy behavior.

## Live/Shadow Servers

Current known layout:

- Tencent live node: `/opt/crypto-auto-trader`
- Aliyun shadow/review node: `/opt/crypto-shadow-lab`

Important live services include:

- `crypto-scanner.service` - A/v11
- `crypto-scanner-v16.service` - B/v16
- `crypto-scanner-v14.service` - C/v14
- `crypto-market-mover-sentinel.service`
- `crypto-account-snapshot.service`
- `crypto-portal-refresh.service`
- `crypto-system-alerts.service`
- `crypto-strategy-evolution-gate.timer`

Server access, API keys, and environment files are not stored in Git. A new machine needs SSH access and the server-side environment setup before it can operate live systems.

## Data And Artifact Policy

The following are intentionally excluded from Git:

- `runtime/` - SQLite event store, latest account snapshots, market cache, generated state.
- `logs/` - local and server log mirrors.
- `reports/` and `复盘报告/` - generated HTML/Markdown reports.
- `server_logs_tencent/` - local mirror of live server logs.
- `回测数据/` and server `backtest_data/` - bulky backtest artifacts.
- `.env`, API keys, SSH keys, certificates, SQLite DBs, and any credential file.

These files are not very sensitive in the same way API keys are, but they can include account, order, position, PnL, and strategy-behavior details. They also grow quickly and change constantly. Keep them on servers or local ignored folders, not in Git.

As of 2026-05-26 13:22 Asia/Shanghai, the data exists on servers:

| Location | Artifact | Approx Size |
| --- | --- | ---: |
| Tencent `/opt/crypto-auto-trader` | `runtime` | 929M |
| Tencent `/opt/crypto-auto-trader` | `logs` | 118M |
| Tencent `/opt/crypto-auto-trader` | `reports` | 14M |
| Tencent `/opt/crypto-auto-trader` | `scanner_data*` | 129M total |
| Tencent `/opt/crypto-auto-trader` | `backtest_data` | 166M |
| Aliyun `/opt/crypto-shadow-lab` | `server_logs_tencent` | 459M |
| Aliyun `/opt/crypto-shadow-lab` | `reports` | 692K |

## Current Live Context

Git is not the source of truth for current positions, current PnL, live signals, or service state. Before answering a live-state question on a new computer, run:

```powershell
python 部署工具\pull_live_context.py
```

This pulls a compact, ignored local mirror of the current command center, account snapshot, alerts, strategy evolution gate, and durable attention ledger. The main machine-readable output is `runtime/live_context_summary_latest.json`.

For detailed recent logs, add:

```powershell
python 部署工具\pull_live_context.py --logs-days 3 --log-tail 800
```

That writes detailed log mirrors to `server_logs_tencent/`, which is ignored by Git.

## Deployment And Rollback

Use `release_manager.py` for live file deployment. It defaults to dry-run; add `--apply` only when the target and component are correct.

```powershell
python 部署工具\release_manager.py deploy --target tencent --component portal
python 部署工具\release_manager.py deploy --target tencent --component strategy-b --apply
python 部署工具\release_manager.py list --target tencent
python 部署工具\release_manager.py rollback --target tencent --release-id <release-id>
python 部署工具\release_manager.py rollback --target tencent --release-id <release-id> --apply
```

Deployments back up overwritten remote files under `/opt/crypto-auto-trader/releases/<release-id>/backup` and write a manifest. Rollback restores those backups and restarts the same recorded services unless `--no-restart` is used.

## Command Center

The command-center entry page is generated by:

```powershell
python 部署工具\decision_attention.py
python 部署工具\portal_dashboard.py --out-dir reports
```

The local HTML entry is `reports/index.html`, ignored by Git because it is generated. The live node regenerates it through `crypto-portal-refresh.service`.

The command center should surface:

- account PnL and current exposure;
- strategy PnL and latest real open events;
- sentinel health and signal flow;
- OPEN_SKIPPED counterfactuals;
- strategy-evolution gate decisions;
- persistent attention items.

## Strategy Evolution Rule

Do not treat a short-window positive shadow result as an upgrade. A strategy change should only become prominent in the command center after the evolution gate has evidence across enough windows and enough samples.

The current principle is:

- P0/P1 from the gate: decision-maker should review first.
- P2: observation or more validation needed.
- Reject: do not promote.
- No automatic full-size live upgrade without explicit approval.

## Git Rules

Every material design/code/config/deployment/rollback/optimization/live-operation change must update `CHANGELOG.md` in the same pushed commit. The entry must include:

- why the change was made;
- what was completed;
- what remains unfinished;
- how it was verified;
- whether it changed the live system or required deployment.

Update `PROJECT_STATE.md` when architecture or current priorities change, and `记忆文档/MEMORY.md` when the decision or incident must be retained long term. Read-only status checks without a new decision or action do not require empty commits.

```powershell
git status --short --branch
git add <changed files>
python 部署工具\git_change_guard.py
git commit -m "Clear message"
git push
```

Before every push, check ignored runtime data did not accidentally enter staging:

```powershell
git ls-files | Select-String -Pattern 'runtime/|logs/|reports/|server_logs_tencent/|复盘报告/|回测数据/|__pycache__|\.sqlite|\.db|\.env|id_rsa|\.pem|\.key'
```

This command should return no tracked files.
