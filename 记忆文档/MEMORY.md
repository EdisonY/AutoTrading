# MEMORY.md - 长期记忆

## 2026-05-30 运行续检、开仓尺寸确认与关注台账口径修复
- 用户要求继续未完成的整体运行检查。最终 live context（2026-05-30 09:13 CST）显示 Tencent 六个预期服务全部 active，system alerts `ok/0`，账户浮盈约 `+16.47 USDT`，持仓 `6`，attention `P0=0/P1=2/P2=3`。
- 已完成开仓后真实数量确认：`ExecutionEngine.open_position()` 在请求确认时用交易所持仓数量覆盖订单回报数量；B/v16、C/v14 本地持仓 size 用成交/确认数量；C/v14 开仓也启用 position confirmation。
- A/v11 增加 post-open sizing guard：如果确认后的初始保证金偏离 100 USDT 目标区间且不是 minNotional 例外，会立刻尝试关闭新仓并记录 `OPEN_SIZING_MISMATCH_CLOSED` 或 `OPEN_SIZING_MISMATCH_FAILED`。系统告警和清理脚本已保护/展示这些事件。
- Aliyun `shadow_sync_from_tencent.py` 已修复：镜像路径回到项目根 `server_logs_tencent`，同步 compact runtime/report 文件，默认不再拉 legacy JSONL，生成瘦身 SQLite 快照并 quick_check，SSH stream 失败时 scp fallback；同时修掉旧 Python 对 f-string backslash 的 SyntaxError。
- `decision_attention.py` 的 summary counts 现在只统计 `status=open` 的事项，`cleared_pending_review`、`archived`、`resolved` 不再把历史 P0 算进当前 P0。Tencent 远端重建后显示 open P0=0。
- 未做：没有批准或部署任何 B/v16 P1 策略候选；C/v14 仍有 P2 stale-open 关注项，应继续看过滤层/反事实，不要直接放宽阈值。

## 2026-05-27 双服务器架构优化 + 长期计划扩展
- 用户明确要求：策略间资金无法动态分配，放弃此项；结合服务器内存和存储容量对新增功能有限制；币安API调用不能被ban。
- 阿里云服务器（39.105.156.210）资源闲置，但无法调用币安实盘API（被墙）。决定将阿里云作为分析节点，承担所有不需要API的离线计算任务。
- 长期计划已重写为双服务器架构版本：腾讯云只保留6个API依赖的常驻服务，阿里云承担真相台账、命令中心、复盘、反事实、进化门禁、哨兵评估等全部分析任务。
- 资源约束已写入计划：新增进程RSS<50MB、存储红线15GB、API weight<600/min（50%安全线）。
- 新增Phase 0.5（架构迁移）、Phase 9（实盘过渡验证）、VPB评估实验、哨兵覆盖度审计、回滚触发条件。
- 执行顺序：0→0.5→1→2→6→3/4/5→7→8→9。
- Phase 0.5 已完成：腾讯精简为7服务+1timer，阿里云2个timer（每日全量+每2小时轻量），反向同步已上线。
- Phase 1 已完成：`strategy_truth_ledger.py` 分离主动策略PnL与恢复仓PnL。本地测试结果：A/v11净亏PF=0.01，B/v16净赚PF=5.9，C/v14无数据。
- Phase 2 已完成：`portal_dashboard.py` 新增"策略质量看板"，直接展示各策略主动PnL、恢复仓PnL、PF、胜率、盈亏比。
- Phase 6 已完成：`sentinel_quality_review.py` 评估哨兵贡献。本地测试：17,982哨兵决策，开仓率0.1%，过滤率68%。涨幅榜10,097次扫描仅8次开仓。前向收益计算待补充。
- Phase 3/4/5 已完成：新增10个影子实验配置到 `core/experiment.py`。A/v11：15m阈值115/120、浮动止损回撤0.8/1.0。B/v16：分档止损、过热封顶85。C/v14：候选压缩long65/70、过滤消融（赛道/趋势）。实验总数13个。
- Phase 7 已完成：恢复仓管理。真相台账增加恢复仓详细审查和5种退出策略shadow测试（4h/8h/24h时间退出、2%回撤、反向信号）。
- Phase 8 已完成：晋级门禁硬化。手续费调整0.15%、P0需≥50样本+调整后正PnL、P1需≥30样本、回滚触发条件（PF衰减<0.8x、硬顶率>1.5x）。
- Phase 9 已完成：实盘过渡验证清单。当前结果：A/v11 2/6、B/v16 4/6、C/v14 2/6。无策略满足全部条件，实盘过渡需用户明确审批。

