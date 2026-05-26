# 研究经验快照 - 2026-05-22

- 案例数: 113
- 候选假设: 3

## 高频问题
- A/v11 / loss_trade / opened: 42
- B/v16 / missed_big_move / pre_filter_no_record: 14
- A/v11 / missed_big_move / pre_filter_no_record: 12
- B/v16 / loss_trade / opened: 10
- C/v14 / loss_trade / opened: 10
- C/v14 / missed_big_move / signal_candidate: 7
- C/v14 / missed_big_move / pre_filter_no_record: 6
- A/v11 / reverse_trade / opened: 5
- C/v14 / missed_big_move / confirmation: 3
- B/v16 / reverse_trade / opened: 2
- C/v14 / reverse_trade / opened: 1
- B/v16 / missed_big_move / confirmation: 1

## 候选假设
- HYP-2026-05-22-A-v11-reverse_trade-stage-guard: 反向/硬顶样本集中，入场阶段识别不足。 -> 对强趋势左侧逆势、中段回调误判样本增加阶段保护，仅进入影子过滤实验。
- HYP-2026-05-22-B-v16-reverse_trade-stage-guard: 反向/硬顶样本集中，入场阶段识别不足。 -> 对强趋势左侧逆势、中段回调误判样本增加阶段保护，仅进入影子过滤实验。
- HYP-2026-05-22-C-v14-confirmation-soft-pass: 大行情样本多次被确认层过滤。 -> 高分且日内/大盘同向时，进入15m弱确认软放行影子实验，而不是直接实盘放开。