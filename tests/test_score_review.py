"""评分复核核心服务测试：季度公示异议全流程与关键不变量。"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from score_review import (  # noqa: E402
    ConflictError,
    RecusalError,
    ScoreReviewService,
    StateError,
    ValidationError,
)


def make_service() -> ScoreReviewService:
    clock = lambda: datetime(2026, 10, 4, 9, 0, tzinfo=timezone.utc)  # noqa: E731
    return ScoreReviewService(clock=clock)


def seed_world(svc: ScoreReviewService) -> dict:
    """两条规则、三家机构、一个已发布批次。"""
    svc.create_rule(code="DATA-ERROR", name="数据差错", category="报送质量",
                    deduction=10, effective_from="2026-01-01", by="监管员-甲")
    svc.create_rule(code="LATE-FILING", name="迟报材料", category="报送时效",
                    deduction=5, effective_from="2026-01-01", by="监管员-甲")
    ev1 = svc.register_evidence(org_id="ORG-001", rule_code="DATA-ERROR", period="2026-Q3",
                                description="三季度报送数据差错", by="监管员-乙")
    ev2 = svc.register_evidence(org_id="ORG-002", rule_code="LATE-FILING", period="2026-Q3",
                                description="材料迟报两天", by="监管员-乙")
    ev3 = svc.register_evidence(org_id="ORG-003", rule_code="DATA-ERROR", period="2026-Q3",
                                description="三季度报送数据差错", by="监管员-乙")
    for ev in (ev1, ev2, ev3):
        svc.verify_evidence(ev["id"], approved=True, by="监管员-丙")
    batch = svc.create_batch(period="2026-Q3", as_of="2026-09-30", by="监管员-丙")
    svc.publish_batch(batch["id"], by="监管员-丙")
    return {"ev1": ev1, "ev2": ev2, "ev3": ev3, "batch": batch}


class QuarterlyScenarioTest(unittest.TestCase):
    """题述场景：整改完成仍被旧扣分影响，经申诉复核而非直接重算解决。"""

    def test_full_scenario(self) -> None:
        svc = make_service()
        world = seed_world(svc)
        batch_id = world["batch"]["id"]

        # 发布后：ORG-001 被扣 10 分，排名垫底；输入摘要已冻结
        results = svc.batch_results(batch_id)
        self.assertEqual(results["results"]["ORG-001"]["score"], 90)
        self.assertEqual(results["results"]["ORG-002"]["score"], 95)
        self.assertEqual(results["ranking"][0]["org_id"], "ORG-002")
        frozen_digest = svc.get_batch(batch_id)["data"]["frozen_input_digest"]
        self.assertTrue(frozen_digest)

        # 整改验证通过，但已发布结果保持不变（不直接重算）
        rect = svc.submit_rectification(evidence_id=world["ev1"]["id"], org_id="ORG-001",
                                        description="已完成数据差错整改", by="合规员-001")
        svc.verify_rectification(rect["id"], passed=True, note="复核通过", by="监管员-丙")
        self.assertEqual(svc.batch_results(batch_id)["results"]["ORG-001"]["score"], 90)

        # 申诉登记 → 补证（新版本）→ 指派（回避拦截）→ 部分撤销
        item_id = results["results"]["ORG-001"]["entries"][0]["item_id"]
        appeal = svc.file_appeal(batch_id=batch_id, org_id="ORG-001",
                                 deduction_item_ids=[item_id],
                                 reason="整改已完成仍被旧扣分影响", by="合规员-001")
        appeal_id = appeal["id"]
        self.assertEqual(appeal["data"]["status"], "登记")

        supplemented = svc.supplement_appeal(
            appeal_id, by="合规员-001",
            new_evidence={"rule_code": "DATA-ERROR", "description": "整改完成证明"},
            note="补充整改完成证明",
        )
        self.assertEqual(supplemented["version"], appeal["version"] + 1)
        self.assertEqual(supplemented["data"]["status"], "处置中")
        self.assertEqual(len(supplemented["data"]["evidence_ids"]), 1)

        svc.register_recusal(reviewer_id="专家-甲", org_id="ORG-001",
                             reason="曾任该机构顾问", by="监管员-丙")
        with self.assertRaises(RecusalError):
            svc.assign_reviewer(appeal_id, reviewer_id="专家-甲", by="监管员-丙")

        svc.assign_reviewer(appeal_id, reviewer_id="专家-乙", by="监管员-丙")
        with self.assertRaises(ConflictError):
            svc.decide_appeal(appeal_id, revoked_item_ids=[item_id],
                              comment="越权决定", by="专家-丙")

        decided = svc.decide_appeal(appeal_id, revoked_item_ids=[item_id],
                                    comment="整改属实，撤销该扣分项", by="专家-乙")
        self.assertEqual(decided["data"]["status"], "已决定")
        self.assertEqual(decided["data"]["review_result"]["score"], 100)
        self.assertEqual(decided["data"]["decision"]["scope"], "全部撤销")

        # 对比：原结果 90 / 复核结果 100，原公示排名不变
        comparison = svc.appeal_comparison(appeal_id)
        self.assertEqual(comparison["original"]["score"], 90)
        self.assertEqual(comparison["original"]["rank"], 2)
        self.assertEqual(comparison["review"]["score"], 100)
        self.assertEqual(comparison["delta"], 10)
        self.assertEqual(svc.batch_results(batch_id)["results"]["ORG-001"]["score"], 90)

        # 规则勘误：DATA-ERROR 调整为 8 分，2026-10-01 起生效
        svc.errata_rule("DATA-ERROR", deduction=8, effective_from="2026-10-01",
                        reason="扣分标准勘误", by="监管员-甲")

        # 历史口径复算：9-30 口径仍按 10 分；10-15 口径按 8 分
        old = svc.recalculate_as_of(period="2026-Q3", as_of="2026-09-30",
                                    org_id="ORG-003", by="监管员-丙")
        self.assertEqual(old["data"]["results"]["ORG-003"]["score"], 90)
        new = svc.recalculate_as_of(period="2026-Q3", as_of="2026-10-15",
                                    org_id="ORG-003", by="监管员-丙")
        self.assertEqual(new["data"]["results"]["ORG-003"]["score"], 92)

        # 勘误与撤销使当前输入与冻结摘要漂移
        self.assertTrue(svc.digest_check(batch_id)["drift"])

        # 批次重开（新版本）→ 重新发布：排名整体改变，历史 run 保留
        reopened = svc.reopen_batch(batch_id, reason="复核决定生效", by="监管员-丙")
        self.assertEqual(reopened["data"]["status"], "重开中")
        republished = svc.publish_batch(batch_id, by="监管员-丙")
        self.assertEqual(republished["version"], reopened["version"] + 1)
        final = svc.batch_results(batch_id)
        self.assertEqual(final["results"]["ORG-001"]["score"], 100)
        self.assertEqual(final["ranking"][0]["org_id"], "ORG-001")
        self.assertEqual(len(republished["data"]["published_run_ids"]), 2)
        self.assertNotEqual(final["input_digest"], frozen_digest)

        # 归档
        archived = svc.archive_appeal(appeal_id, by="监管员-丙")
        self.assertEqual(archived["data"]["status"], "已归档")


class VersioningTest(unittest.TestCase):
    """申诉补证、规则勘误、部分撤销、批次重开都必须产生新版本。"""

    def test_mutations_append_versions(self) -> None:
        svc = make_service()
        world = seed_world(svc)
        batch_id = world["batch"]["id"]
        results = svc.batch_results(batch_id)
        item_id = results["results"]["ORG-001"]["entries"][0]["item_id"]

        rule_before = svc.rule_versions("DATA-ERROR")["version"]
        svc.errata_rule("DATA-ERROR", deduction=8, effective_from="2026-10-01",
                        reason="勘误", by="监管员-甲")
        rule_entity = svc.rule_versions("DATA-ERROR")
        self.assertEqual(rule_entity["version"], rule_before + 2)  # 关闭旧区间 + 新版本
        reasons = [v["reason"] for v in rule_entity["versions"]]
        self.assertTrue(any("勘误" in r for r in reasons))

        appeal = svc.file_appeal(batch_id=batch_id, org_id="ORG-001",
                                 deduction_item_ids=[item_id], reason="异议", by="合规员-001")
        supplemented = svc.supplement_appeal(appeal["id"], by="合规员-001",
                                             evidence_ids=[world["ev1"]["id"]])
        self.assertEqual(supplemented["version"], 2)

        svc.assign_reviewer(appeal["id"], reviewer_id="专家-乙", by="监管员-丙")
        decided = svc.decide_appeal(appeal["id"], revoked_item_ids=[item_id],
                                    comment="撤销", by="专家-乙")
        self.assertEqual(decided["version"], 4)
        item = svc.repo.items.get(item_id)
        self.assertEqual(item["data"]["status"], "已撤销")
        self.assertEqual(item["version"], 2)

        reopened = svc.reopen_batch(batch_id, reason="生效", by="监管员-丙")
        self.assertEqual(reopened["version"], svc.get_batch(batch_id)["version"])

    def test_partial_revocation_keeps_other_items(self) -> None:
        svc = make_service()
        svc.create_rule(code="R1", name="规则一", category="c", deduction=10,
                        effective_from="2026-01-01", by="监管员-甲")
        svc.create_rule(code="R2", name="规则二", category="c", deduction=5,
                        effective_from="2026-01-01", by="监管员-甲")
        ev1 = svc.register_evidence(org_id="ORG-001", rule_code="R1", period="2026-Q3",
                                    description="d1", by="监管员-乙")
        ev2 = svc.register_evidence(org_id="ORG-001", rule_code="R2", period="2026-Q3",
                                    description="d2", by="监管员-乙")
        svc.verify_evidence(ev1["id"], approved=True, by="监管员-丙")
        svc.verify_evidence(ev2["id"], approved=True, by="监管员-丙")
        batch = svc.create_batch(period="2026-Q3", as_of="2026-09-30", by="监管员-丙")
        svc.publish_batch(batch["id"], by="监管员-丙")
        entries = svc.batch_results(batch["id"])["results"]["ORG-001"]["entries"]
        item_ids = [e["item_id"] for e in entries]
        appeal = svc.file_appeal(batch_id=batch["id"], org_id="ORG-001",
                                 deduction_item_ids=item_ids, reason="异议", by="合规员-001")
        svc.assign_reviewer(appeal["id"], reviewer_id="专家-乙", by="监管员-丙")
        decided = svc.decide_appeal(appeal["id"], revoked_item_ids=[item_ids[0]],
                                    comment="只撤销第一项", by="专家-乙")
        self.assertEqual(decided["data"]["decision"]["scope"], "部分撤销")
        review = decided["data"]["review_result"]
        self.assertEqual(review["score"], 95)  # 100 - 剩余 5 分
        comparison = svc.appeal_comparison(appeal["id"])
        self.assertEqual(comparison["original"]["score"], 85)
        self.assertEqual(comparison["delta"], 10)


class RecusalTest(unittest.TestCase):
    def test_recusal_blocks_assignment(self) -> None:
        svc = make_service()
        world = seed_world(svc)
        batch_id = world["batch"]["id"]
        item_id = svc.batch_results(batch_id)["results"]["ORG-001"]["entries"][0]["item_id"]
        appeal = svc.file_appeal(batch_id=batch_id, org_id="ORG-001",
                                 deduction_item_ids=[item_id], reason="异议", by="合规员-001")

        # 显式登记的回避关系
        svc.register_recusal(reviewer_id="专家-甲", org_id="ORG-001",
                             reason="利益相关", by="监管员-丙")
        with self.assertRaises(RecusalError):
            svc.assign_reviewer(appeal["id"], reviewer_id="专家-甲", by="监管员-丙")
        # 申诉登记人本人
        with self.assertRaises(RecusalError):
            svc.assign_reviewer(appeal["id"], reviewer_id="合规员-001", by="监管员-丙")
        # 证据/整改经办人
        with self.assertRaises(RecusalError):
            svc.assign_reviewer(appeal["id"], reviewer_id="监管员-丙", by="监管员-丙")
        # 无回避情形的专家可以指派
        assigned = svc.assign_reviewer(appeal["id"], reviewer_id="专家-乙", by="监管员-丙")
        self.assertEqual(assigned["data"]["reviewer_id"], "专家-乙")


class GuardTest(unittest.TestCase):
    def test_state_and_validation_guards(self) -> None:
        svc = make_service()
        svc.create_rule(code="R1", name="规则一", category="c", deduction=10,
                        effective_from="2026-01-01", by="监管员-甲")
        with self.assertRaises(ConflictError):
            svc.create_rule(code="R1", name="重复", category="c", deduction=1,
                            effective_from="2026-01-01", by="监管员-甲")
        with self.assertRaises(ValidationError):
            svc.errata_rule("R1", effective_from="2025-12-31", reason="回溯改写",
                            by="监管员-甲")

        ev = svc.register_evidence(org_id="ORG-001", rule_code="R1", period="2026-Q3",
                                   description="d", by="监管员-乙")
        svc.verify_evidence(ev["id"], approved=True, by="监管员-丙")
        with self.assertRaises(StateError):
            svc.verify_evidence(ev["id"], approved=True, by="监管员-丙")

        batch = svc.create_batch(period="2026-Q3", as_of="2026-09-30", by="监管员-丙")
        with self.assertRaises(StateError):
            svc.reopen_batch(batch["id"], reason="草稿不能重开", by="监管员-丙")
        with self.assertRaises(StateError):
            svc.file_appeal(batch_id=batch["id"], org_id="ORG-001",
                            deduction_item_ids=["DI-000001"], reason="未发布", by="合规员-001")
        svc.publish_batch(batch["id"], by="监管员-丙")
        with self.assertRaises(StateError):
            svc.publish_batch(batch["id"], by="监管员-丙")  # 已发布不能重复发布
        svc.reopen_batch(batch["id"], reason="复核生效", by="监管员-丙")
        svc.publish_batch(batch["id"], by="监管员-丙")

        item_id = svc.batch_results(batch["id"])["results"]["ORG-001"]["entries"][0]["item_id"]
        appeal = svc.file_appeal(batch_id=batch["id"], org_id="ORG-001",
                                 deduction_item_ids=[item_id], reason="异议", by="合规员-001")
        with self.assertRaises(StateError):
            svc.decide_appeal(appeal["id"], revoked_item_ids=[], comment="未指派",
                              by="专家-乙")
        svc.assign_reviewer(appeal["id"], reviewer_id="专家-乙", by="监管员-丙")
        with self.assertRaises(ValidationError):
            svc.decide_appeal(appeal["id"], revoked_item_ids=["DI-999999"],
                              comment="超范围", by="专家-乙")

    def test_trial_run_is_separated_from_formal(self) -> None:
        svc = make_service()
        world = seed_world(svc)
        batch_id = world["batch"]["id"]
        before = svc.batch_results(batch_id)

        trial = svc.trial_run(period="2026-Q3", as_of="2026-09-30", by="监管员-丙")
        self.assertEqual(trial["data"]["kind"], "试算")
        self.assertIsNone(trial["data"]["batch_id"])
        self.assertEqual(trial["data"]["results"]["ORG-001"]["score"], 90)

        # 试算不改变已发布结果，也不进入批次
        after = svc.batch_results(batch_id)
        self.assertEqual(before["run_id"], after["run_id"])
        self.assertEqual(len(svc.get_batch(batch_id)["data"]["published_run_ids"]), 1)

        # 历史复算记录当时使用的规则版本
        svc.errata_rule("DATA-ERROR", deduction=8, effective_from="2026-10-01",
                        reason="勘误", by="监管员-甲")
        recalc = svc.recalculate_as_of(period="2026-Q3", as_of="2026-09-30", by="监管员-丙")
        used = {r["code"]: r for r in recalc["data"]["rules_used"]}
        self.assertEqual(used["DATA-ERROR"]["deduction"], 10)


if __name__ == "__main__":
    unittest.main()