## 2026-05-27 Polymarket 下线 + 内存/C-v14 复核
- 用户明确要求删除所有 Polymarket 相关文件代码并停掉相关服务。已停止/禁用/移除腾讯 `polymarket-monitor.service`，删除 `/opt/polymarket-lab`，并删除本地 `polymarket_lab` 代码与两套部署脚本。
- 总入口、系统告警、实时上下文拉取、持久关注台账都已移除 Polymarket 当前模块。历史记忆保留曾经做过的 Polymarket 研究结论，但后续 agent 不应把它当成当前活跃系统，除非用户明确重新开启。
- 下线后服务器内存复核：MemAvailable 约 1.4GiB，SwapTotal 2GiB、SwapUsed 约 210MiB，近 12 小时 kernel journal 未见新的 OOM。常驻交易服务的单进程 RSS 约 40-49MB，内存压力不来自三策略本身。
- C/v14 现状：昨日 2026-05-26 复盘显示 `入场候选 124 / 原始信号 38930 / 开仓 3 / 跳过 101`。这说明旧“信号太多”主要是原始分析噪声，但真实入场候选到开仓约 2.4%，仍偏保守；主要卡点仍是策略层 `can_trade=False`、分数阈值、15m 确认和同币种/仓位风控。此次未直接放宽实盘条件。

## 2026-05-27 A/v11 替换策略小范围观察 + P级事项数据库化
- 用户确认：继续推进 `A/v11 EXP-20260523-v11-replacement-quality`，但只按“扩展审核/小范围增强”走，不全量放开；其它 P0 全部归档。
- 已把 A/v11 满仓替换参数收窄到实验验证口径：`STRONG_SIGNAL_THRESHOLD=112`、`EVICT_SCORE_GAP=25`、盈利 `>=2%` 仓位硬保护、策略版本 `small_live_v11_replacement_quality_v1`。不增加总仓位、不取消同币种禁止叠仓、不降低入场阈值。
- 已写入人工审批记录：`research_memory/approvals/approve_small_live_A_v11_replacement_quality_2026-05-27.json`，scope 为 `shadow_plus_small_live_guarded`。
- 已修复研究审批识别：`apply_research_approval.py` 和 `experiment_runner.py` 现在支持按 `candidate_id`、`experiment_id`、`family_id` 三种键读取人工动作，固定实验也能被门禁识别。
- P级关注项不再以 JSON 为主存储。`decision_attention.py` 现在将 `attention_items` 与 `attention_acknowledgements` 写入 `runtime/event_store.sqlite3`；`research_memory/attention/open_items.json` 只是入口页/跨机同步缓存。
- 新增 `部署工具/acknowledge_attention_items.py`：可把用户确认归档写入 SQLite 和导出 JSON。服务器已归档 `入口页刷新失败`、`总入口页面偏旧`、`近期发生 OOM` 三个非 A/v11 P0。
- 线上验证：2026-05-27 01:14 服务器统一门禁跑通，`P0=0`，A/v11 变成 P2 `small_live_monitoring / keep_small_live_monitoring`；A/v11 replacement-quality 最新正式窗口增量约 `+844.0191`。所有核心服务仍 active。
- 后续观察重点：A/v11 的 `EVICT_CLOSE`、`OPEN_RETRY_AFTER_EVICT`、真实 PnL、硬顶风险、错失盈利；未满观察窗口前不要继续扩大战略替换权限。

## 2026-05-17 ~ 2026-05-24 历史里程碑摘要（已精简）

以下为精简后保留的关键结论，详细过程见各日期 CHANGELOG.md 条目。

