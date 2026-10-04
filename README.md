# 机构合规评分复核

本项目维护机构合规评分复核的领域约定、角色边界与样例数据，并提供完整的 Python 后端服务，供接口和自动化验证统一使用。当前契约覆盖机构合规员、执业人员、监管人员、复核专家，并明确评分规则时态、发布输入冻结、复核回避关系、新旧结果对比等关键约束。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/score_review/`：评分复核后端服务（规则、证据、整改、批次、申诉、复核）。
- `tools/check_contract.py`：命令行摘要检查。
- `tools/run_server.py`：启动 HTTP 服务（默认 127.0.0.1:8000）。
- `tools/demo_scenario.py`：季度公示异议全流程演示。
- `tests/`：契约完整性回归测试、服务测试、API 测试。

## 后端设计

- **版本化存储**：所有实体只追加新版本。申诉补证、规则勘误、部分撤销、批次重开均产生新版本，历史快照完整保留。
- **评分规则时态**：规则按生效区间 `[effective_from, effective_to)` 管理；勘误只能关闭当前区间并开启新区间，历史口径不可改写，任何 `as_of` 复算都复现当时的规则版本。
- **试算与正式分离**：试算（`/api/trial-runs`）与历史复算（`/api/recalculate`）产生独立 run，永不进入批次；只有批次发布才产生正式结果。
- **发布输入冻结**：发布时对规则版本、证据版本、整改版本、扣分项版本计算 sha256 摘要并冻结；`digest-check` 可校验事后输入漂移。
- **复核不改榜**：整改验证通过、申诉部分撤销只影响复核结果；公示排名只有在批次重开并重新发布后才会整体改变。
- **复核回避**：显式回避登记 + 自动拦截（申诉登记人本人、相关证据/整改经办人）。

## 主要 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/rules` | 登记评分规则 |
| POST | `/api/rules/{code}/errata` | 规则勘误（产生新版本） |
| GET | `/api/rules?as_of=` | 按口径日期查询生效规则版本 |
| POST | `/api/evidence` / `/api/evidence/{id}/verify` | 证据登记与核验（通过生成扣分项） |
| POST | `/api/rectifications` / `/api/rectifications/{id}/verify` | 整改提交与验证 |
| POST | `/api/batches` / `/api/batches/{id}/publish` | 批次创建与发布（冻结输入摘要） |
| POST | `/api/batches/{id}/reopen` | 批次重开（新版本，之后可重新发布） |
| GET | `/api/batches/{id}/results[/{org}]` | 已发布结果与排名 |
| GET | `/api/batches/{id}/digest-check` | 冻结摘要漂移校验 |
| POST | `/api/trial-runs` | 试算（与正式结果分离） |
| POST | `/api/recalculate` | 按历史口径复算 |
| POST | `/api/appeals` | 申诉登记 |
| POST | `/api/appeals/{id}/supplement` | 申诉补证（新版本） |
| POST | `/api/appeals/{id}/assign` | 指派复核人（回避校验） |
| POST | `/api/appeals/{id}/decide` | 复核决定（支持部分撤销） |
| GET | `/api/appeals/{id}/comparison` | 原结果 vs 复核结果对比 |
| POST | `/api/recusals` | 登记回避关系 |

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`

场景演示：`python3 tools/demo_scenario.py`

启动服务：`python3 tools/run_server.py`
