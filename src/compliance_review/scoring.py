"""评分引擎（纯函数）。

口径：
- 每家机构满分 100，命中证据按证据发生时点生效的规则版本扣分。
- 规则版本有生效区间 [start_date, end_date)，同一 code 区间不得重叠。
- 已核验整改在 effective_date 起消除对应扣分项（旧扣分项不再影响当前评分），
  但在按历史批次口径复算时，以该批次冻结快照中的整改状态为准。
- 规则勘误默认不回溯：只影响勘误生效日之后发生的证据；
  retroactive=true 的勘误可对历史证据重新适用（在快照中显式记录）。
"""
from __future__ import annotations

from typing import Any

BASE_SCORE = 100.0


def resolve_rule(rule_versions: list[dict], rule_code: str, on_date: str) -> dict | None:
    """返回某规则 code 在 on_date 时点生效的版本。"""
    candidates = [
        rv
        for rv in rule_versions
        if rv["code"] == rule_code
        and rv["start_date"] <= on_date
        and (rv["end_date"] is None or on_date < rv["end_date"])
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda rv: rv["version"])


def score_institution(
    institution_id: str,
    evidence: list[dict],
    rule_versions: list[dict],
    remediations: list[dict],
    *,
    as_of: str,
    retroactive_codes: set[str] | None = None,
    revoked_evidence_ids: set[str] | None = None,
) -> dict:
    """计算单家机构截至 as_of 的评分与扣分明细。

    remediation 记录形如 {evidence_id, status, effective_date}，
    status == "verified" 且 effective_date <= as_of 的整改可消除扣分。
    retroactive_codes: 允许追溯适用的勘误规则 code 集合。
    revoked_evidence_ids: 经部分撤销的证据，从该版本起不再参与评分。
    """
    retroactive_codes = retroactive_codes or set()
    revoked_evidence_ids = revoked_evidence_ids or set()
    remediated = {
        r["evidence_id"]
        for r in remediations
        if r.get("status") == "verified"
        and r.get("effective_date")
        and r["effective_date"] <= as_of
    }

    items: list[dict] = []
    total_deduction = 0.0
    for ev in evidence:
        if ev["institution_id"] != institution_id or ev["occurred_at"] > as_of:
            continue
        if ev["id"] in revoked_evidence_ids:
            continue
        rv = resolve_rule(rule_versions, ev["rule_code"], ev["occurred_at"])
        if rv is None:
            items.append(
                {
                    "evidence_id": ev["id"],
                    "rule_code": ev["rule_code"],
                    "deduction": 0.0,
                    "applied_rule_version": None,
                    "status": "no_rule_in_force",
                }
            )
            continue

        # 回溯勘误：仅在批次快照显式授权（retroactive_codes）时才对历史证据适用；
        # 否则回退到该证据发生时点生效的、勘误之前的最后版本。
        # 非回溯勘误由生效区间天然隔离，不会作用于勘误生效前的证据。
        is_errata = rv["kind"] == "errata"
        if is_errata and rv.get("retroactive") and rv["code"] not in retroactive_codes:
            prior = [old for old in rule_versions if old["version"] < rv["version"]]
            fallback = resolve_rule(prior, ev["rule_code"], ev["occurred_at"])
            if fallback is not None:
                rv = fallback

        if ev["id"] in remediated:
            items.append(
                {
                    "evidence_id": ev["id"],
                    "rule_code": ev["rule_code"],
                    "rule_version": rv["version"],
                    "deduction": 0.0,
                    "status": "remediated",
                }
            )
            continue

        total_deduction += rv["deduction"]
        items.append(
            {
                "evidence_id": ev["id"],
                "rule_code": ev["rule_code"],
                "rule_version": rv["version"],
                "deduction": rv["deduction"],
                "status": "deducted",
            }
        )

    score = round(max(0.0, BASE_SCORE - total_deduction), 2)
    return {
        "institution_id": institution_id,
        "score": score,
        "deductions": items,
        "total_deduction": round(total_deduction, 2),
    }


def score_all(
    institutions: list[dict],
    evidence: list[dict],
    rule_versions: list[dict],
    remediations: list[dict],
    *,
    as_of: str,
    retroactive_codes: set[str] | None = None,
    revoked_evidence_ids: set[str] | None = None,
) -> list[dict]:
    results = [
        score_institution(
            inst["id"],
            evidence,
            rule_versions,
            remediations,
            as_of=as_of,
            retroactive_codes=retroactive_codes,
            revoked_evidence_ids=revoked_evidence_ids,
        )
        for inst in institutions
    ]
    # 排名：分数降序，同分按机构 id 稳定排序。
    results.sort(key=lambda r: (-r["score"], r["institution_id"]))
    for rank, result in enumerate(results, start=1):
        result["rank"] = rank
    return results


def recompute_from_snapshot(snapshot: dict) -> list[dict]:
    """按历史批次冻结的输入快照、以原口径确定性复算。

    快照包含 as_of、rule_versions、evidence、remediations、institutions、
    retroactive_codes，复算结果应与发布时 results 一致（用于审计校验）。
    """
    return score_all(
        snapshot["institutions"],
        snapshot["evidence"],
        snapshot["rule_versions"],
        snapshot["remediations"],
        as_of=snapshot["as_of"],
        retroactive_codes=set(snapshot.get("retroactive_codes", [])),
        revoked_evidence_ids=set(snapshot.get("revoked_evidence_ids", [])),
    )
