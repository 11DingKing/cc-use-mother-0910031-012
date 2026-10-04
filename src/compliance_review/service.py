"""业务服务层：评分规则时态、批次版本、发布冻结、回避、对比与历史复算。"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any

from .canonical import canonical_dumps, digest
from .errors import (
    NotFoundError,
    RecusalError,
    StateConflictError,
    ValidationError,
)
from .scoring import BASE_SCORE, recompute_from_snapshot, score_all
from .store import Store

VERSION_KINDS = {
    "initial",          # 首次发布
    "appeal_supplement",  # 申诉补证
    "rule_errata",       # 规则勘误
    "partial_revocation",  # 部分撤销
    "reopen",            # 批次重开后的复核
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    return date.today().isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Service:
    def __init__(self, store: Store) -> None:
        self.store = store

    # ------------------------------------------------------------------ 审计
    def _audit(self, action: str, entity: str, payload: Any, actor: str | None = None) -> None:
        self.store.execute(
            "INSERT INTO audit_log(at, actor, action, entity, payload) VALUES(:at,:actor,:action,:entity,:payload)",
            at=_now(), actor=actor, action=action, entity=entity, payload=payload,
        )

    # ---------------------------------------------------------- 机构与复核人
    def create_institution(self, inst_id: str, name: str) -> dict:
        if self.store.one("SELECT id FROM institutions WHERE id=:id", id=inst_id):
            raise StateConflictError(f"机构已存在：{inst_id}")
        self.store.execute(
            "INSERT INTO institutions(id,name,created_at) VALUES(:id,:name,:at)",
            id=inst_id, name=name, at=_now(),
        )
        self._audit("create_institution", inst_id, {"name": name})
        return self.store.get("SELECT * FROM institutions WHERE id=:id", id=inst_id)

    def list_institutions(self) -> list[dict]:
        return self.store.query("SELECT * FROM institutions ORDER BY id")

    def create_reviewer(self, reviewer_id: str, name: str) -> dict:
        if self.store.one("SELECT id FROM reviewers WHERE id=:id", id=reviewer_id):
            raise StateConflictError(f"复核人已存在：{reviewer_id}")
        self.store.execute(
            "INSERT INTO reviewers(id,name,created_at) VALUES(:id,:name,:at)",
            id=reviewer_id, name=name, at=_now(),
        )
        return self.store.get("SELECT * FROM reviewers WHERE id=:id", id=reviewer_id)

    def add_recusal(self, reviewer_id: str, institution_id: str, reason: str) -> dict:
        self._require_reviewer(reviewer_id)
        self._require_institution(institution_id)
        if self.store.one(
            "SELECT 1 FROM recusals WHERE reviewer_id=:r AND institution_id=:i",
            r=reviewer_id, i=institution_id,
        ):
            raise StateConflictError("回避关系已登记")
        rid = self.store.execute(
            "INSERT INTO recusals(reviewer_id,institution_id,reason,created_at)"
            " VALUES(:r,:i,:reason,:at)",
            r=reviewer_id, i=institution_id, reason=reason, at=_now(),
        )
        self._audit("add_recusal", reviewer_id, {"institution_id": institution_id, "reason": reason})
        return self.store.get("SELECT * FROM recusals WHERE id=:id", id=rid)

    def assert_not_recused(self, reviewer_id: str, institution_id: str) -> None:
        """复核回避：复核人与被评机构存在登记关系时禁止参与。"""
        row = self.store.one(
            "SELECT reason FROM recusals WHERE reviewer_id=:r AND institution_id=:i",
            r=reviewer_id, i=institution_id,
        )
        if row:
            raise RecusalError(
                f"复核人 {reviewer_id} 与机构 {institution_id} 存在回避关系：{row['reason']}"
            )

    def _require_reviewer(self, reviewer_id: str) -> dict:
        row = self.store.one("SELECT * FROM reviewers WHERE id=:id", id=reviewer_id)
        if row is None:
            raise NotFoundError(f"复核人不存在：{reviewer_id}")
        return row

    def _require_institution(self, inst_id: str) -> dict:
        row = self.store.one("SELECT * FROM institutions WHERE id=:id", id=inst_id)
        if row is None:
            raise NotFoundError(f"机构不存在：{inst_id}")
        return row

    # ---------------------------------------------------------------- 规则
    def create_rule(
        self, code: str, name: str, deduction: float, start_date: str, *, kind: str = "normal"
    ) -> dict:
        return self._insert_rule_version(code, name, deduction, start_date, None, kind, False, None)

    def issue_errata(
        self,
        code: str,
        name: str,
        deduction: float,
        start_date: str,
        reason: str,
        *,
        retroactive: bool = False,
    ) -> dict:
        """发布规则勘误：新版本生效，旧开放版本在生效日截止。

        retroactive=True 时 start_date 可早于今天，表示对自该日起的历史证据纠正；
        是否在某次复核中真正追溯适用，由批次版本快照的 retroactive_codes 授权。
        """
        if not self.store.one("SELECT 1 FROM rule_versions WHERE code=:c", c=code):
            raise NotFoundError(f"规则不存在，无法勘误：{code}")
        return self._insert_rule_version(
            code, name, deduction, start_date, None, "errata", retroactive, reason
        )

    def _insert_rule_version(
        self, code, name, deduction, start_date, end_date, kind, retroactive, reason
    ) -> dict:
        if deduction < 0 or deduction > BASE_SCORE:
            raise ValidationError("扣分项必须在 0..100 之间")
        if kind not in ("normal", "errata"):
            raise ValidationError("规则类型必须是 normal 或 errata")
        row = self.store.one(
            "SELECT COALESCE(MAX(version),0) AS v FROM rule_versions WHERE code=:c", c=code
        )
        version = row["v"] + 1
        new_end = end_date or "9999-12-31"

        # 回溯勘误是旁路纠正，可与历史时间轴区间共存，不参与截断与重叠判定。
        if not retroactive:
            existing = self.store.query("SELECT * FROM rule_versions WHERE code=:c", c=code)
            temporal = [
                rv for rv in existing
                if not (rv["kind"] == "errata" and rv["retroactive"])
            ]
            # 只对已闭合的历史区间做重叠校验；开放版本将在下面被截断到 start_date，
            # 形成 [...,start) 与 [start,...) 的无缝衔接，天然不重叠。
            for rv in temporal:
                if rv["end_date"] is None:
                    continue
                if start_date < rv["end_date"] and rv["start_date"] < new_end:
                    raise ValidationError(
                        f"规则 {code} 生效区间与版本 {rv['version']} 重叠"
                    )
            self.store.execute(
                "UPDATE rule_versions SET end_date=:s WHERE code=:c AND end_date IS NULL"
                " AND NOT (kind='errata' AND retroactive=1)",
                s=start_date, c=code,
            )
        self.store.execute(
            "INSERT INTO rule_versions(code,version,name,deduction,start_date,end_date,kind,"
            "reason,retroactive,created_at) VALUES(:code,:version,:name,:deduction,:sd,:ed,"
            ":kind,:reason,:retro,:at)",
            code=code, version=version, name=name, deduction=deduction, sd=start_date,
            ed=end_date, kind=kind, reason=reason, retro=retroactive, at=_now(),
        )
        self._audit(
            "issue_rule_version", f"{code}:v{version}",
            {"code": code, "version": version, "kind": kind, "retroactive": retroactive},
        )
        return self.store.get(
            "SELECT * FROM rule_versions WHERE code=:c AND version=:v", c=code, v=version
        )

    def list_rule_versions(self, code: str | None = None) -> list[dict]:
        if code:
            return self.store.query(
                "SELECT * FROM rule_versions WHERE code=:c ORDER BY version", c=code
            )
        return self.store.query("SELECT * FROM rule_versions ORDER BY code,version")

    # ---------------------------------------------------------------- 证据
    def add_evidence(
        self, institution_id: str, rule_code: str, occurred_at: str, period: str,
        detail: str | None = None,
    ) -> dict:
        self._require_institution(institution_id)
        if not self.store.one("SELECT 1 FROM rule_versions WHERE code=:c", c=rule_code):
            raise NotFoundError(f"规则不存在：{rule_code}")
        ev_id = _new_id("ev")
        self.store.execute(
            "INSERT INTO evidence(id,institution_id,rule_code,occurred_at,period,detail,created_at)"
            " VALUES(:id,:inst,:code,:at,:period,:detail,:cat)",
            id=ev_id, inst=institution_id, code=rule_code, at=occurred_at,
            period=period, detail=detail, cat=_now(),
        )
        self._audit("add_evidence", ev_id, {"institution_id": institution_id, "rule_code": rule_code})
        return self.store.get("SELECT * FROM evidence WHERE id=:id", id=ev_id)

    def list_evidence(self, institution_id: str | None = None) -> list[dict]:
        if institution_id:
            return self.store.query(
                "SELECT * FROM evidence WHERE institution_id=:i ORDER BY occurred_at",
                i=institution_id,
            )
        return self.store.query("SELECT * FROM evidence ORDER BY occurred_at")

    # ---------------------------------------------------------------- 整改
    def submit_remediation(self, evidence_id: str, note: str) -> dict:
        ev = self.store.one("SELECT * FROM evidence WHERE id=:id", id=evidence_id)
        if ev is None:
            raise NotFoundError(f"证据不存在：{evidence_id}")
        if self.store.one(
            "SELECT 1 FROM remediations WHERE evidence_id=:e AND status != 'rejected'",
            e=evidence_id,
        ):
            raise StateConflictError("该证据已有进行中的整改记录")
        rid = _new_id("rem")
        self.store.execute(
            "INSERT INTO remediations(id,evidence_id,status,submitted_at,submit_note)"
            " VALUES(:id,:e,'submitted',:at,:note)",
            id=rid, e=evidence_id, at=_now(), note=note,
        )
        self._audit("submit_remediation", rid, {"evidence_id": evidence_id})
        return self.store.get("SELECT * FROM remediations WHERE id=:id", id=rid)

    def verify_remediation(
        self, remediation_id: str, verifier_id: str, effective_date: str, note: str | None = None
    ) -> dict:
        """核验整改：核验人受回避限制；effective_date 起旧扣分项消除。"""
        rem = self.store.one("SELECT * FROM remediations WHERE id=:id", id=remediation_id)
        if rem is None:
            raise NotFoundError(f"整改记录不存在：{remediation_id}")
        if rem["status"] == "verified":
            raise StateConflictError("整改已核验")
        ev = self.store.get("SELECT * FROM evidence WHERE id=:id", id=rem["evidence_id"])
        self._require_reviewer(verifier_id)
        self.assert_not_recused(verifier_id, ev["institution_id"])
        self.store.execute(
            "UPDATE remediations SET status='verified', verified_at=:vat,"
            " effective_date=:ed, verifier_id=:v, verify_note=:note WHERE id=:id",
            vat=_now(), ed=effective_date, v=verifier_id, note=note, id=remediation_id,
        )
        self._audit(
            "verify_remediation", remediation_id,
            {"evidence_id": rem["evidence_id"], "effective_date": effective_date},
            actor=verifier_id,
        )
        return self.store.get("SELECT * FROM remediations WHERE id=:id", id=remediation_id)

    def list_remediations(self, evidence_id: str | None = None) -> list[dict]:
        if evidence_id:
            return self.store.query(
                "SELECT * FROM remediations WHERE evidence_id=:e", e=evidence_id
            )
        return self.store.query("SELECT * FROM remediations ORDER BY submitted_at")

    # ---------------------------------------------------------------- 批次
    def create_batch(self, batch_id: str, period: str, name: str, cutoff_date: str) -> dict:
        if self.store.one("SELECT 1 FROM batches WHERE id=:id", id=batch_id):
            raise StateConflictError(f"批次已存在：{batch_id}")
        self.store.execute(
            "INSERT INTO batches(id,period,name,cutoff_date,status,created_at)"
            " VALUES(:id,:period,:name,:cut,'open',:at)",
            id=batch_id, period=period, name=name, cut=cutoff_date, at=_now(),
        )
        self._audit("create_batch", batch_id, {"period": period, "cutoff_date": cutoff_date})
        return self.store.get("SELECT * FROM batches WHERE id=:id", id=batch_id)

    def list_batches(self) -> list[dict]:
        return self.store.query("SELECT * FROM batches ORDER BY created_at")

    def _require_batch(self, batch_id: str) -> dict:
        row = self.store.one("SELECT * FROM batches WHERE id=:id", id=batch_id)
        if row is None:
            raise NotFoundError(f"批次不存在：{batch_id}")
        return row

    def _no_open_draft(self, batch_id: str) -> None:
        if self.store.one(
            "SELECT 1 FROM batch_versions WHERE batch_id=:b AND status='draft'", b=batch_id
        ):
            raise StateConflictError("批次存在未发布的草稿版本，请先发布或放弃")

    def _published_revoked_ids(self, batch_id: str) -> set[str]:
        rows = self.store.query(
            "SELECT evidence_id FROM revocations r JOIN batch_versions v"
            " ON r.batch_id=v.batch_id AND r.version=v.version"
            " WHERE r.batch_id=:b AND v.status='published'",
            b=batch_id,
        )
        return {r["evidence_id"] for r in rows}

    def _collect_inputs(self, batch: dict, revoked: set[str], retroactive_codes: set[str],
                        as_of: str) -> dict:
        period, cutoff = batch["period"], batch["cutoff_date"]
        evidence = self.store.query(
            "SELECT * FROM evidence WHERE period=:p AND occurred_at<=:cut ORDER BY occurred_at,id",
            p=period, cut=cutoff,
        )
        ev_ids = [e["id"] for e in evidence]
        remediations: list[dict] = []
        if ev_ids:
            placeholders = ",".join(f":e{i}" for i in range(len(ev_ids)))
            params = {f"e{i}": eid for i, eid in enumerate(ev_ids)}
            remediations = self.store.query(
                f"SELECT * FROM remediations WHERE evidence_id IN ({placeholders})"
                " ORDER BY evidence_id, submitted_at", **params
            )
        rules = self.store.query(
            "SELECT code,version,name,deduction,start_date,end_date,kind,retroactive"
            " FROM rule_versions ORDER BY code,version"
        )
        for rv in rules:
            rv["retroactive"] = bool(rv["retroactive"])
        institutions = self.list_institutions()
        results = score_all(
            institutions, evidence, rules, remediations,
            as_of=as_of, retroactive_codes=retroactive_codes,
            revoked_evidence_ids=revoked,
        )
        snapshot = {
            "batch_id": batch["id"],
            "period": period,
            "cutoff_date": cutoff,
            "as_of": as_of,
            "institutions": sorted(institutions, key=lambda x: x["id"]),
            "rule_versions": rules,
            "evidence": evidence,
            "remediations": remediations,
            "revoked_evidence_ids": sorted(revoked),
            "retroactive_codes": sorted(retroactive_codes),
        }
        return {"snapshot": snapshot, "results": results}

    def trial(self, batch_id: str, params: dict | None = None) -> dict:
        """试算：不落正式版本，不修改任何发布结果。"""
        params = params or {}
        batch = self._require_batch(batch_id)
        as_of = params.get("as_of") or _today()
        revoked = self._published_revoked_ids(batch_id) | set(params.get("revoked_evidence_ids", []))
        retroactive_codes = set(params.get("retroactive_codes", []))
        collected = self._collect_inputs(batch, revoked, retroactive_codes, as_of)
        trial_id = self.store.execute(
            "INSERT INTO trials(batch_id,params,results,created_at)"
            " VALUES(:b,:params,:results,:at)",
            b=batch_id, params=params, results=collected["results"], at=_now(),
        )
        return {
            "trial_id": trial_id,
            "batch_id": batch_id,
            "params": {
                "as_of": as_of,
                "revoked_evidence_ids": sorted(revoked),
                "retroactive_codes": sorted(retroactive_codes),
            },
            "results": collected["results"],
        }

    def create_version(
        self,
        batch_id: str,
        kind: str,
        created_by: str,
        reason: str,
        *,
        appeal_id: str | None = None,
        revoked_evidence_ids: list[str] | None = None,
        retroactive_codes: list[str] | None = None,
        target_institution_id: str | None = None,
    ) -> dict:
        """创建草稿版本（申诉补证/规则勘误/部分撤销/重开均必须产生新版本）。"""
        batch = self._require_batch(batch_id)
        if kind not in VERSION_KINDS:
            raise ValidationError(f"未知版本类型：{kind}")
        self._no_open_draft(batch_id)
        # 版本号由批次单调序号分配：废弃草稿不复用号段，保证审计可追溯。
        version = batch["next_version"]
        if kind == "initial" and version != 1:
            raise ValidationError("首个版本必须为 initial")
        if kind != "initial" and version == 1:
            raise ValidationError("首个版本必须为 initial")

        # 复核回避：定向变更（补证/部分撤销）检查目标机构；
        # 批次级变更（首次发布/勘误/重开）覆盖全部机构，逐一检查。
        self._require_reviewer(created_by)
        if target_institution_id:
            self.assert_not_recused(created_by, target_institution_id)
        elif kind in ("initial", "rule_errata", "reopen"):
            for inst in self.list_institutions():
                self.assert_not_recused(created_by, inst["id"])

        revoked = self._published_revoked_ids(batch_id) | set(revoked_evidence_ids or [])
        retroactive_codes = set(retroactive_codes or [])

        # 部分撤销：登记撤销项并校验证据归属。
        if kind == "partial_revocation":
            for ev_id in revoked_evidence_ids or []:
                ev = self.store.one("SELECT * FROM evidence WHERE id=:id", id=ev_id)
                if ev is None:
                    raise NotFoundError(f"证据不存在：{ev_id}")
                if target_institution_id and ev["institution_id"] != target_institution_id:
                    raise ValidationError("撤销证据不属于申诉机构")
                self.store.execute(
                    "INSERT INTO revocations(batch_id,version,institution_id,evidence_id,reason,"
                    "created_by,created_at) VALUES(:b,:v,:inst,:e,:reason,:by,:at)",
                    b=batch_id, v=version, inst=ev["institution_id"], e=ev_id,
                    reason=reason, by=created_by, at=_now(),
                )

        # 草稿结果：按当前数据的试算预览；发布时才构建并冻结正式快照。
        preview = self._collect_inputs(batch, revoked, retroactive_codes, _today())
        self.store.execute(
            "INSERT INTO batch_versions(batch_id,version,kind,status,created_at,created_by,reason,"
            "appeal_id,results) VALUES(:b,:v,:kind,'draft',:at,:by,:reason,:appeal,:results)",
            b=batch_id, v=version, kind=kind, at=_now(), by=created_by, reason=reason,
            appeal=appeal_id, results=preview["results"],
        )
        self.store.execute(
            "UPDATE batches SET next_version=:v WHERE id=:b", v=version + 1, b=batch_id
        )
        self._audit(
            "create_version", f"{batch_id}:v{version}",
            {"kind": kind, "reason": reason, "appeal_id": appeal_id}, actor=created_by,
        )
        return self.get_version(batch_id, version)

    def publish_version(self, batch_id: str, version: int, published_by: str,
                        as_of: str | None = None) -> dict:
        """发布：此刻构建输入快照、计算正式结果并冻结（digest 入卷）。"""
        as_of = as_of or _today()
        bv = self._require_version(batch_id, version)
        if bv["status"] != "draft":
            raise StateConflictError("仅草稿版本可发布")
        batch = self._require_batch(batch_id)
        # 重开/撤销等定向版本在创建时已做回避检查；发布人若是复核人同样校验。
        if self.store.one("SELECT 1 FROM reviewers WHERE id=:id", id=published_by):
            target = None
            if bv["appeal_id"]:
                appeal = self.store.one("SELECT * FROM appeals WHERE id=:id", id=bv["appeal_id"])
                target = appeal["institution_id"] if appeal else None
            insts = [target] if target else [i["id"] for i in self.list_institutions()]
            for inst_id in insts:
                self.assert_not_recused(published_by, inst_id)

        # 以该草稿登记的撤销集与勘误授权重算，并在发布瞬间冻结。
        rev_rows = self.store.query(
            "SELECT evidence_id FROM revocations WHERE batch_id=:b AND version<=:v",
            b=batch_id, v=version,
        )
        revoked = {r["evidence_id"] for r in rev_rows}
        # 回溯勘误授权沿版本继承；rule_errata 版本发布时把所有回溯勘误纳入授权。
        prior = self.store.one(
            "SELECT snapshot FROM batch_versions WHERE batch_id=:b AND version<:v"
            " AND status='published' ORDER BY version DESC LIMIT 1",
            b=batch_id, v=version,
        )
        retroactive_codes: set[str] = set()
        if prior and prior["snapshot"]:
            import json
            retroactive_codes = set(
                json.loads(prior["snapshot"]).get("retroactive_codes", [])
            )
        if bv["kind"] == "rule_errata":
            retroactive_codes |= {
                rv["code"] for rv in self.list_rule_versions()
                if rv["kind"] == "errata" and bool(rv["retroactive"])
            }
        collected = self._collect_inputs(batch, revoked, retroactive_codes, as_of)
        snapshot, results = collected["snapshot"], collected["results"]
        snapshot_digest = digest(snapshot)
        results_digest = digest(results)
        self.store.execute(
            "UPDATE batch_versions SET status='published', snapshot=:snap, digest=:dig,"
            " results=:res WHERE batch_id=:b AND version=:v",
            snap=snapshot, dig=snapshot_digest, res=results, b=batch_id, v=version,
        )
        self.store.execute(
            "UPDATE batches SET status='published', current_version=:v WHERE id=:b",
            v=version, b=batch_id,
        )
        self._audit(
            "publish_version", f"{batch_id}:v{version}",
            {"snapshot_digest": snapshot_digest, "results_digest": results_digest},
            actor=published_by,
        )
        return self.get_version(batch_id, version)

    def reopen_batch(self, batch_id: str, reviewer_id: str, reason: str) -> dict:
        """批次重开：已发布批次才能重开，且必须开新版本。"""
        batch = self._require_batch(batch_id)
        if batch["status"] != "published":
            raise StateConflictError("仅已发布批次可重开")
        version = self.create_version(batch_id, "reopen", reviewer_id, reason)
        self.store.execute("UPDATE batches SET status='reopened' WHERE id=:b", b=batch_id)
        self._audit("reopen_batch", batch_id, {"reason": reason}, actor=reviewer_id)
        return version

    def discard_version(self, batch_id: str, version: int, reviewer_id: str) -> dict:
        """放弃未发布的草稿版本（已发布版本不可废弃）。"""
        bv = self._require_version(batch_id, version)
        if bv["status"] != "draft":
            raise StateConflictError("仅草稿版本可放弃")
        self._require_reviewer(reviewer_id)
        self.store.execute(
            "DELETE FROM revocations WHERE batch_id=:b AND version=:v",
            b=batch_id, v=version,
        )
        self.store.execute(
            "DELETE FROM batch_versions WHERE batch_id=:b AND version=:v",
            b=batch_id, v=version,
        )
        # 若批次因重开进入 reopened，废弃重开草稿后回到 published。
        batch = self._require_batch(batch_id)
        if batch["status"] == "reopened":
            self.store.execute(
                "UPDATE batches SET status='published' WHERE id=:b", b=batch_id
            )
        self._audit("discard_version", f"{batch_id}:v{version}", {}, actor=reviewer_id)
        return {"discarded": f"{batch_id}:v{version}"}

    def list_versions(self, batch_id: str) -> list[dict]:
        self._require_batch(batch_id)
        rows = self.store.query(
            "SELECT batch_id,version,kind,status,created_at,created_by,reason,appeal_id,digest"
            " FROM batch_versions WHERE batch_id=:b ORDER BY version",
            b=batch_id,
        )
        return rows

    def _require_version(self, batch_id: str, version: int) -> dict:
        row = self.store.one(
            "SELECT * FROM batch_versions WHERE batch_id=:b AND version=:v",
            b=batch_id, v=version,
        )
        if row is None:
            raise NotFoundError(f"版本不存在：{batch_id} v{version}")
        if isinstance(row["results"], str):
            import json
            row["results"] = json.loads(row["results"])
        if isinstance(row.get("snapshot"), str):
            import json
            row["snapshot"] = json.loads(row["snapshot"])
        return row

    def get_version(self, batch_id: str, version: int) -> dict:
        return self._require_version(batch_id, version)

    # ---------------------------------------------------------------- 申诉
    def create_appeal(
        self, institution_id: str, period: str, batch_id: str, reason: str
    ) -> dict:
        self._require_institution(institution_id)
        batch = self._require_batch(batch_id)
        if batch["status"] != "published":
            raise StateConflictError("仅可对已发布批次提出申诉")
        appeal_id = _new_id("ap")
        self.store.execute(
            "INSERT INTO appeals(id,institution_id,period,batch_id,reason,status,created_at)"
            " VALUES(:id,:inst,:period,:b,:reason,'open',:at)",
            id=appeal_id, inst=institution_id, period=period, b=batch_id,
            reason=reason, at=_now(),
        )
        self._audit("create_appeal", appeal_id, {"institution_id": institution_id, "batch_id": batch_id})
        return self.store.get("SELECT * FROM appeals WHERE id=:id", id=appeal_id)

    def supplement_appeal(
        self, appeal_id: str, note: str, attachments: list[str], reviewer_id: str
    ) -> dict:
        """申诉补证：登记材料并立即产生新的草稿版本（appeal_supplement）。"""
        appeal = self.store.one("SELECT * FROM appeals WHERE id=:id", id=appeal_id)
        if appeal is None:
            raise NotFoundError(f"申诉不存在：{appeal_id}")
        if appeal["status"] != "open":
            raise StateConflictError("申诉已作出决定，不能继续补证")
        self.assert_not_recused(reviewer_id, appeal["institution_id"])
        sid = self.store.execute(
            "INSERT INTO supplements(appeal_id,note,attachments,created_at)"
            " VALUES(:a,:note,:att,:at)",
            a=appeal_id, note=note, att=list(attachments), at=_now(),
        )
        version = self.create_version(
            appeal["batch_id"], "appeal_supplement", reviewer_id,
            reason=f"申诉 {appeal_id} 补证：{note}", appeal_id=appeal_id,
            target_institution_id=appeal["institution_id"],
        )
        self._audit(
            "supplement_appeal", appeal_id,
            {"supplement_id": sid, "batch_version": version["version"]}, actor=reviewer_id,
        )
        supplement = self.store.get("SELECT * FROM supplements WHERE id=:id", id=sid)
        import json
        supplement["attachments"] = json.loads(supplement["attachments"])
        return {
            "supplement": supplement,
            "batch_version": version,
        }

    def decide_appeal(
        self, appeal_id: str, decider_id: str, decision: str, note: str
    ) -> dict:
        """申诉决定：granted 维持新版本流程（草稿须已发布）；rejected 记录驳回。"""
        appeal = self.store.one("SELECT * FROM appeals WHERE id=:id", id=appeal_id)
        if appeal is None:
            raise NotFoundError(f"申诉不存在：{appeal_id}")
        if appeal["status"] != "open":
            raise StateConflictError("申诉已决定")
        if decision not in ("granted", "rejected"):
            raise ValidationError("decision 必须为 granted 或 rejected")
        self._require_reviewer(decider_id)
        self.assert_not_recused(decider_id, appeal["institution_id"])
        self.store.execute(
            "UPDATE appeals SET status=:d, decided_at=:at, decider_id=:by, decision_note=:note"
            " WHERE id=:id",
            d=decision, at=_now(), by=decider_id, note=note, id=appeal_id,
        )
        self._audit("decide_appeal", appeal_id, {"decision": decision}, actor=decider_id)
        return self.store.get("SELECT * FROM appeals WHERE id=:id", id=appeal_id)

    def list_appeals(self, institution_id: str | None = None) -> list[dict]:
        if institution_id:
            return self.store.query(
                "SELECT * FROM appeals WHERE institution_id=:i ORDER BY created_at",
                i=institution_id,
            )
        return self.store.query("SELECT * FROM appeals ORDER BY created_at")

    # ------------------------------------------------ 部分撤销（便捷入口）
    def partial_revoke(
        self, batch_id: str, institution_id: str, evidence_id: str,
        reason: str, reviewer_id: str,
    ) -> dict:
        self._require_institution(institution_id)
        ev = self.store.one("SELECT * FROM evidence WHERE id=:id", id=evidence_id)
        if ev is None:
            raise NotFoundError(f"证据不存在：{evidence_id}")
        if ev["institution_id"] != institution_id:
            raise ValidationError("证据与机构不匹配")
        return self.create_version(
            batch_id, "partial_revocation", reviewer_id, reason,
            revoked_evidence_ids=[evidence_id], target_institution_id=institution_id,
        )

    # ------------------------------------------------------------ 对比/复算
    def compare_versions(self, batch_id: str, left: int, right: int) -> dict:
        """对比两个版本（通常 left=原结果，right=复核结果）。"""
        lv = self._require_version(batch_id, left)
        rv = self._require_version(batch_id, right)
        l_by = {r["institution_id"]: r for r in lv["results"]}
        r_by = {r["institution_id"]: r for r in rv["results"]}
        changes = []
        for inst_id in sorted(set(l_by) | set(r_by)):
            a, b = l_by.get(inst_id), r_by.get(inst_id)
            if a is None or b is None or a["score"] != b["score"] or a["rank"] != b["rank"]:
                changes.append({
                    "institution_id": inst_id,
                    "left": None if a is None else {"score": a["score"], "rank": a["rank"]},
                    "right": None if b is None else {"score": b["score"], "rank": b["rank"]},
                    "score_delta": None if (a is None or b is None) else round(b["score"] - a["score"], 2),
                    "rank_delta": None if (a is None or b is None) else a["rank"] - b["rank"],
                })
        return {
            "batch_id": batch_id,
            "left_version": left,
            "right_version": right,
            "left_status": lv["status"],
            "right_status": rv["status"],
            "changed": changes,
            "identical": not changes,
        }

    def recompute_version(self, batch_id: str, version: int) -> dict:
        """按发布时冻结的输入摘要与历史口径复算，并校验与正式结果逐位一致。"""
        bv = self._require_version(batch_id, version)
        if bv["status"] != "published" or not bv.get("snapshot"):
            raise StateConflictError("仅已发布版本可按历史口径复算")
        snapshot = bv["snapshot"]
        recomputed = recompute_from_snapshot(snapshot)
        frozen = bv["results"]
        return {
            "batch_id": batch_id,
            "version": version,
            "snapshot_digest": bv["digest"],
            "recomputed_digest": digest({"results": recomputed}),
            "frozen_digest": digest({"results": frozen}),
            "matches_frozen": canonical_dumps(recomputed) == canonical_dumps(frozen),
            "recomputed_results": recomputed,
        }