### 策略版本演进（截至 2026-05-24）
- v13 已下线清理，v16 接管 Account B。v12 已停用。
- A/v11：半木夏 MACD+SuperTrend，满仓替换机制（STRONG_SIGNAL_THRESHOLD=112），同币种禁止叠仓，100 USDT 保证金纪律。
- B/v16：CVD/OFI 订单流策略，实盘 aggTrades 数据，硬底止损 10%，小仓阶段保护已临时关闭。
- C/v14：四维度评分（趋势/动量/量价/结构），分档 ATR 止损，BTC 趋势过滤，同赛道限制 2 仓。
- 三策略统一：4x 杠杆，同币种禁止叠仓，可用保证金保护 max(300USDT, 25%权益)。

### 架构升级（2026-05-24 ~ 2026-05-25）
- SQLite 事件库 `event_store.sqlite3` 已上线，三策略 JSONL+SQLite 双写。
- 统一行情缓存 `market_data_cache.json` 已上线（每 15 秒刷新）。
- 账户快照服务已上线（每 30 秒采集三账户）。
- 哨兵事件总线已上线，`sentinel_scanner.py` 为公共模块。
- 数据维护 timer 已上线（每小时归档/清理）。
- 入口页已从 JSONL 全量扫描切到 SQLite 聚合优先。

### 研究系统（2026-05-23）
- 研究经验库已上线：cases/hypotheses/lessons/snapshots 目录。
- 影子实验闭环：候选假设 → 影子实验 → 晋级门禁 → 人工审批。
- 研究审阅台 + 人工审批台已上线。
- 阿里云影子机已同步全套闭环脚本。

### Polymarket（2026-05-24 上线，2026-05-27 下线）
- 曾作为独立只读研究系统运行，连续监控 216 轮无套利机会。
- 2026-05-27 按用户要求下线，代码和服务已清理。

### 已修复的关键问题
- 双向持仓 key bug（2026-05-08）：(symbol, side) 复合 key
- v16 精度修复（2026-05-13）：ExchangeInfoCache 按 stepSize 取整
- A/v11 保证金尺寸修复（2026-05-25）：按目标保证金 100USDT 计算数量
- A/v11 同币种叠仓门控（2026-05-25）：禁止同币种重复开仓
- C/v14 信号口径修正（2026-05-26）：只记 1h 入场候选为 SIGNAL

### 2026-05-28 运维修复
- event_store.sqlite3 从 1.7GB 清理到 85MB（删除 SENTINEL_SCANNED/SIGNAL/EVENT 旧数据，VACUUM）
- 哨兵扫描分离到独立表 `sentinel_scans`（按 date 分区），不再写入 events 表
- 分析流水线从阿里云迁回腾讯本地运行（SQLite 85MB，全流水线 <1s 完成）
- A/v11 Testnet 账户被平台封禁（-1109），用户注册新账户，API key 已更新
- portal_dashboard.py 修复 `{{}}` f-string 语法错误
- 阿里云流水线 timeout 从默认 90s 增到 900s，sync 步骤加 timeout 600
- 关注项确认 API 服务部署在阿里云 8090 端口（attention_api_server.py）
- 哨兵 400 修复（2026-05-24）：testnet 不可用币种过滤
- Binance 下单预检硬化（2026-05-27）：OPEN_SKIPPED vs OPEN_FAILED 归因

### 2026-05-29 双服务器报表链路补齐
- 腾讯入口页服务口径已修正：`crypto-portal-refresh.service` 和已迁移的 counterfactual/evolution/market-review timer 不再作为腾讯故障或 P0 关注项出现。
- Aliyun 分析链路已补齐：`shadow_sync_from_tencent.py` 使用 SQLite backup API 生成一致 DB 快照并 quick_check；`sentinel_quality_review.py` 已确认读取 `sentinel_scans`；`counterfactual_open_skips.py` 参数已修到当前 CLI；attention ledger 在 portal 生成前重建；truth/sentinel runtime JSON 和 attention JSON 会反向同步回腾讯。
- 2026-05-29 02:12 CST 最终状态：腾讯 A/B/C、哨兵、账户快照、系统告警均 active；系统告警 0；关注项 P0=0；账户浮盈约 +91.31 USDT，持仓 6。
- 剩余关注：后续继续通过 system alerts/data maintenance 监控 SQLite 增长。2026-05-29 02:16 CST 已复核 Tencent DB 约 212MB、Aliyun mirror 约 215MB，均 quick_check ok 且 freelist_count=0，当前不需要立即 VACUUM 或破坏性清理。

