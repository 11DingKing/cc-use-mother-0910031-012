"""季度公示异议场景演示。

情节：季度公示前，ORG-001 对合规评分提出异议——整改已完成却仍被旧扣分项
影响；直接重算会让此前公示排名整体改变。演示如何用申诉复核流程解决：
登记 → 发布（冻结输入摘要）→ 整改验证 → 申诉补证 → 回避拦截 → 部分撤销
→ 新旧结果对比 → 规则勘误与历史口径复算 → 批次重开后排名才整体更新。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from score_review import RecusalError, ScoreReviewService


def show(title: str, value) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> None:
    svc = ScoreReviewService(clock=lambda: datetime(2026, 10, 4, 9, 0, tzinfo=timezone.utc))

    svc.create_rule(code="DATA-ERROR", name="数据差错", category="报送质量",
                    deduction=10, effective_from="2026-01-01", by="监管员-甲")
    svc.create_rule(code="LATE-FILING", name="迟报材料", category="报送时效",
                    deduction=5, effective_from="2026-01-01", by="监管员-甲")
    ev1 = svc.register_evidence(org_id="ORG-001", rule_code="DATA-ERROR",
                                period="2026-Q3", description="三季度报送数据差错", by="监管员-乙")
    ev2 = svc.register_evidence(org_id="ORG-002", rule_code="LATE-FILING",
                                period="2026-Q3", description="材料迟报", by="监管员-乙")
    for ev in (ev1, ev2):
        svc.verify_evidence(ev["id"], approved=True, by="监管员-丙")

    batch = svc.create_batch(period="2026-Q3", as_of="2026-09-30", by="监管员-丙")
    published = svc.publish_batch(batch["id"], by="监管员-丙")
    show("批次发布：冻结输入摘要", {
        "batch_id": batch["id"],
        "frozen_input_digest": published["data"]["frozen_input_digest"],
        "ranking": svc.batch_results(batch["id"])["ranking"],
    })

    rect = svc.submit_rectification(evidence_id=ev1["id"], org_id="ORG-001",
                                    description="已完成数据差错整改", by="合规员-001")
    svc.verify_rectification(rect["id"], passed=True, note="现场复核通过", by="监管员-丙")
    print("\n整改已验证通过；已发布结果保持不变：",
          svc.batch_org_result(batch["id"], "ORG-001")["score"])

    item_id = svc.batch_org_result(batch["id"], "ORG-001")["entries"][0]["item_id"]
    appeal = svc.file_appeal(batch_id=batch["id"], org_id="ORG-001",
                             deduction_item_ids=[item_id],
                             reason="整改已完成仍被旧扣分项影响", by="合规员-001")
    appeal_id = appeal["id"]
    svc.supplement_appeal(appeal_id, by="合规员-001",
                          new_evidence={"rule_code": "DATA-ERROR", "description": "整改完成证明"},
                          note="补充整改完成证明")

    svc.register_recusal(reviewer_id="专家-甲", org_id="ORG-001",
                         reason="曾任该机构顾问", by="监管员-丙")
    try:
        svc.assign_reviewer(appeal_id, reviewer_id="专家-甲", by="监管员-丙")
    except RecusalError as exc:
        print("\n回避拦截：", exc.message, exc.details["reasons"])

    svc.assign_reviewer(appeal_id, reviewer_id="专家-乙", by="监管员-丙")
    svc.decide_appeal(appeal_id, revoked_item_ids=[item_id],
                      comment="整改属实，撤销该扣分项", by="专家-乙")
    show("原结果 vs 复核结果", svc.appeal_comparison(appeal_id))

    svc.errata_rule("DATA-ERROR", deduction=8, effective_from="2026-10-01",
                    reason="扣分标准勘误", by="监管员-甲")
    old = svc.recalculate_as_of(period="2026-Q3", as_of="2026-09-30", org_id="ORG-002", by="监管员-丙")
    new = svc.recalculate_as_of(period="2026-Q3", as_of="2026-10-15", org_id="ORG-002", by="监管员-丙")
    print("\n历史口径复算（ORG-002 不受勘误影响，仅示意口径差异）：")
    print("  2026-09-30 口径规则版本：", old["data"]["rules_used"])
    print("  2026-10-15 口径规则版本：", new["data"]["rules_used"])
    show("输入漂移校验", svc.digest_check(batch["id"]))

    svc.reopen_batch(batch["id"], reason="复核决定生效", by="监管员-丙")
    svc.publish_batch(batch["id"], by="监管员-丙")
    show("批次重开后重新发布：排名整体更新", svc.batch_results(batch["id"])["ranking"])


if __name__ == "__main__":
    main()
