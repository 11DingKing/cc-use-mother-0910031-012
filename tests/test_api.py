"""HTTP API 端到端测试。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from compliance_review.api import make_server


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = make_server("127.0.0.1", 0, ":memory:")
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_end_to_end_http(self) -> None:
        st, _ = self.call("POST", "/institutions", {"id": "INS-A", "name": "甲"})
        self.assertEqual(st, 201)
        self.call("POST", "/institutions", {"id": "INS-B", "name": "乙"})
        self.assertEqual(self.call("POST", "/reviewers", {"id": "RV-1", "name": "张"})[0], 201)
        self.assertEqual(
            self.call("POST", "/reviewers/RV-1/recusals",
                      {"institution_id": "INS-A", "reason": "亲属任职"})[0],
            201,
        )
        self.assertEqual(
            self.call("POST", "/rules",
                      {"code": "R1", "name": "错报", "deduction": 10,
                       "start_date": "2026-01-01"})[0],
            201,
        )
        self.assertEqual(
            self.call("POST", "/evidence",
                      {"institution_id": "INS-A", "rule_code": "R1",
                       "occurred_at": "2026-08-01", "period": "2026Q3"})[0],
            201,
        )
        self.assertEqual(
            self.call("POST", "/batches",
                      {"id": "B1", "period": "2026Q3", "name": "三季公示",
                       "cutoff_date": "2026-09-30"})[0],
            201,
        )
        # 试算不落版本
        st, trial = self.call("POST", "/batches/B1/trial", {"params": {"as_of": "2026-09-30"}})
        self.assertEqual(st, 200)
        a = next(r for r in trial["results"] if r["institution_id"] == "INS-A")
        self.assertEqual(a["score"], 90.0)
        self.assertEqual(self.call("GET", "/batches/B1/versions")[1], [])

        # 发布 v1
        self.assertEqual(
            self.call("POST", "/batches/B1/versions",
                      {"kind": "initial", "created_by": "RV-1", "reason": "首次公示"})[0],
            403,  # RV-1 回避 INS-A，批次级操作被拒
        )
        self.call("POST", "/reviewers", {"id": "RV-2", "name": "李"})
        self.assertEqual(
            self.call("POST", "/batches/B1/versions",
                      {"kind": "initial", "created_by": "RV-2", "reason": "首次公示"})[0],
            201,
        )
        st, v1 = self.call("POST", "/batches/B1/versions/1/publish",
                           {"published_by": "RV-2", "as_of": "2026-09-30"})
        self.assertEqual(st, 200)
        self.assertTrue(v1["digest"])

        # 申诉 + 补证产生 v2
        st, appeal = self.call("POST", "/appeals",
                               {"institution_id": "INS-A", "period": "2026Q3",
                                "batch_id": "B1", "reason": "整改已完成"})
        self.assertEqual(st, 201)
        # 补证需要先有整改核验才能提分；这里仅验证产生新版本与对比接口
        st, sup = self.call("POST", f"/appeals/{appeal['id']}/supplement",
                            {"note": "补充情况说明", "attachments": ["a.pdf"],
                             "reviewer_id": "RV-1"})
        self.assertEqual(st, 403)  # 回避
        st, sup = self.call("POST", f"/appeals/{appeal['id']}/supplement",
                            {"note": "补充情况说明", "attachments": ["a.pdf"],
                             "reviewer_id": "RV-2"})
        self.assertEqual(st, 201)
        self.assertEqual(sup["batch_version"]["version"], 2)

        st, cmp = self.call("GET", "/batches/B1/compare?left=1&right=2")
        self.assertEqual(st, 200)
        self.assertIn("changed", cmp)

        # 草稿版本不能复算
        self.assertEqual(self.call("POST", "/batches/B1/versions/2/recompute")[0], 409)


if __name__ == "__main__":
    unittest.main()