### 用户偏好（持续更新）
- 中文回复，绝对路径，简洁直接
- 绿涨红跌配色（--up:#22c55e, --down:#ef4444）
- Windows/PowerShell 环境，curl 需用 curl.exe
- 不要教学/术语/健康表等解释性内容
- 所有变更必须同步 Git + CHANGELOG.md

---
## 2026-05-29 全局运行自检与账户方向口径修复
- 触发：用户要求整体检查项目、策略运行、服务器运行并自动修复问题。
- 发现并修复：Aliyun 每日 `crypto-shadow-review.timer` 原脚本只刷新 evolution/portal 的一部分，没有在 portal 前重建 decision attention，导致已验证的 P0 没有及时进入入口关注台账。现已新增并部署完整 `aliyun_shadow_review.sh`：同步腾讯 SQLite -> truth ledger -> sentinel review -> research memory -> signal quality -> experiments -> counterfactual -> evolution gate -> decision attention -> research dashboard -> portal -> 反向同步腾讯。
- 发现并修复：Binance testnet 个别持仓行 `positionSide` 与真实经济方向冲突，例如 C/v14 FIL 原始 `positionSide=LONG` 但 `positionAmt/notional/raw upnl` 显示实际是 SHORT；B/v16 NEAR/GRASS 也有类似冲突。新增 `core/position_utils.py`，统一用“原始浮盈与 entry/mark 匹配”优先推断有效方向，账户快照、日报当前持仓、共享模型、开仓后确认持仓都改用同一口径。
- 当前验证：2026-05-29 02:56 CST 拉取 live context 显示腾讯六个预期服务全部 active，系统告警 0，账户浮盈约 +28.46 USDT，持仓 8，关注项 P0=1/P1=1/P2=10。P0 是策略进化候选提醒，不是服务器故障。
- 部署记录：腾讯 account、A/v11、B/v16、C/v14、portal/system-alerts 已部署；Aliyun shadow 已部署。后续 agent 不要再按原始 `positionSide` 单独解释当前持仓方向，必须通过 `core.position_utils.infer_position_side()`。
---
## 2026-05-29 A/v11 full trailing approval + B/C close confirmation loop
- User approved both A/v11 P0 trailing-pullback candidates for full rollout: `EXP-20260527-v11-trailing-pullback-0p8` and `EXP-20260527-v11-trailing-pullback-1p0`. Because live code can only run one 15m pullback parameter at a time, the selected live value is the wider `15m=1.0 ATR`; `30m` remains `0.8 ATR`. The 0.8 candidate remains durable approved evidence and a rollback option.
- Close/forced-close rule: a strategy must not treat an exchange order response as sufficient proof of closure. It must confirm the matching symbol/side position is gone; if still present, retry with remaining quantity; if still present after retry, write `CLOSE_FAILED` or `FORCED_CLOSE_FAILED`, keep local state, and surface a bad system alert on the command center.
- B/v16 and C/v14 must not directly trust raw Binance testnet `positionSide`; exchange-side count, restored positions, duplicate-position display, hard-stop loss calculation, and close direction must use `core.position_utils.infer_position_side()` / `leveraged_loss_pct()`.
- Aliyun daily shadow-review must run `daily_market_review.py` so `market_review_latest.md/html` does not stay on an old date.
- Final 2026-05-29 noon verification: all six Tencent live services were active and command-center attention P0 was cleared after approved A/v11 trailing candidates were resolved from the attention ledger. System alert remains bad because B/v16 legacy `BCHUSDT` long, `ETHUSDT` long, and `FHEUSDT` short still reject close attempts with Binance Testnet `-4061`; this is an exposed unresolved live-risk item, not a silent local-state issue.
