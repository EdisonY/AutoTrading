# MEMORY.md - 长期记忆

## 2026-05-27 双服务器架构优化 + 长期计划扩展
- 用户明确要求：策略间资金无法动态分配，放弃此项；结合服务器内存和存储容量对新增功能有限制；币安API调用不能被ban。
- 阿里云服务器（39.105.156.210）资源闲置，但无法调用币安实盘API（被墙）。决定将阿里云作为分析节点，承担所有不需要API的离线计算任务。
- 长期计划已重写为双服务器架构版本：腾讯云只保留6个API依赖的常驻服务，阿里云承担真相台账、命令中心、复盘、反事实、进化门禁、哨兵评估等全部分析任务。
- 资源约束已写入计划：新增进程RSS<50MB、存储红线15GB、API weight<600/min（50%安全线）。
- 新增Phase 0.5（架构迁移）、Phase 9（实盘过渡验证）、VPB评估实验、哨兵覆盖度审计、回滚触发条件。
- 执行顺序：0→0.5→1→2→6→3/4/5→7→8→9。
- Phase 0.5 已完成：腾讯精简为7服务+1timer，阿里云2个timer（每日全量+每2小时轻量），反向同步已上线。
- Phase 1 已完成：`strategy_truth_ledger.py` 分离主动策略PnL与恢复仓PnL。本地测试结果：A/v11净亏PF=0.01，B/v16净赚PF=5.9，C/v14无数据。

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
- 哨兵 400 修复（2026-05-24）：testnet 不可用币种过滤
- Binance 下单预检硬化（2026-05-27）：OPEN_SKIPPED vs OPEN_FAILED 归因

### 用户偏好（持续更新）
- 中文回复，绝对路径，简洁直接
- 绿涨红跌配色（--up:#22c55e, --down:#ef4444）
- Windows/PowerShell 环境，curl 需用 curl.exe
- 不要教学/术语/健康表等解释性内容
- 所有变更必须同步 Git + CHANGELOG.md

---

