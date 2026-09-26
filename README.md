# 减重训练安全干预台

连锁减重训练营的训练风险干预服务：从入营同意、医学与运动评估、禁忌转诊、教练
资质、饮食训练处方、每日执行、体征症状到应急处置与商业结算，全部以**追加型事件**
留存版本。系统依据**已批准规则**给出风险分层与动作门槛，但不替代医生诊断。

## 安全语义（硬约束）

- **红旗信号即时停训与升级**：胸闷、心悸、头晕、明显乏力（及晕厥）或体征越界，
  触发 `TRAINING_STOPPED`；只有关闭时间晚于停训时间的**新专业复核**
  （`PROFESSIONAL_REVIEW_CLOSED`，medical_reviewer/medical_director）才能恢复。
- **本地优先**：停训与现场处置先落本地事件，应急呼叫失败只记
  `NOTIFICATION_FAILED`，不阻塞处置；重连后外发箱只补送仍未确认的通知。
- **计划变更只作用未来**：`PLAN_REVISED` / `PRESCRIPTION_SUPERSEDED` 必须带
  晚于当前时间的 `effective_at`，历史安排与已完成执行不回填。
- **医疗事实追加只读**：评估、禁忌、体征、事故、每日执行属于不可变事实；
  销售/业绩角色不得写入，原始体重与不良事件不可修改。
- **商业与医疗隔离**：`COMMITMENT_RECORDED`（如"不瘦退款"）与
  `REFUND_SETTLED` 独立留存，退款载荷不得携带对医疗事实的改写。
  员工隐瞒停训记录以 `SUSPENSION_RECORD_FILED` 追加纠正。
- **补传幂等**：`(source_record_id, content_hash)` 为幂等键，重复到达只保留一次
  事实（`INGESTION_DEDUPED`）；同标识异内容不改写原记录，开
  `DATA_REVIEW_OPENED` 人工复核。
- **可核对、可追溯**：学员视图给出停训依据（规则 + 原始记录）、后续安排与退款
  进度；监管视图从事故一路追到入营评估、当班教练资质、处方版本与每次处置。

## 目录

- `contracts/domain.schema.json`：领域事件信封、17 个聚合与 34 类登记事件。
- `src/weight_camp_safety/contracts.py`：交换层校验（必填、类型、时区时间、
  版本、登记枚举、幂等键成对、`basis_refs` 引用去重）。
- `src/weight_camp_safety/rules.py`：已批准规则库 `2026.09-approved`——
  风险分层（LOW/MODERATE/HIGH 与强度上限）、红旗停训、动作门槛
  （同意/转诊闭环/禁忌/资质有效期/处方角色/复核恢复/未来变更/写保护/退款隔离）。
- `src/weight_camp_safety/eventlog.py`：追加日志（聚合版本严格 +1）、
  离线补传幂等网关与同标识异内容复核。
- `src/weight_camp_safety/emergency.py`：本地即时停训、应急处置留痕与
  通知外发箱（失败不阻塞、重连只补未确认、送达需回执）。
- `src/weight_camp_safety/trace.py`：监管事故追溯链路、依据闭环检查、
  学员核对视图。
- `src/weight_camp_safety/scenario.py`：完整事故时间线构造器（联调夹具）。
- `data/sample.json`：单事件信封联调样例。
- `data/sample_trace.json`：33 个事件的端到端事故全链路样例（由脚本生成）。
- `scripts/dump_sample_trace.py`：重新生成全链路样例。
- `tests/`：契约、规则、幂等补传、应急外发箱、追溯与视图共 48 条测试。

## 端到端时间线

`scenario.build_scenario()` 构造：入营问卷注明心血管疾病 → HIGH 分层与禁忌转诊 →
过期教练开课被拒、有效教练按低强度医学处方带训 → 穿戴数据重复幂等丢弃、
同标识异内容进复核 → 胸闷心悸头晕本地即时停训与应急处置 → 120 通知断网失败、
重连只补未确认通知 → 无新复核恢复被拒、医学主管新复核后恢复 →
处方/计划仅未来生效 → 销售改写体重与事故被拒、退款结算不改医疗事实。

## 测试

```bash
python3 -m unittest discover -s tests
python3 -m compileall -q src tests scripts
python3 scripts/dump_sample_trace.py   # 重新生成 data/sample_trace.json
```
