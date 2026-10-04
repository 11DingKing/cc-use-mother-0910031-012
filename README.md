# 机构合规评分复核

机构季度合规评分的评分规则时态管理、证据与整改、试算/发布分离、批次版本化与冻结、申诉复核、回避校验、新旧结果对比与历史口径复算后端。纯 Python 3.11 标准库实现（SQLite + http.server），零第三方依赖。

## 业务约束如何落地

- **评分规则与生效区间**：每条规则（`code`）有不可变版本序列，版本带 `[start_date, end_date)` 生效区间，新版本发布自动截断旧开放区间，区间重叠被拒绝。证据按**发生时点**适用当时生效的规则版本。
- **规则勘误**：勘误是新版本。非回溯勘误由生效区间天然隔离，只作用于生效后的证据；回溯勘误（`retroactive=true`）是旁路版本，仅当批次版本快照显式纳入 `retroactive_codes` 授权时才改变历史口径。
- **证据项与整改验证**：整改提交→复核人核验（带回避校验）→`effective_date` 起旧扣分项标记为 `remediated`，不再扣分，但不改变已冻结的历史结果。
- **试算与正式结果分离**：`trial` 只写试算表并返回预览，不产生批次版本；正式结果只能通过「建草稿版本→发布」产生。
- **发布冻结输入摘要**：发布瞬间构建完整输入快照（机构、全部规则版本、期内证据、整改、撤销集、勘误授权、截止日），计算规范化 sha256 摘要与结果一并入卷；此后原始数据再变化也不影响该版本。
- **必须产生新版本的四类动作**：申诉补证（`appeal_supplement`）、规则勘误（`rule_errata`）、部分撤销（`partial_revocation`）、批次重开（`reopen`）。撤销集与勘误授权沿版本累积继承。
- **复核回避**：登记复核人—机构回避关系；整改核验、申诉补证/决定、部分撤销按目标机构拦截，首次发布/勘误/重开等批次级动作对全部机构逐一检查。
- **新旧结果对比**：任意两个版本可按机构对比分数差与排名变化（含「分数不变但排名因他人提分而变动」）。
- **按历史口径复算**：对已发布版本，仅用其冻结快照重算，并逐位比对（规范化 JSON）确认与发布结果一致。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/compliance_review/`：评分复核后端。
  - `canonical.py`：规范化 JSON 与 sha256 摘要。
  - `scoring.py`：时态规则解析、整改/撤销/勘误口径、排名、快照复算（纯函数）。
  - `store.py`：SQLite 表结构与存取。
  - `service.py`：业务规则、版本生命周期、冻结、回避、对比、复算。
  - `api.py`：HTTP API 与服务启动。
- `tools/check_contract.py`：契约命令行检查。
- `tools/demo_scenario.py`：完整业务场景演示。
- `tests/`：契约回归、业务全流程、HTTP 端到端测试。

## 运行

```bash
# 启动服务（默认 127.0.0.1:8080，可用 --host/--port/--db 覆盖）
PYTHONPATH=src python3 -m compliance_review.api --port 8080 --db data/compliance.db

# 场景演示（无需起服务）
python3 tools/demo_scenario.py
```

## 主要 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/institutions` `/reviewers` | 登记机构、复核人 |
| POST | `/reviewers/{rid}/recusals` | 登记回避关系 |
| POST/GET | `/rules`，POST `/rules/{code}/errata` | 规则版本与勘误 |
| POST/GET | `/evidence`，POST `/evidence/{eid}/remediations` | 证据与整改提交 |
| POST | `/remediations/{rid}/verify` | 整改核验（回避校验） |
| POST/GET | `/batches` | 发布批次 |
| POST | `/batches/{bid}/trial` | 试算（不落版本） |
| POST | `/batches/{bid}/versions` | 建草稿版本（kind 区分五类） |
| POST | `/batches/{bid}/versions/{v}/publish` | 发布并冻结快照（可传 `as_of`） |
| POST | `/batches/{bid}/reopen`、`/partial-revoke` | 重开、部分撤销（产生新版本） |
| GET | `/batches/{bid}/compare?left=1&right=2` | 新旧结果对比 |
| POST | `/batches/{bid}/versions/{v}/recompute` | 历史口径复算并校验 |
| POST/GET | `/appeals`，POST `/appeals/{aid}/supplement`、`/decision` | 申诉、补证（新版本）、决定 |

版本 `kind`：`initial`、`appeal_supplement`、`rule_errata`、`partial_revocation`、`reopen`。
批次状态：`open`（有草稿/未发布）、`published`（最新版本已发布，可申诉/重开）、`reopened`（重开草稿未发布）。

## 验证

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tools tests
python3 tools/check_contract.py domain/contract.json
```
