"""机构合规评分复核的核心服务。

覆盖：评分规则（生效区间与勘误）、证据项、整改验证、发布批次、
试算与正式计算分离、发布输入摘要冻结、申诉与复核（含回避限制）、
部分撤销、批次重开，以及原结果与复核结果对比、按历史口径复算。

关键不变量：
- 历史口径不可改写：规则勘误只能关闭当前区间并开启新区间，
  任何 as_of 落在过去的复算都复现当时的规则版本。
- 发布即冻结：批次发布时把规则版本、证据版本、整改版本、扣分项版本
  一并算出 sha256 摘要冻结，事后可校验输入是否漂移。
- 试算不进批次：试算/历史复算只产生独立 run，永不改变已发布结果。
- 复核差异不直接改榜：部分撤销只影响复核结果；要改动公示排名，
  必须显式重开批次并重新发布。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from .digest import digest
from .errors import ConflictError, RecusalError, StateError, ValidationError
from .repository import InMemoryRepository

BASE_SCORE = 100

# 申诉状态机，与 domain/contract.json 的 states 对齐
APPEAL_STATES = ("登记", "待核验", "处置中", "已决定", "已归档")

BATCH_DRAFT = "草稿"
BATCH_PUBLISHED = "已发布"
BATCH_REOPENED = "重开中"

ITEM_ACTIVE = "有效"
ITEM_REVOKED = "已撤销"

RUN_TRIAL = "试算"
RUN_FORMAL = "正式"
RUN_HISTORICAL = "历史复算"


def _check_date(value: str, field: str) -> None:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError):
        raise ValidationError(f"{field} 必须是 YYYY-MM-DD 格式：{value!r}") from None


class ScoreReviewService:
    """评分复核领域服务门面。"""

    def __init__(self, repo: InMemoryRepository | None = None, clock: Callable[[], datetime] | None = None) -> None:
        self.repo = repo or InMemoryRepository()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> str:
        return self._clock().isoformat()

    def _today(self) -> str:
        return self._clock().date().isoformat()

    # ------------------------------------------------------------------
    # 评分规则：登记、勘误、按口径日期选取版本
    # ------------------------------------------------------------------

    def create_rule(
        self,
        *,
        code: str,
        name: str,
        category: str,
        deduction: int,
        effective_from: str,
        effective_to: str | None = None,
        by: str,
    ) -> dict:
        """登记评分规则（版本 1）。同一 code 即同一规则的版本链。"""
        with self.repo.lock:
            if self.repo.rules.get_or_none(code) is not None:
                raise ConflictError(f"规则已存在：{code}")
            _check_date(effective_from, "effective_from")
            if effective_to is not None:
                _check_date(effective_to, "effective_to")
                if effective_to <= effective_from:
                    raise ValidationError("生效结束日必须晚于生效开始日")
            if not isinstance(deduction, int) or deduction < 0:
                raise ValidationError("扣分必须是非负整数")
            data = {
                "code": code,
                "name": name,
                "category": category,
                "deduction": deduction,
                "effective_from": effective_from,
                "effective_to": effective_to,
            }
            return self.repo.rules.create(data, entity_id=code, at=self._now(), by=by, reason="规则登记")

    def errata_rule(
        self,
        code: str,
        *,
        effective_from: str,
        reason: str,
        by: str,
        deduction: int | None = None,
        name: str | None = None,
        category: str | None = None,
        effective_to: str | None = None,
    ) -> dict:
        """规则勘误：关闭当前版本区间并产生新版本，历史口径保持不变。"""
        with self.repo.lock:
            entity = self.repo.rules.get(code)
            current = entity["data"]
            _check_date(effective_from, "effective_from")
            if effective_to is not None:
                _check_date(effective_to, "effective_to")
                if effective_to <= effective_from:
                    raise ValidationError("生效结束日必须晚于生效开始日")
            if effective_from < current["effective_from"]:
                raise ValidationError("勘误生效日早于当前版本生效日，历史口径不可改写")
            if current["effective_to"] is not None and effective_from > current["effective_to"]:
                raise ValidationError("勘误生效日超出当前版本生效区间")
            if deduction is not None and (not isinstance(deduction, int) or deduction < 0):
                raise ValidationError("扣分必须是非负整数")
            now = self._now()
            # 1) 关闭当前版本区间（旧版本保留在历史中，旧口径仍可复算）
            self.repo.rules.update(
                code, {"effective_to": effective_from}, at=now, by=by, reason=f"规则勘误（关闭旧区间）：{reason}"
            )
            # 2) 勘误后的新版本自生效日起生效
            new_data = {
                "deduction": deduction if deduction is not None else current["deduction"],
                "name": name if name is not None else current["name"],
                "category": category if category is not None else current["category"],
                "effective_from": effective_from,
                "effective_to": effective_to,
            }
            return self.repo.rules.update(code, new_data, at=now, by=by, reason=f"规则勘误（新版本）：{reason}")

    def rule_as_of(self, code: str, as_of: str) -> dict | None:
        """取 as_of 当日生效的规则版本；无则返回 None。"""
        _check_date(as_of, "as_of")
        entity = self.repo.rules.get(code)
        chosen = None
        for version in entity["versions"]:
            data = version["data"]
            if data["effective_from"] <= as_of:
                chosen = version  # 生效日单调不减，取最后一个不晚于 as_of 的版本
            else:
                break
        if chosen is None:
            return None
        data = chosen["data"]
        if data["effective_to"] is not None and as_of >= data["effective_to"]:
            return None
        return {**data, "rule_version": chosen["version"]}

    def rules_as_of(self, as_of: str) -> list[dict]:
        """as_of 当日生效的全部规则版本（历史口径复算的基础）。"""
        result = []
        for entity in self.repo.rules.all():
            rule = self.rule_as_of(entity["id"], as_of)
            if rule is not None:
                result.append(rule)
        return sorted(result, key=lambda r: r["code"])

    def list_rules(self) -> list[dict]:
        return sorted(
            ({"version": e["version"], **e["data"]} for e in self.repo.rules.all()),
            key=lambda r: r["code"],
        )

    def rule_versions(self, code: str) -> dict:
        return self.repo.rules.get(code)

    # ------------------------------------------------------------------
    # 证据项与整改验证
    # ------------------------------------------------------------------

    def register_evidence(
        self,
        *,
        org_id: str,
        rule_code: str,
        period: str,
        description: str,
        by: str,
        appeal_id: str | None = None,
    ) -> dict:
        """登记证据项；申诉补证时携带 appeal_id。"""
        with self.repo.lock:
            self.repo.rules.get(rule_code)
            data = {
                "org_id": org_id,
                "rule_code": rule_code,
                "period": period,
                "description": description,
                "status": "待核验",
                "appeal_id": appeal_id,
            }
            reason = "申诉补证登记" if appeal_id else "证据登记"
            return self.repo.evidence.create(data, at=self._now(), by=by, reason=reason)

    def verify_evidence(self, evidence_id: str, *, approved: bool, by: str) -> dict:
        """核验证据；通过后生成扣分项，进入评分引擎。"""
        with self.repo.lock:
            entity = self.repo.evidence.get(evidence_id)
            if entity["data"]["status"] != "待核验":
                raise StateError(f"证据当前状态为 {entity['data']['status']}，不能重复核验")
            status = "已核验" if approved else "已驳回"
            updated = self.repo.evidence.update(
                evidence_id, {"status": status}, at=self._now(), by=by, reason="证据核验"
            )
            if approved:
                self._create_deduction_item(updated, by=by)
            return updated

    def _create_deduction_item(self, evidence_entity: dict, *, by: str) -> dict:
        data = evidence_entity["data"]
        item = {
            "org_id": data["org_id"],
            "period": data["period"],
            "rule_code": data["rule_code"],
            "evidence_id": evidence_entity["id"],
            "status": ITEM_ACTIVE,
            "revoked_by": None,
            "revoke_reason": None,
            "appeal_id": None,
        }
        return self.repo.items.create(item, at=self._now(), by=by, reason="证据核验通过，生成扣分项")

    def list_evidence(self, *, org_id: str | None = None, period: str | None = None) -> list[dict]:
        def hit(entity: dict) -> bool:
            data = entity["data"]
            return (org_id is None or data["org_id"] == org_id) and (period is None or data["period"] == period)

        return self.repo.evidence.find(hit)

    def get_evidence(self, evidence_id: str) -> dict:
        return self.repo.evidence.get(evidence_id)

    def submit_rectification(self, *, evidence_id: str, org_id: str, description: str, by: str) -> dict:
        """机构针对某证据对应的扣分提交整改。"""
        with self.repo.lock:
            evidence = self.repo.evidence.get(evidence_id)["data"]
            if evidence["org_id"] != org_id:
                raise ValidationError("整改机构与证据机构不一致")
            pending = self.repo.rectifications.find(
                lambda r: r["data"]["evidence_id"] == evidence_id and r["data"]["status"] == "待验证"
            )
            if pending:
                raise ConflictError("该证据已有待验证的整改记录")
            data = {
                "evidence_id": evidence_id,
                "org_id": org_id,
                "description": description,
                "status": "待验证",
                "note": "",
            }
            return self.repo.rectifications.create(data, at=self._now(), by=by, reason="整改登记")

    def verify_rectification(self, rectification_id: str, *, passed: bool, note: str = "", by: str) -> dict:
        """整改验证。通过不等于自动撤销扣分：只使其具备申诉撤销资格。"""
        with self.repo.lock:
            entity = self.repo.rectifications.get(rectification_id)
            if entity["data"]["status"] != "待验证":
                raise StateError(f"整改当前状态为 {entity['data']['status']}，不能重复验证")
            status = "验证通过" if passed else "验证失败"
            return self.repo.rectifications.update(
                rectification_id, {"status": status, "note": note}, at=self._now(), by=by, reason="整改验证"
            )

    # ------------------------------------------------------------------
    # 评分引擎：试算、正式、历史复算共用一套求值逻辑
    # ------------------------------------------------------------------

    def _evaluate(self, *, period: str, as_of: str, org_id: str | None = None) -> tuple[dict, list]:
        rules = {r["code"]: r for r in self.rules_as_of(as_of)}
        items = [
            i
            for i in self.repo.items.all()
            if i["data"]["period"] == period
            and i["data"]["status"] == ITEM_ACTIVE
            and (org_id is None or i["data"]["org_id"] == org_id)
        ]
        evidence = [
            e
            for e in self.repo.evidence.all()
            if e["data"]["period"] == period and (org_id is None or e["data"]["org_id"] == org_id)
        ]
        orgs = sorted({i["data"]["org_id"] for i in items} | {e["data"]["org_id"] for e in evidence})
        results: dict[str, dict] = {}
        for org in orgs:
            entries = []
            for item in items:
                if item["data"]["org_id"] != org:
                    continue
                rule = rules.get(item["data"]["rule_code"])
                entries.append(
                    {
                        "item_id": item["id"],
                        "rule_code": item["data"]["rule_code"],
                        "rule_version": rule["rule_version"] if rule else None,
                        "points": rule["deduction"] if rule else 0,
                        "evidence_id": item["data"]["evidence_id"],
                    }
                )
            score = max(0, BASE_SCORE - sum(e["points"] for e in entries))
            results[org] = {"score": score, "entries": entries}
        ranking = [
            {"org_id": org, "score": res["score"], "rank": rank}
            for rank, (org, res) in enumerate(
                sorted(results.items(), key=lambda kv: (-kv[1]["score"], kv[0])), start=1
            )
        ]
        return results, ranking

    def _frozen_input(self, period: str, as_of: str) -> dict:
        """汇总一次计算的全部输入，用于发布时冻结摘要。"""
        evidence_of_period = {
            e["id"]: e for e in self.repo.evidence.all() if e["data"]["period"] == period
        }
        rectifications = [
            r for r in self.repo.rectifications.all() if r["data"]["evidence_id"] in evidence_of_period
        ]
        items = [i for i in self.repo.items.all() if i["data"]["period"] == period]
        return {
            "period": period,
            "as_of": as_of,
            "rules": [
                {
                    "code": r["code"],
                    "version": r["rule_version"],
                    "deduction": r["deduction"],
                    "effective_from": r["effective_from"],
                    "effective_to": r["effective_to"],
                }
                for r in self.rules_as_of(as_of)
            ],
            "evidence": [
                {"id": e["id"], "version": e["version"], "org_id": e["data"]["org_id"],
                 "rule_code": e["data"]["rule_code"], "status": e["data"]["status"]}
                for e in sorted(evidence_of_period.values(), key=lambda x: x["id"])
            ],
            "rectifications": [
                {"id": r["id"], "version": r["version"], "evidence_id": r["data"]["evidence_id"],
                 "status": r["data"]["status"]}
                for r in sorted(rectifications, key=lambda x: x["id"])
            ],
            "items": [
                {"id": i["id"], "version": i["version"], "org_id": i["data"]["org_id"],
                 "rule_code": i["data"]["rule_code"], "status": i["data"]["status"]}
                for i in sorted(items, key=lambda x: x["id"])
            ],
        }

    def _run(
        self,
        *,
        kind: str,
        period: str,
        as_of: str,
        org_id: str | None,
        by: str,
        batch_id: str | None = None,
        input_digest: str | None = None,
    ) -> dict:
        results, ranking = self._evaluate(period=period, as_of=as_of, org_id=org_id)
        data = {
            "kind": kind,
            "batch_id": batch_id,
            "period": period,
            "as_of": as_of,
            "org_id": org_id,
            "results": results,
            "ranking": ranking,
            "input_digest": input_digest or digest(self._frozen_input(period, as_of)),
            "rules_used": [
                {"code": r["code"], "version": r["rule_version"], "deduction": r["deduction"]}
                for r in self.rules_as_of(as_of)
            ],
        }
        return self.repo.runs.create(data, at=self._now(), by=by, reason=f"{kind}计算")

    def trial_run(self, *, period: str, as_of: str | None = None, org_id: str | None = None, by: str) -> dict:
        """试算：结果独立存放，不进入任何批次，不影响已发布结果。"""
        with self.repo.lock:
            return self._run(kind=RUN_TRIAL, period=period, as_of=as_of or self._today(), org_id=org_id, by=by)

    def recalculate_as_of(self, *, period: str, as_of: str, org_id: str | None = None, by: str) -> dict:
        """按历史口径复算：严格使用 as_of 当日生效的规则版本。"""
        with self.repo.lock:
            _check_date(as_of, "as_of")
            return self._run(kind=RUN_HISTORICAL, period=period, as_of=as_of, org_id=org_id, by=by)

    def get_run(self, run_id: str) -> dict:
        return self.repo.runs.get(run_id)

    # ------------------------------------------------------------------
    # 发布批次：发布冻结输入摘要，重开产生新版本
    # ------------------------------------------------------------------

    def create_batch(self, *, period: str, as_of: str, by: str) -> dict:
        with self.repo.lock:
            _check_date(as_of, "as_of")
            data = {
                "period": period,
                "as_of": as_of,
                "status": BATCH_DRAFT,
                "frozen_input_digest": None,
                "frozen_input": None,
                "published_run_ids": [],
                "current_run_id": None,
            }
            return self.repo.batches.create(data, at=self._now(), by=by, reason="批次创建")

    def publish_batch(self, batch_id: str, *, by: str) -> dict:
        """正式发布：执行正式计算并冻结输入摘要。"""
        with self.repo.lock:
            batch = self.repo.batches.get(batch_id)
            data = batch["data"]
            if data["status"] not in (BATCH_DRAFT, BATCH_REOPENED):
                raise StateError(f"批次当前状态为 {data['status']}，不能发布")
            frozen = self._frozen_input(data["period"], data["as_of"])
            frozen_digest = digest(frozen)
            run = self._run(
                kind=RUN_FORMAL,
                period=data["period"],
                as_of=data["as_of"],
                org_id=None,
                by=by,
                batch_id=batch_id,
                input_digest=frozen_digest,
            )
            return self.repo.batches.update(
                batch_id,
                {
                    "status": BATCH_PUBLISHED,
                    "frozen_input_digest": frozen_digest,
                    "frozen_input": frozen,
                    "published_run_ids": data["published_run_ids"] + [run["id"]],
                    "current_run_id": run["id"],
                },
                at=self._now(),
                by=by,
                reason="批次发布，冻结输入摘要",
            )

    def reopen_batch(self, batch_id: str, *, reason: str, by: str) -> dict:
        """批次重开：产生新的批次版本，之后可重新发布（排名才会整体变化）。"""
        with self.repo.lock:
            batch = self.repo.batches.get(batch_id)
            if batch["data"]["status"] != BATCH_PUBLISHED:
                raise StateError("仅已发布的批次可以重开")
            return self.repo.batches.update(
                batch_id, {"status": BATCH_REOPENED}, at=self._now(), by=by, reason=f"批次重开：{reason}"
            )

    def get_batch(self, batch_id: str) -> dict:
        return self.repo.batches.get(batch_id)

    def batch_results(self, batch_id: str) -> dict:
        batch = self.repo.batches.get(batch_id)
        run_id = batch["data"]["current_run_id"]
        if run_id is None:
            raise StateError("批次尚未发布，无正式结果")
        run = self.repo.runs.get(run_id)["data"]
        return {
            "batch_id": batch_id,
            "run_id": run_id,
            "period": run["period"],
            "as_of": run["as_of"],
            "input_digest": run["input_digest"],
            "results": run["results"],
            "ranking": run["ranking"],
        }

    def batch_org_result(self, batch_id: str, org_id: str) -> dict:
        view = self.batch_results(batch_id)
        result = view["results"].get(org_id)
        if result is None:
            from .errors import NotFoundError

            raise NotFoundError(f"发布结果中无机构：{org_id}")
        rank = next((r["rank"] for r in view["ranking"] if r["org_id"] == org_id), None)
        return {
            "batch_id": batch_id,
            "run_id": view["run_id"],
            "org_id": org_id,
            "score": result["score"],
            "rank": rank,
            "entries": result["entries"],
        }

    def digest_check(self, batch_id: str) -> dict:
        """用当前数据重算输入摘要，与发布时冻结的摘要比对，检查输入漂移。"""
        batch = self.repo.batches.get(batch_id)
        data = batch["data"]
        if data["frozen_input_digest"] is None:
            raise StateError("批次尚未发布，无冻结摘要")
        current_digest = digest(self._frozen_input(data["period"], data["as_of"]))
        return {
            "batch_id": batch_id,
            "frozen_input_digest": data["frozen_input_digest"],
            "current_input_digest": current_digest,
            "drift": current_digest != data["frozen_input_digest"],
        }

    # ------------------------------------------------------------------
    # 复核回避
    # ------------------------------------------------------------------

    def register_recusal(self, *, reviewer_id: str, org_id: str, reason: str, by: str) -> dict:
        """登记复核人与机构之间的回避关系。"""
        with self.repo.lock:
            record = {
                "reviewer_id": reviewer_id,
                "org_id": org_id,
                "reason": reason,
                "at": self._now(),
                "by": by,
            }
            self.repo.recusals.append(record)
            return dict(record)

    def list_recusals(self, *, reviewer_id: str | None = None) -> list[dict]:
        return [r for r in self.repo.recusals if reviewer_id is None or r["reviewer_id"] == reviewer_id]

    def _check_recusal(self, reviewer_id: str, appeal: dict) -> None:
        """回避校验：显式登记关系 + 申诉登记人本人 + 相关证据/整改经办人。"""
        data = appeal["data"]
        reasons = []
        for record in self.repo.recusals:
            if record["reviewer_id"] == reviewer_id and record["org_id"] == data["org_id"]:
                reasons.append(f"存在登记的回避关系：{record['reason']}")
        if appeal["versions"][0]["by"] == reviewer_id:
            reasons.append("复核人是申诉登记人本人")
        participants: set[str] = set()
        for item_id in data["deduction_item_ids"]:
            item = self.repo.items.get(item_id)["data"]
            evidence = self.repo.evidence.get(item["evidence_id"])
            participants |= {v["by"] for v in evidence["versions"]}
            related = self.repo.rectifications.find(
                lambda r: r["data"]["evidence_id"] == item["evidence_id"]
            )
            for rectification in related:
                participants |= {v["by"] for v in rectification["versions"]}
        if reviewer_id in participants:
            reasons.append("复核人曾经办相关证据或整改验证")
        if reasons:
            raise RecusalError(
                "复核人存在回避情形",
                details={"reviewer_id": reviewer_id, "org_id": data["org_id"], "reasons": reasons},
            )

    # ------------------------------------------------------------------
    # 申诉与复核：登记 → 待核验 → 处置中 → 已决定 → 已归档
    # ------------------------------------------------------------------

    def file_appeal(self, *, batch_id: str, org_id: str, deduction_item_ids: list[str], reason: str, by: str) -> dict:
        """针对已发布批次中的扣分项提出申诉。"""
        with self.repo.lock:
            batch = self.repo.batches.get(batch_id)["data"]
            if batch["status"] != BATCH_PUBLISHED:
                raise StateError("只能对已发布批次提出申诉")
            if not deduction_item_ids:
                raise ValidationError("申诉须指定至少一个扣分项")
            for item_id in deduction_item_ids:
                item = self.repo.items.get(item_id)["data"]
                if item["org_id"] != org_id or item["period"] != batch["period"]:
                    raise ValidationError(f"扣分项 {item_id} 不属于该机构或该批次周期")
            data = {
                "batch_id": batch_id,
                "org_id": org_id,
                "deduction_item_ids": list(deduction_item_ids),
                "reason": reason,
                "status": "登记",
                "reviewer_id": None,
                "evidence_ids": [],
                "source_run_id": batch["current_run_id"],
                "review_result": None,
                "decision": None,
            }
            return self.repo.appeals.create(data, at=self._now(), by=by, reason="申诉登记")

    def supplement_appeal(
        self,
        appeal_id: str,
        *,
        by: str,
        evidence_ids: list[str] | None = None,
        new_evidence: dict | None = None,
        note: str = "",
    ) -> dict:
        """申诉补证：挂接已有证据或登记新证据，产生新的申诉版本。"""
        with self.repo.lock:
            appeal = self.repo.appeals.get(appeal_id)
            data = appeal["data"]
            if data["status"] not in ("登记", "待核验", "处置中"):
                raise StateError(f"申诉当前状态为 {data['status']}，不能补证")
            attached = list(data["evidence_ids"])
            for evidence_id in evidence_ids or []:
                self.repo.evidence.get(evidence_id)
                if evidence_id not in attached:
                    attached.append(evidence_id)
            if new_evidence:
                batch = self.repo.batches.get(data["batch_id"])["data"]
                created = self.register_evidence(
                    org_id=data["org_id"],
                    rule_code=new_evidence["rule_code"],
                    period=batch["period"],
                    description=new_evidence["description"],
                    by=by,
                    appeal_id=appeal_id,
                )
                attached.append(created["id"])
            if not evidence_ids and not new_evidence:
                raise ValidationError("补证须至少提供一项证据")
            return self.repo.appeals.update(
                appeal_id,
                {"evidence_ids": attached, "status": "处置中"},
                at=self._now(),
                by=by,
                reason=f"申诉补证：{note}" if note else "申诉补证",
            )

    def assign_reviewer(self, appeal_id: str, *, reviewer_id: str, by: str) -> dict:
        """指派复核人，执行回避校验。"""
        with self.repo.lock:
            appeal = self.repo.appeals.get(appeal_id)
            if appeal["data"]["status"] not in ("登记", "待核验", "处置中"):
                raise StateError(f"申诉当前状态为 {appeal['data']['status']}，不能指派复核人")
            self._check_recusal(reviewer_id, appeal)
            status = "待核验" if appeal["data"]["status"] == "登记" else appeal["data"]["status"]
            return self.repo.appeals.update(
                appeal_id,
                {"reviewer_id": reviewer_id, "status": status},
                at=self._now(),
                by=by,
                reason=f"复核指派：{reviewer_id}",
            )

    def decide_appeal(self, appeal_id: str, *, revoked_item_ids: list[str], comment: str, by: str) -> dict:
        """复核决定：支持部分撤销，产生新版本并计算复核结果（不改原公示）。"""
        with self.repo.lock:
            appeal = self.repo.appeals.get(appeal_id)
            data = appeal["data"]
            if data["status"] not in ("待核验", "处置中"):
                raise StateError(f"申诉当前状态为 {data['status']}，不能作出决定")
            if not data["reviewer_id"]:
                raise StateError("尚未指派复核人")
            if by != data["reviewer_id"]:
                raise ConflictError("仅被指派的复核人可作出决定")
            self._check_recusal(by, appeal)
            unknown = set(revoked_item_ids) - set(data["deduction_item_ids"])
            if unknown:
                raise ValidationError(
                    "只能撤销申诉范围内的扣分项", details={"unknown": sorted(unknown)}
                )
            run = self.repo.runs.get(data["source_run_id"])["data"]
            original = run["results"].get(data["org_id"])
            if original is None:
                raise StateError("原发布结果中无该机构")
            entries = original["entries"]
            entry_ids = {e["item_id"] for e in entries}
            missing = set(revoked_item_ids) - entry_ids
            if missing:
                raise ValidationError("扣分项不在原发布结果中", details={"missing": sorted(missing)})
            for item_id in revoked_item_ids:
                if self.repo.items.get(item_id)["data"]["status"] != ITEM_ACTIVE:
                    raise ConflictError(f"扣分项 {item_id} 已被撤销")
            now = self._now()
            scope = "部分撤销" if set(revoked_item_ids) < set(data["deduction_item_ids"]) else "全部撤销"
            if not revoked_item_ids:
                scope = "维持原结果"
            for item_id in revoked_item_ids:
                self.repo.items.update(
                    item_id,
                    {
                        "status": ITEM_REVOKED,
                        "revoked_by": by,
                        "revoke_reason": f"申诉 {appeal_id} 复核决定",
                        "appeal_id": appeal_id,
                    },
                    at=now,
                    by=by,
                    reason=f"{scope}：申诉 {appeal_id}",
                )
            revoked = set(revoked_item_ids)
            remaining = [e for e in entries if e["item_id"] not in revoked]
            revoked_entries = [e for e in entries if e["item_id"] in revoked]
            review_score = max(0, BASE_SCORE - sum(e["points"] for e in remaining))
            review_result = {
                "score": review_score,
                "revoked_item_ids": list(revoked_item_ids),
                "revoked_entries": revoked_entries,
                "remaining_entries": remaining,
                "decided_by": by,
                "decided_at": now,
            }
            decision = {
                "scope": scope,
                "revoked_item_ids": list(revoked_item_ids),
                "comment": comment,
                "decided_by": by,
                "decided_at": now,
            }
            return self.repo.appeals.update(
                appeal_id,
                {"status": "已决定", "review_result": review_result, "decision": decision},
                at=now,
                by=by,
                reason=f"复核决定（{scope}）",
            )

    def archive_appeal(self, appeal_id: str, *, by: str) -> dict:
        with self.repo.lock:
            appeal = self.repo.appeals.get(appeal_id)
            if appeal["data"]["status"] != "已决定":
                raise StateError("仅已决定的申诉可以归档")
            return self.repo.appeals.update(
                appeal_id, {"status": "已归档"}, at=self._now(), by=by, reason="申诉归档"
            )

    def get_appeal(self, appeal_id: str) -> dict:
        return self.repo.appeals.get(appeal_id)

    def appeal_comparison(self, appeal_id: str) -> dict:
        """对比原公示结果与复核结果；原结果取自申诉登记时冻结的源 run。"""
        appeal = self.repo.appeals.get(appeal_id)
        data = appeal["data"]
        run = self.repo.runs.get(data["source_run_id"])["data"]
        original = run["results"].get(data["org_id"])
        if original is None:
            raise StateError("原发布结果中无该机构")
        rank = next((r["rank"] for r in run["ranking"] if r["org_id"] == data["org_id"]), None)
        review = data["review_result"]
        return {
            "appeal_id": appeal_id,
            "batch_id": data["batch_id"],
            "run_id": data["source_run_id"],
            "org_id": data["org_id"],
            "original": {"score": original["score"], "rank": rank, "entries": original["entries"]},
            "review": review,
            "delta": review["score"] - original["score"] if review else None,
            "note": "原公示结果保持不变；复核差异须经批次重开并重新发布后生效",
        }
