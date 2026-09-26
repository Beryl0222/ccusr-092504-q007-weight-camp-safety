# 减重训练安全干预台

本项目提供减重训练安全干预台所需的领域事件交换约定、基础校验库与训练风险干预服务。各接入方使用统一的聚合标识、事件版本和发生时间表达业务事实，避免跨系统交换时丢失来源顺序。

## 目录

- `contracts/domain.schema.json`：领域事件信封和已登记类型。
- `data/sample.json`：中文联调样例。
- `src/weight_camp_safety/contracts.py`：不依赖第三方包的基础校验器。
- `src/weight_camp_safety/models.py`：角色、档案类别、风险分层与红旗症状等共享定义。
- `src/weight_camp_safety/archive.py`：只增不改的版本化档案库。
- `src/weight_camp_safety/rules.py`：已批准规则集（风险分层与动作门槛）。
- `src/weight_camp_safety/ingest.py`：离线补传与穿戴数据的幂等接入。
- `src/weight_camp_safety/notify.py`：应急外呼的可靠包装。
- `src/weight_camp_safety/service.py`：训练风险干预服务编排。
- `tests/test_contracts.py`：契约边界检查。
- `tests/test_intervention.py`：干预流程验收。

## 领域约定

- 学员同意、医学与运动评估、禁忌与转诊意见、教练资质、饮食训练处方、每日执行、体征症状、应急处置及商业承诺分别按类别留存版本，历史版本永远可读。
- 系统依据已批准规则集给出风险分层（低/中/高/禁忌）与动作门槛，结论固定附带"不替代医生诊断"声明。
- 胸闷、心悸、头晕、明显乏力等红旗症状触发即时停训与升级；只有停训之后新的专业复核可以恢复训练。
- 计划变更只作用未来安排；原始体重与不良事件关键字段一经登记不可改写，业绩人员仅可登记商业承诺，退款结算不反向改变医疗事实。
- 门店离线补传与穿戴数据重复到达时保持一次记录；同标识异内容进入复核队列，不直接落库。
- 应急呼叫失败不阻塞本地停训；服务恢复后只补送未确认的通知。
- 学员可核对停训依据、后续安排与退款进度；监管查询从事故追到入营评估、当班人员、处方版本和每次处置。

事件类型包括 ASSESSMENT_APPROVED、PLAN_ACTIVATED、PRESCRIPTION_UPDATED、SESSION_RECORDED、TRAINING_STOPPED、ESCALATION_RAISED、TRAINING_RESUMED、REFUND_SETTLED、FOLLOWUP_CLOSED；聚合类型包括 participant、risk_assessment、training_plan、safety_incident、refund_settlement。校验器负责交换层必填字段、类型、时间和版本检查，干预服务在此约定上组合业务流程并输出合规事件。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```
