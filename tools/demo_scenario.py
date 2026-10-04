"""命令行场景演示：季度公示 → 整改 → 申诉补证 → 复核新版本 → 新旧对比 → 历史复算。

运行：PYTHONPATH=src python3 tools/demo_scenario.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from compliance_review.service import Service
from compliance_review.store import Store

Q3_END, Q4_NOW = "2026-09-30", "2026-10-04"


def show(title: str, results: list[dict]) -> None:
    print(f"\n[{title}]")
    for r in results:
        print(f"  第{r['rank']}名 {r['institution_id']}: {r['score']} 分（扣 {r['total_deduction']}）")


def main() -> None:
    s = Service(Store(":memory:"))
    s.create_institution("INS-A", "甲机构")
    s.create_institution("INS-B", "乙机构")
    s.create_reviewer("RV-1", "张复核")
    s.create_reviewer("RV-2", "李复核")
    s.add_recusal("RV-1", "INS-A", "近亲属在甲机构任职")

    s.create_rule("R-FIN", "财务数据错报", 10.0, "2026-01-01")
    s.create_rule("R-GOV", "治理架构缺陷", 5.0, "2026-01-01")
    fin_ev = None
    for inst, code, at, detail in [
        ("INS-A", "R-FIN", "2026-08-10", "季报错报"),
        ("INS-A", "R-GOV", "2026-07-01", "独董缺位"),
        ("INS-B", "R-FIN", "2026-08-20", "附注遗漏"),
    ]:
        ev = s.add_evidence(inst, code, at, "2026Q3", detail)
        if inst == "INS-A" and code == "R-FIN":
            fin_ev = ev["id"]
    s.create_batch("B-Q3", "2026Q3", "2026年三季度公示", Q3_END)

    print("=" * 60)
    print("试算（非正式，不落版本）")
    trial = s.trial("B-Q3", {"as_of": Q3_END})
    show("试算结果", trial["results"])
    assert s.list_versions("B-Q3") == []

    print("=" * 60)
    print("正式发布 v1，冻结输入摘要")
    s.create_version("B-Q3", "initial", "RV-2", "季度首次公示")
    v1 = s.publish_version("B-Q3", 1, "RV-2", as_of=Q3_END)
    print(f"  快照摘要 sha256: {v1['digest']}")

    print("=" * 60)
    print("公示后甲机构完成整改并核验，effective_date=2026-10-01")
    rem = s.submit_remediation(fin_ev, "已重述季报")
    s.verify_remediation(rem["id"], "RV-2", "2026-10-01", "材料齐全")
    rec1 = s.recompute_version("B-Q3", 1)
    print(f"  按 v1 历史口径复算 matches_frozen={rec1['matches_frozen']}，"
          f"甲机构仍为 {next(r for r in rec1['recomputed_results'] if r['institution_id']=='INS-A')['score']} 分")

    print("=" * 60)
    print("甲机构申诉：已整改却仍被旧扣分项影响 → 补证产生 v2")
    appeal = s.create_appeal("INS-A", "2026Q3", "B-Q3", "整改已完成，旧扣分项应消除")
    out = s.supplement_appeal(appeal["id"], "整改验收报告", ["proof.pdf"], "RV-2")
    print(f"  新版本 v{out['batch_version']['version']}（{out['batch_version']['kind']}，草稿）")
    v2 = s.publish_version("B-Q3", 2, "RV-2", as_of=Q4_NOW)
    show("复核发布结果 v2", v2["results"])

    print("=" * 60)
    print("原结果 v1 与复核结果 v2 对比")
    cmp_ = s.compare_versions("B-Q3", 1, 2)
    for c in cmp_["changed"]:
        print(f"  {c['institution_id']}: {c['left']['score']}→{c['right']['score']} "
              f"(Δ{c['score_delta']:+.0f})，排名 {c['left']['rank']}→{c['right']['rank']}")

    print("=" * 60)
    print("按历史口径复算两个已发布版本")
    for v in (1, 2):
        rec = s.recompute_version("B-Q3", v)
        print(f"  v{v}: matches_frozen={rec['matches_frozen']}")

    print("=" * 60)
    print("回避校验：RV-1 与 INS-A 有回避关系，参与补证被拒")
    try:
        s.supplement_appeal(appeal["id"], "违规操作", [], "RV-1")
    except Exception as exc:  # noqa: BLE001
        print(f"  已拦截：{exc}")


if __name__ == "__main__":
    main()
