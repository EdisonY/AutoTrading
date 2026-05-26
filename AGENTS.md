# AutoTrading Agent Instructions

This repository operates a live trading system. Before analyzing current state or changing strategy behavior, read:

1. `README.md`
2. `PROJECT_STATE.md`
3. `记忆文档/MEMORY.md` near the latest entries
4. `research_memory/attention/open_items.json`
5. `CHANGELOG.md` near the latest entries

## Current-State Rule

Git contains code and durable memory, not authoritative live runtime data. For any request about current positions, current PnL, current signals, current alerts, or whether a service is running:

```powershell
python 部署工具\pull_live_context.py
```

Then use the pulled `reports/index.html` and `runtime/live_context_summary_latest.json`. Use SSH directly only when the summary is insufficient or a fix must be deployed.

## Safety And Scope

- Never commit secrets, `.env`, SSH/API keys, SQLite databases, runtime files, logs, generated reports, server mirrors, or bulky backtest output.
- Never infer current live state from tracked memory snapshots alone.
- Treat strategy changes as live-risk changes: implement, compile-check, deploy with a release receipt, then verify service status and visible reports.
- Do not automatically approve or roll out strategy upgrades based on one short-window result. Respect the strategy evolution gate and explicit user approval rules.

## Mandatory Git Change Ledger

- Every material design, strategy, code, configuration, deployment, rollback, optimization, or live operational change must be recorded in `CHANGELOG.md` and pushed to Git in the same work session.
- Every entry must state the trigger/reason, what completed, what is not completed, verification performed, and live/deployment impact.
- Update `PROJECT_STATE.md` when current architecture, operating procedure, or unresolved priorities change. Update `记忆文档/MEMORY.md` for durable decisions, incidents, or milestones.
- A live deploy, rollback, service/config change, or consequential risk decision requires a Git ledger entry even if no code file changed.
- A read-only inspection with no new conclusion or action need not create an empty commit.
- Before committing staged changes, run `python 部署工具\git_change_guard.py`; do not push a material change if it fails.

## Deployment And Rollback

Use the unified release tool for new deployments:

```powershell
python 部署工具\release_manager.py deploy --target tencent --component portal --dry-run
python 部署工具\release_manager.py deploy --target tencent --component strategy-b --apply
python 部署工具\release_manager.py list --target tencent
python 部署工具\release_manager.py rollback --target tencent --release-id <release-id> --apply
```

Run `--dry-run` first unless the user explicitly needs an immediate known fix. Releases back up overwritten remote files before uploading; rollbacks restore those backups and recheck affected services.

## Avoiding Stalls

- Bound SSH/network/subprocess work with timeouts.
- Prefer short status-producing operations over waiting on lengthy research refreshes.
- Start long server jobs through existing services/timers and poll their status instead of holding an interactive command open.
- If blocked, state the blocker promptly, take the safe next action, and keep durable state/docs accurate.
