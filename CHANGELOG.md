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
