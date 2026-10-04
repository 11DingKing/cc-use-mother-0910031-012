"""基于标准库的 JSON HTTP API。

运行：PYTHONPATH=src python3 -m score_review.api  （或 tools/run_server.py）
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from .errors import DomainError, NotFoundError, ValidationError
from .services import ScoreReviewService


def _require(body: dict, *fields: str) -> None:
    missing = [f for f in fields if body.get(f) in (None, "")]
    if missing:
        raise ValidationError("缺少必填字段：" + "、".join(missing))


class Api:
    """路由分发：把 HTTP 调用映射到 ScoreReviewService。"""

    def __init__(self, service: ScoreReviewService) -> None:
        self.service = service
        self.routes: list[tuple[str, re.Pattern, object]] = []
        add = self._add
        add("POST", "/api/rules", self.create_rule)
        add("GET", "/api/rules", self.list_rules)
        add("GET", "/api/rules/{code}/versions", self.rule_versions)
        add("POST", "/api/rules/{code}/errata", self.errata_rule)
        add("POST", "/api/evidence", self.register_evidence)
        add("GET", "/api/evidence", self.list_evidence)
        add("GET", "/api/evidence/{eid}", self.get_evidence)
        add("POST", "/api/evidence/{eid}/verify", self.verify_evidence)
        add("POST", "/api/rectifications", self.submit_rectification)
        add("POST", "/api/rectifications/{rid}/verify", self.verify_rectification)
        add("POST", "/api/batches", self.create_batch)
        add("GET", "/api/batches/{bid}", self.get_batch)
        add("POST", "/api/batches/{bid}/publish", self.publish_batch)
        add("POST", "/api/batches/{bid}/reopen", self.reopen_batch)
        add("GET", "/api/batches/{bid}/results", self.batch_results)
        add("GET", "/api/batches/{bid}/results/{org}", self.batch_org_result)
        add("GET", "/api/batches/{bid}/digest-check", self.digest_check)
        add("POST", "/api/trial-runs", self.trial_run)
        add("POST", "/api/recalculate", self.recalculate)
        add("GET", "/api/runs/{rid}", self.get_run)
        add("POST", "/api/appeals", self.file_appeal)
        add("GET", "/api/appeals/{aid}", self.get_appeal)
        add("POST", "/api/appeals/{aid}/supplement", self.supplement_appeal)
        add("POST", "/api/appeals/{aid}/assign", self.assign_reviewer)
        add("POST", "/api/appeals/{aid}/decide", self.decide_appeal)
        add("POST", "/api/appeals/{aid}/archive", self.archive_appeal)
        add("GET", "/api/appeals/{aid}/comparison", self.appeal_comparison)
        add("POST", "/api/recusals", self.register_recusal)
        add("GET", "/api/recusals", self.list_recusals)

    def _add(self, method: str, pattern: str, handler) -> None:
        regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")
        self.routes.append((method, regex, handler))

    def dispatch(self, method: str, path: str, query: dict, body: dict) -> tuple[int, object]:
        for route_method, regex, handler in self.routes:
            if route_method != method:
                continue
            match = regex.match(path)
            if match:
                return handler(match.groupdict(), query, body)
        raise NotFoundError(f"接口不存在：{method} {path}")

    # -- 评分规则 ------------------------------------------------------

    def create_rule(self, p, q, b):
        _require(b, "code", "name", "category", "deduction", "effective_from", "by")
        entity = self.service.create_rule(
            code=b["code"], name=b["name"], category=b["category"], deduction=b["deduction"],
            effective_from=b["effective_from"], effective_to=b.get("effective_to"), by=b["by"],
        )
        return 201, entity

    def list_rules(self, p, q, b):
        if q.get("as_of"):
            return 200, self.service.rules_as_of(q["as_of"])
        return 200, self.service.list_rules()

    def rule_versions(self, p, q, b):
        return 200, self.service.rule_versions(p["code"])

    def errata_rule(self, p, q, b):
        _require(b, "effective_from", "reason", "by")
        return 200, self.service.errata_rule(
            p["code"], effective_from=b["effective_from"], reason=b["reason"], by=b["by"],
            deduction=b.get("deduction"), name=b.get("name"), category=b.get("category"),
            effective_to=b.get("effective_to"),
        )

    # -- 证据与整改 ----------------------------------------------------

    def register_evidence(self, p, q, b):
        _require(b, "org_id", "rule_code", "period", "description", "by")
        return 201, self.service.register_evidence(
            org_id=b["org_id"], rule_code=b["rule_code"], period=b["period"],
            description=b["description"], by=b["by"],
        )

    def list_evidence(self, p, q, b):
        return 200, self.service.list_evidence(org_id=q.get("org_id"), period=q.get("period"))

    def get_evidence(self, p, q, b):
        return 200, self.service.get_evidence(p["eid"])

    def verify_evidence(self, p, q, b):
        _require(b, "approved", "by")
        return 200, self.service.verify_evidence(p["eid"], approved=bool(b["approved"]), by=b["by"])

    def submit_rectification(self, p, q, b):
        _require(b, "evidence_id", "org_id", "description", "by")
        return 201, self.service.submit_rectification(
            evidence_id=b["evidence_id"], org_id=b["org_id"], description=b["description"], by=b["by"],
        )

    def verify_rectification(self, p, q, b):
        _require(b, "passed", "by")
        return 200, self.service.verify_rectification(
            p["rid"], passed=bool(b["passed"]), note=b.get("note", ""), by=b["by"],
        )

    # -- 发布批次 ------------------------------------------------------

    def create_batch(self, p, q, b):
        _require(b, "period", "as_of", "by")
        return 201, self.service.create_batch(period=b["period"], as_of=b["as_of"], by=b["by"])

    def get_batch(self, p, q, b):
        return 200, self.service.get_batch(p["bid"])

    def publish_batch(self, p, q, b):
        _require(b, "by")
        return 200, self.service.publish_batch(p["bid"], by=b["by"])

    def reopen_batch(self, p, q, b):
        _require(b, "reason", "by")
        return 200, self.service.reopen_batch(p["bid"], reason=b["reason"], by=b["by"])

    def batch_results(self, p, q, b):
        return 200, self.service.batch_results(p["bid"])

    def batch_org_result(self, p, q, b):
        return 200, self.service.batch_org_result(p["bid"], p["org"])

    def digest_check(self, p, q, b):
        return 200, self.service.digest_check(p["bid"])

    # -- 试算与历史复算 --------------------------------------------------

    def trial_run(self, p, q, b):
        _require(b, "period", "by")
        return 201, self.service.trial_run(
            period=b["period"], as_of=b.get("as_of"), org_id=b.get("org_id"), by=b["by"],
        )

    def recalculate(self, p, q, b):
        _require(b, "period", "as_of", "by")
        return 201, self.service.recalculate_as_of(
            period=b["period"], as_of=b["as_of"], org_id=b.get("org_id"), by=b["by"],
        )

    def get_run(self, p, q, b):
        return 200, self.service.get_run(p["rid"])

    # -- 申诉与复核 ----------------------------------------------------

    def file_appeal(self, p, q, b):
        _require(b, "batch_id", "org_id", "deduction_item_ids", "reason", "by")
        return 201, self.service.file_appeal(
            batch_id=b["batch_id"], org_id=b["org_id"],
            deduction_item_ids=b["deduction_item_ids"], reason=b["reason"], by=b["by"],
        )

    def get_appeal(self, p, q, b):
        return 200, self.service.get_appeal(p["aid"])

    def supplement_appeal(self, p, q, b):
        _require(b, "by")
        return 200, self.service.supplement_appeal(
            p["aid"], by=b["by"], evidence_ids=b.get("evidence_ids"),
            new_evidence=b.get("new_evidence"), note=b.get("note", ""),
        )

    def assign_reviewer(self, p, q, b):
        _require(b, "reviewer_id", "by")
        return 200, self.service.assign_reviewer(p["aid"], reviewer_id=b["reviewer_id"], by=b["by"])

    def decide_appeal(self, p, q, b):
        _require(b, "revoked_item_ids", "comment", "by")
        return 200, self.service.decide_appeal(
            p["aid"], revoked_item_ids=b["revoked_item_ids"], comment=b["comment"], by=b["by"],
        )

    def archive_appeal(self, p, q, b):
        _require(b, "by")
        return 200, self.service.archive_appeal(p["aid"], by=b["by"])

    def appeal_comparison(self, p, q, b):
        return 200, self.service.appeal_comparison(p["aid"])

    # -- 回避登记 ------------------------------------------------------

    def register_recusal(self, p, q, b):
        _require(b, "reviewer_id", "org_id", "reason", "by")
        return 201, self.service.register_recusal(
            reviewer_id=b["reviewer_id"], org_id=b["org_id"], reason=b["reason"], by=b["by"],
        )

    def list_recusals(self, p, q, b):
        return 200, self.service.list_recusals(reviewer_id=q.get("reviewer_id"))


class _RequestHandler(BaseHTTPRequestHandler):
    server_version = "ScoreReview/0.1"

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = None
        if not isinstance(body, dict):
            status, payload = 400, {"error": {
                "code": "bad_json", "message": "请求体必须是 JSON 对象", "details": {}}}
        else:
            try:
                status, payload = self.server.api.dispatch(method, path, query, body)  # type: ignore[attr-defined]
            except DomainError as exc:
                status, payload = exc.status, {"error": exc.to_dict()}
            except Exception as exc:  # noqa: BLE001 - 兜底，保证服务不中断
                status, payload = 500, {"error": {
                    "code": "internal", "message": str(exc), "details": {}}}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def log_message(self, *args) -> None:  # 保持测试输出干净
        pass


def make_server(service: ScoreReviewService | None = None, host: str = "127.0.0.1", port: int = 8000):
    server = ThreadingHTTPServer((host, port), _RequestHandler)
    server.api = Api(service or ScoreReviewService())  # type: ignore[attr-defined]
    return server


def main() -> None:  # pragma: no cover
    server = make_server()
    print("评分复核服务已启动：http://127.0.0.1:8000")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
