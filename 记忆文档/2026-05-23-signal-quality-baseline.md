# 2026-05-23 信号质量基线与下一阶段计划

- 已新增 `F:\AutoTrading\部署工具\signal_quality_review.py`，用于三策略 A/v11、B/v16、C/v14 的信号质量基线复盘。
- 脚本汇总 `signals`、`decisions`、`events`、`trades` 四层日志，输出总览、决策漏斗、分数段表现、胜亏原因聚类、典型误判样本、下一步校准顺序。
- 已通过 `python -m py_compile`，并生成 `F:\AutoTrading\reports\signal_quality_2026-05-09_to_2026-05-22.md/html` 与 `signal_quality_latest.html`。
- 当前本地镜像在该日期范围内没有读取到可用 signals/trades 样本，报告已明确提示“管道已通，但不能代表策略真实表现”。下一步必须先同步腾讯云远端最新日志，再用同一脚本重跑有效 baseline。
- 策略提升原则：暂不改各策略内部算法，先把可复盘样本、DecisionRecord 原生字段、错单分层归因补齐；再做小步阈值与权重校准。
- 优先优化方向：减少强趋势逆势单、低分放行单、硬顶尾部单、确认层误杀高质量信号；其次再考虑扩大高分信号覆盖率。
- 2026-05-23 已新增腾讯云日志同步脚本 `F:\AutoTrading\部署工具\sync_tencent_logs.py`，并用 `--days 3` 的增量模式从 `129.226.151.144` 拉取到本地 `server_logs_tencent`。当前已得到有效样本，生成 `reports/signal_quality_2026-05-20_to_2026-05-22.md/html`。
- 最近三天基线结论：A/v11 全量PnL +1094.86，但剔除恢复仓仅 +35.02；B/v16 +24.80，主要卡在 15m 无确认或弱确认；C/v14 -45.69，胜率 26.7%，硬顶率 10%，硬顶尾部和左侧入场最需要优先处理。
- 已开始收口策略代码：v14 收紧 1h 基础阈值、15m 确认阈值，并加入硬顶尾部过滤；v16 给 15m 确认增加更细的放行/拒绝门槛，并开始补 DecisionRecord 原生字段。
