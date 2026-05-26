# AutoTrading Change Ledger

This is the durable reason-and-outcome ledger for every material design, code, configuration, deployment, rollback, optimization, or live operational change.

## Recording Rule

- Update this file in the same commit as any material repository change.
- Record a live deploy, rollback, service/config change, or risk decision even when no source code changed.
- Keep generated runtime/log/report data out of Git; record only the conclusion, validation result, release id, and remaining work here.
- Update `PROJECT_STATE.md` when the current architecture, operating procedure, or open priorities change.
- Update `记忆文档/MEMORY.md` when a durable decision, important incident, or milestone should survive daily report rollover.
- Read-only inspection that yields no new decision, incident, or action does not require an empty Git commit.

## Entry Template

```markdown
## YYYY-MM-DD HH:MM CST - Short title
- Trigger / reason:
- Completed:
- Not completed / remaining:
- Verification:
- Live impact / deployment:
- Files / release / commit:
```

## 2026-05-26 18:00 CST - Enforce complete Git synchronization for future changes
- Trigger / reason: The decision maker requires every future operation, design update, optimization, and change to be synchronized to Git with clear reasons, completed work, and remaining work so a different computer or model can continue without missing context.
- Completed: Added this durable change ledger; extended model and handoff rules; added a staged-change guard that rejects material code/config/tool commits without a staged `CHANGELOG.md` update.
- Not completed / remaining: GitHub CI does not yet enforce the guard remotely; until CI is added, agents must run the local check before every commit and push.
- Verification: `python -m py_compile 部署工具\git_change_guard.py` passed; when the tool file was staged without this ledger it correctly blocked the commit, and after adding this ledger `python 部署工具\git_change_guard.py` passed.
- Live impact / deployment: Documentation and local Git workflow only; no live strategy file, server configuration, or service is changed.
- Files / release / commit: `CHANGELOG.md`, `AGENTS.md`, `README.md`, `PROJECT_STATE.md`, `记忆文档/MEMORY.md`, `部署工具/git_change_guard.py`; no live release; Git commit contains this record.

## 2026-05-26 21:05 CST - Correct C/v14 signal-to-entry observability
- Trigger / reason: C/v14 showed extremely high signal counts but very few opens, making the command center imply a broken conversion funnel. Code inspection showed the metric was counting raw 15m confirmation signals and low-score raw analysis candidates as `SIGNAL`, while the live open gate only allows 1h entry candidates above the true threshold.
- Completed: C/v14 now writes `SIGNAL` only for real 1h entry candidates that pass the live score gate; raw candidate count remains in system heartbeat as `raw_signals_found`. Portal strategy summary now labels the column as entry candidates and, for C/v14, recalculates historical candidate counts from existing SQLite rows using the actual 1h long/short thresholds while showing raw candidate count separately. `config/v14.toml` was corrected to match the currently effective hardcoded thresholds. `release_manager.py` now accepts explicit `--dry-run` to match handoff documentation.
- Not completed / remaining: This does not loosen C/v14 live entry rules or prove a better C/v14 alpha. Next step is to collect post-fix candidate/open ratios and then decide whether C/v14 scoring itself should be redesigned or retired.
- Verification: Local `py_compile` passed for `策略文件/scanner_v14.py`, `部署工具/portal_dashboard.py`, and `部署工具/release_manager.py`. Local portal generation completed. `release_manager.py` dry-run passed for Tencent `strategy-c` and `portal`.
- Live impact / deployment: Intended live impact is observability/reporting only for C/v14 and portal; no trade threshold is loosened. Live deployment was attempted for release `20260526-210539-strategy-c-4c37e4d` but did not upload files because both Paramiko and native SSH timed out during SSH protocol banner exchange.
- Files / release / commit: `策略文件/scanner_v14.py`, `部署工具/portal_dashboard.py`, `config/v14.toml`, `部署工具/release_manager.py`; code commit `4c37e4d` pushed; live deployment remains pending until SSH access is healthy.

## 2026-05-26 21:14 CST - Retry Tencent deployment blocked before SSH login
- Trigger / reason: User requested updating the C/v14 observability fix to the server.
- Completed: Retried Tencent `strategy-c` release `20260526-211114-strategy-c-051f7d5` with a 30 second SSH/banner timeout and separately probed local TCP connectivity to port 22.
- Not completed / remaining: No server files were uploaded and no live services were restarted. Need restore healthy SSH banner/login first, then deploy `strategy-c` and `portal`.
- Verification: Paramiko deployment failed before authentication with `Error reading SSH protocol banner`. Native SSH also timed out during banner exchange. A short TCP probe returned `tcp_22_connected`, so port 22 is reachable but sshd did not return the SSH banner in time.
- Live impact / deployment: No live impact; deployment did not reach upload stage.
- Files / release / commit: This ledger entry records the failed operational attempt; code remains at `051f7d5`.
