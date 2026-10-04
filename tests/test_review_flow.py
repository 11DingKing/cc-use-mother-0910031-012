"""合规评分复核全链路测试：整改、试算/发布分离、冻结、申诉补证、
勘误、部分撤销、重开、回避、新旧对比、历史口径复算。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from compliance_review.canonical import canonical_dumps
from compliance_review.errors import (
    RecusalError,
    StateConflictError,
    ValidationError,
)
from compliance_review.service import Service
from compliance_review.store import Store

AS_OF_Q3 = "2026-09-30"
AS_OF_Q4 = "2026-10-04"


def build_world() -> Service:
    """构造季度公示场景：两家机构、两条规则、各有扣分证据。"""
    s = Service(Store(":memory:"))
    s.create_institution("INS-A", "甲机构")
    s.create_institution("INS-B", "乙机构")
    s.create_reviewer("RV-1", "张复核")
    s.create_reviewer("RV-2", "李复核")
    # RV-1 与甲机构存在回避关系
    s.add_recusal("RV-1", "INS-A", "近亲属在甲机构任职")

    s.create_rule("R-FIN", "财务数据错报", 10.0, "2026-01-01")
    s.create_rule("R-GOV", "治理架构缺陷", 5.0, "2026-01-01")
    s.add_evidence("INS-A", "R-FIN", "2026-08-10", "2026Q3", "季报错报")
    s.add_evidence("INS-A", "R-GOV", "2026-07-01", "2026Q3", "独董缺位")
    s.add_evidence("INS-B", "R-FIN", "2026-08-20", "2026Q3", "附注遗漏")
    s.create_batch("B-2026Q3", "2026Q3", "2026年三季度公示", AS_OF_Q3)
    return s


class ScoringFlowTest(unittest.TestCase):
    def test_full_appeal_remediation_flow(self) -> None:
        s = build_world()

        # 试算：甲 85（-15）排第 2，乙 90（-10）排第 1。试算不产生版本。
        trial = s.trial("B-2026Q3", {"as_of": AS_OF_Q3})
        by = {r["institution_id"]: r for r in trial["results"]}
        self.assertEqual(by["INS-B"]["rank"], 1)
        self.assertEqual(by["INS-A"]["score"], 85.0)
        self.assertEqual(by["INS-B"]["score"], 90.0)
        self.assertEqual(s.list_versions("B-2026Q3"), [])

        # 正式发布 v1 并冻结输入摘要。
        s.create_version("B-2026Q3", "initial", "RV-2", "季度首次公示")
        v1 = s.publish_version("B-2026Q3", 1, "RV-2", as_of=AS_OF_Q3)
        self.assertIsNotNone(v1["digest"])
        self.assertEqual(v1["status"], "published")

        # 公示后甲机构完成整改并通过核验（10-01 起生效）。
        rem = s.submit_remediation(
            next(e["id"] for e in s.list_evidence("INS-A") if e["rule_code"] == "R-FIN"),
            "已重述季报",
        )
        s.verify_remediation(rem["id"], "RV-2", "2026-10-01", "材料齐全")

        # 直接改现状不会影响已发布的 v1：历史口径复算仍为 85 分。
        recomputed = s.recompute_version("B-2026Q3", 1)
        self.assertTrue(recomputed["matches_frozen"])
        v1_now = {r["institution_id"]: r for r in recomputed["recomputed_results"]}
        self.assertEqual(v1_now["INS-A"]["score"], 85.0)

        # 甲机构对仍被旧扣分项影响提出申诉并补证 → 必须产生 v2 草稿。
        appeal = s.create_appeal("INS-A", "2026Q3", "B-2026Q3", "整改已完成，旧扣分项应消除")
        out = s.supplement_appeal(appeal["id"], "整改验收报告", ["proof.pdf"], "RV-2")
        self.assertEqual(out["batch_version"]["version"], 2)
        self.assertEqual(out["batch_version"]["kind"], "appeal_supplement")
        self.assertEqual(out["batch_version"]["status"], "draft")

        # 发布前 v1 仍是当前公示版本；发布 v2 后甲升为 95（仅剩 R-GOV 扣 5）、反超乙。
        batch = next(b for b in s.list_batches() if b["id"] == "B-2026Q3")
        self.assertEqual(batch["current_version"], 1)
        v2 = s.publish_version("B-2026Q3", 2, "RV-2", as_of=AS_OF_Q4)
        by2 = {r["institution_id"]: r for r in v2["results"]}
        self.assertEqual(by2["INS-A"]["score"], 95.0)
        self.assertEqual(by2["INS-A"]["total_deduction"], 5.0)

        # 对比原结果与复核结果：甲 +10 分、排名 2→1；乙排名 1→2 但分数未变。
        cmp = s.compare_versions("B-2026Q3", 1, 2)
        self.assertFalse(cmp["identical"])
        changes = {c["institution_id"]: c for c in cmp["changed"]}
        self.assertEqual(changes["INS-A"]["score_delta"], 10.0)
        self.assertEqual(changes["INS-A"]["rank_delta"], 1)
        self.assertEqual(changes["INS-B"]["score_delta"], 0.0)
        self.assertEqual(changes["INS-B"]["rank_delta"], -1)

        # v1、v2 冻结快照各自可按历史口径复算且逐位一致。
        for v in (1, 2):
            rec = s.recompute_version("B-2026Q3", v)
            self.assertTrue(rec["matches_frozen"], f"v{v} 复算与冻结结果不一致")

    def test_rule_errata_non_retroactive_leaves_history_intact(self) -> None:
        s = build_world()
        s.create_version("B-2026Q3", "initial", "RV-2", "首次公示")
        v1 = s.publish_version("B-2026Q3", 1, "RV-2", as_of=AS_OF_Q3)
        frozen_a = next(r for r in v1["results"] if r["institution_id"] == "INS-A")

        # 10-01 勘误：R-FIN 扣分 10→6，非回溯。历史证据口径不变。
        s.issue_errata("R-FIN", "财务数据错报(勘误)", 6.0, "2026-10-01",
                       "口径细化，扣分校准", retroactive=False)
        v = s.create_version("B-2026Q3", "rule_errata", "RV-2", "R-FIN 勘误校准")
        v2 = s.publish_version("B-2026Q3", v["version"], "RV-2", as_of=AS_OF_Q4)
        a2 = next(r for r in v2["results"] if r["institution_id"] == "INS-A")
        # 8 月的证据仍按发生时生效的旧版本扣 10 分。
        self.assertEqual(a2["score"], frozen_a["score"])

        # 回溯勘误必须显式授权才改变历史口径；未授权的试算维持旧分。
        s.issue_errata("R-FIN", "财务数据错报(回溯勘误)", 6.0, "2026-07-01",
                       "口径错误，回溯纠正", retroactive=True)
        trial = s.trial("B-2026Q3", {"as_of": AS_OF_Q4})
        a_trial = next(r for r in trial["results"] if r["institution_id"] == "INS-A")
        self.assertEqual(a_trial["score"], 85.0)

    def test_partial_revocation_and_reopen_create_versions(self) -> None:
        s = build_world()
        s.create_version("B-2026Q3", "initial", "RV-2", "首次公示")
        s.publish_version("B-2026Q3", 1, "RV-2", as_of=AS_OF_Q3)

        # 部分撤销甲的一条证据 → 新版本，且撤销沿后续版本持续生效。
        gov_ev = next(e["id"] for e in s.list_evidence("INS-A") if e["rule_code"] == "R-GOV")
        v = s.partial_revoke("B-2026Q3", "INS-A", gov_ev, "证据来源不合法", "RV-2")
        self.assertEqual(v["kind"], "partial_revocation")
        self.assertEqual(v["version"], 2)
        v2 = s.publish_version("B-2026Q3", 2, "RV-2", as_of=AS_OF_Q4)
        a2 = next(r for r in v2["results"] if r["institution_id"] == "INS-A")
        self.assertEqual(a2["score"], 90.0)

        # 批次重开必须再开新版本；未发布批次禁止重开。
        reopened = s.reopen_batch("B-2026Q3", "RV-2", "监管要求复核")
        self.assertEqual(reopened["kind"], "reopen")
        self.assertEqual(reopened["version"], 3)
        batch = next(b for b in s.list_batches() if b["id"] == "B-2026Q3")
        self.assertEqual(batch["status"], "reopened")
        with self.assertRaises(StateConflictError):
            s.reopen_batch("B-2026Q3", "RV-2", "重复重开")

        # 撤销项在重开版本中仍有效。
        v3 = s.publish_version("B-2026Q3", 3, "RV-2", as_of=AS_OF_Q4)
        a3 = next(r for r in v3["results"] if r["institution_id"] == "INS-A")
        self.assertEqual(a3["score"], 90.0)
        rec3 = s.recompute_version("B-2026Q3", 3)
        self.assertTrue(rec3["matches_frozen"])

    def test_recusal_blocks_reviewer(self) -> None:
        s = build_world()
        # RV-1 与甲机构有回避关系：不能核验甲的整改、不能补证、不能定向撤销。
        ev = next(e["id"] for e in s.list_evidence("INS-A") if e["rule_code"] == "R-GOV")
        rem = s.submit_remediation(ev, "已补选独董")
        with self.assertRaises(RecusalError):
            s.verify_remediation(rem["id"], "RV-1", "2026-10-01")
        # 无回避的 RV-2 可以核验。
        s.verify_remediation(rem["id"], "RV-2", "2026-10-01")

        s.create_version("B-2026Q3", "initial", "RV-2", "首次公示")
        s.publish_version("B-2026Q3", 1, "RV-2", as_of=AS_OF_Q3)
        appeal = s.create_appeal("INS-A", "2026Q3", "B-2026Q3", "申请复核")
        with self.assertRaises(RecusalError):
            s.supplement_appeal(appeal["id"], "材料", [], "RV-1")
        with self.assertRaises(RecusalError):
            s.partial_revoke("B-2026Q3", "INS-A", ev, "撤销", "RV-1")

        # 勘误是批次级变更（影响甲机构），RV-1 同样被回避拦截。
        with self.assertRaises(RecusalError):
            s.create_version("B-2026Q3", "rule_errata", "RV-1", "勘误")

    def test_trial_is_isolated_and_draft_rules(self) -> None:
        s = build_world()
        # 未发布不能提申诉。
        with self.assertRaises(StateConflictError):
            s.create_appeal("INS-A", "2026Q3", "B-2026Q3", "提前申诉")
        s.create_version("B-2026Q3", "initial", "RV-2", "首次公示")
        # 存在草稿时不能再建版本。
        with self.assertRaises(StateConflictError):
            s.create_version("B-2026Q3", "rule_errata", "RV-2", "并行草稿")
        # 未发布版本不能按历史口径复算。
        with self.assertRaises(StateConflictError):
            s.recompute_version("B-2026Q3", 1)

    def test_draft_discard_keeps_monotonic_version_numbers(self) -> None:
        s = build_world()
        s.create_version("B-2026Q3", "initial", "RV-2", "首次公示")
        s.publish_version("B-2026Q3", 1, "RV-2", as_of=AS_OF_Q3)
        # 建一个勘误草稿后放弃；撤销/补证类草稿同理。
        s.issue_errata("R-FIN", "财务错报", 8.0, "2026-10-01", "校准")
        s.create_version("B-2026Q3", "rule_errata", "RV-2", "勘误草稿")
        s.discard_version("B-2026Q3", 2, "RV-2")
        versions = s.list_versions("B-2026Q3")
        self.assertEqual([v["version"] for v in versions], [1])
        # 新版本号不复用 v2。
        v3 = s.create_version("B-2026Q3", "rule_errata", "RV-2", "再次勘误")
        self.assertEqual(v3["version"], 3)
        # 已发布版本不可废弃。
        with self.assertRaises(StateConflictError):
            s.discard_version("B-2026Q3", 1, "RV-2")

    def test_rule_version_interval_overlap_rejected(self) -> None:
        s = Service(Store(":memory:"))
        s.create_rule("R1", "规则", 3.0, "2026-01-01")
        # 勘误生效日落在开放版本区间内会先把旧版本截断到生效日，不重叠；
        # 但在旧版本已关闭区间内部插入起点会重叠报错。
        s.issue_errata("R1", "规则", 4.0, "2026-06-01", "修订")
        with self.assertRaises(ValidationError):
            s.issue_errata("R1", "规则", 2.0, "2026-03-01", "区间内插入")


if __name__ == "__main__":
    unittest.main()
