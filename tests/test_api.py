"""HTTP API 测试：真实起服务，走完整申诉复核链路。"""
from __future__ import annotations

import http.client
import json
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from score_review import ScoreReviewService  # noqa: E402
from score_review.api import make_server  # noqa: E402


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = make_server(ScoreReviewService(), port=0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request(
            method, path,
            body=json.dumps(body) if body is not None else None,
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        payload = json.loads(resp.read())
        conn.close()
        return resp.status, payload

    def test_full_flow_over_http(self) -> None:
        # 规则与勘误
        status, _ = self.call("POST", "/api/rules", {
            "code": "DATA-ERROR", "name": "数据差错", "category": "报送质量",
            "deduction": 10, "effective_from": "2026-01-01", "by": "监管员-甲"})
        self.assertEqual(status, 201)

        # 证据 → 核验 → 扣分项
        status, ev = self.call("POST", "/api/evidence", {
            "org_id": "ORG-001", "rule_code": "DATA-ERROR", "period": "2026-Q3",
            "description": "数据差错", "by": "监管员-乙"})
        self.assertEqual(status, 201)
        status, ev = self.call("POST", f"/api/evidence/{ev['id']}/verify",
                               {"approved": True, "by": "监管员-丙"})
        self.assertEqual(ev["data"]["status"], "已核验")

        # 批次发布，冻结摘要
        status, batch = self.call("POST", "/api/batches", {
            "period": "2026-Q3", "as_of": "2026-09-30", "by": "监管员-丙"})
        batch_id = batch["id"]
        status, published = self.call("POST", f"/api/batches/{batch_id}/publish",
                                      {"by": "监管员-丙"})
        self.assertEqual(published["data"]["status"], "已发布")
        self.assertTrue(published["data"]["frozen_input_digest"])

        status, results = self.call("GET", f"/api/batches/{batch_id}/results/ORG-001")
        self.assertEqual(results["score"], 90)
        item_id = results["entries"][0]["item_id"]

        # 申诉 → 补证 → 回避拦截 → 指派 → 决定
        status, appeal = self.call("POST", "/api/appeals", {
            "batch_id": batch_id, "org_id": "ORG-001", "deduction_item_ids": [item_id],
            "reason": "整改已完成仍被旧扣分影响", "by": "合规员-001"})
        self.assertEqual(status, 201)
        appeal_id = appeal["id"]

        status, supplemented = self.call("POST", f"/api/appeals/{appeal_id}/supplement", {
            "by": "合规员-001",
            "new_evidence": {"rule_code": "DATA-ERROR", "description": "整改完成证明"}})
        self.assertEqual(supplemented["version"], 2)

        self.call("POST", "/api/recusals", {
            "reviewer_id": "专家-甲", "org_id": "ORG-001",
            "reason": "曾任该机构顾问", "by": "监管员-丙"})
        status, err = self.call("POST", f"/api/appeals/{appeal_id}/assign",
                                {"reviewer_id": "专家-甲", "by": "监管员-丙"})
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "recusal_conflict")

        status, _ = self.call("POST", f"/api/appeals/{appeal_id}/assign",
                              {"reviewer_id": "专家-乙", "by": "监管员-丙"})
        self.assertEqual(status, 200)
        status, decided = self.call("POST", f"/api/appeals/{appeal_id}/decide", {
            "revoked_item_ids": [item_id], "comment": "整改属实", "by": "专家-乙"})
        self.assertEqual(decided["data"]["status"], "已决定")

        # 对比原结果与复核结果
        status, comparison = self.call("GET", f"/api/appeals/{appeal_id}/comparison")
        self.assertEqual(comparison["original"]["score"], 90)
        self.assertEqual(comparison["review"]["score"], 100)
        self.assertEqual(comparison["delta"], 10)

        # 勘误后按历史口径复算
        self.call("POST", "/api/rules/DATA-ERROR/errata", {
            "effective_from": "2026-10-01", "deduction": 8,
            "reason": "扣分标准勘误", "by": "监管员-甲"})
        status, recalc = self.call("POST", "/api/recalculate", {
            "period": "2026-Q3", "as_of": "2026-09-30", "by": "监管员-丙"})
        self.assertEqual(status, 201)
        used = {r["code"]: r for r in recalc["data"]["rules_used"]}
        self.assertEqual(used["DATA-ERROR"]["deduction"], 10)

        # 试算不进批次
        status, trial = self.call("POST", "/api/trial-runs",
                                  {"period": "2026-Q3", "by": "监管员-丙"})
        self.assertEqual(trial["data"]["kind"], "试算")
        status, batch_view = self.call("GET", f"/api/batches/{batch_id}")
        self.assertEqual(len(batch_view["data"]["published_run_ids"]), 1)

        # 重开 → 重新发布 → 排名更新
        status, _ = self.call("POST", f"/api/batches/{batch_id}/reopen",
                              {"reason": "复核决定生效", "by": "监管员-丙"})
        self.assertEqual(status, 200)
        status, _ = self.call("POST", f"/api/batches/{batch_id}/publish", {"by": "监管员-丙"})
        status, results = self.call("GET", f"/api/batches/{batch_id}/results/ORG-001")
        self.assertEqual(results["score"], 100)

    def test_error_shapes(self) -> None:
        status, err = self.call("GET", "/api/nope")
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "not_found")

        status, err = self.call("POST", "/api/rules", {"code": "X"})
        self.assertEqual(status, 422)
        self.assertEqual(err["error"]["code"], "validation")

        status, err = self.call("GET", "/api/batches/B-999999")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
