"""HTTP API（标准库实现，零第三方依赖）。

路由总览：
  POST   /institutions                         登记机构
  GET    /institutions
  POST   /reviewers                            登记复核人
  POST   /reviewers/{rid}/recusals             登记回避关系
  POST   /rules                                新建评分规则（首版本）
  POST   /rules/{code}/errata                  发布规则勘误（新版本）
  GET    /rules[?code=]                        规则与生效区间
  POST   /evidence                             登记证据项
  GET    /evidence[?institution_id=]
  POST   /evidence/{eid}/remediations          提交整改
  POST   /remediations/{rid}/verify            核验整改（回避校验）
  GET    /remediations
  POST   /batches                              创建发布批次
  GET    /batches
  POST   /batches/{bid}/trial                  试算（与正式结果分离）
  POST   /batches/{bid}/versions               新建版本（申诉补证/勘误/撤销/重开）
  POST   /batches/{bid}/versions/{v}/publish   发布并冻结输入摘要
  POST   /batches/{bid}/versions/{v}/discard   放弃未发布草稿
  POST   /batches/{bid}/reopen                 批次重开（产生新版本）
  POST   /batches/{bid}/partial-revoke         部分撤销（产生新版本）
  GET    /batches/{bid}/versions[/{v}]         版本列表/详情（含冻结摘要）
  GET    /batches/{bid}/compare?left=&right=   原结果 vs 复核结果对比
  POST   /batches/{bid}/versions/{v}/recompute 按历史口径复算并校验
  POST   /appeals                              提出申诉
  GET    /appeals[?institution_id=]
  POST   /appeals/{aid}/supplement             申诉补证（产生新版本）
  POST   /appeals/{aid}/decision               申诉决定
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .errors import AppError
from .service import Service
from .store import Store


def _json_default(value):
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"不可序列化类型：{type(value)}")


class ApiHandler(BaseHTTPRequestHandler):
    service: Service = None  # 由 make_server 注入到类属性

    def log_message(self, fmt: str, *args) -> None:  # 静音默认日志
        return

    # ------------------------------------------------------------ 基础收发
    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            raise AppError("请求体必须是 JSON", code="invalid_json", status=400)
        if not isinstance(data, dict):
            raise AppError("请求体必须是 JSON 对象", code="invalid_json", status=400)
        return data

    def _send(self, status: int, body) -> None:
        encoded = json.dumps(body, ensure_ascii=False, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        try:
            status, body = self._route(method, path, query)
            self._send(status, body)
        except AppError as exc:
            self._send(exc.status, exc.to_dict())
        except Exception as exc:  # noqa: BLE001 - 兜底，避免线程崩溃
            self._send(500, {"error": "internal_error", "message": str(exc)})

    # ------------------------------------------------------------ 路由
    def _route(self, method: str, path: str, query: dict):
        s = self.service
        body = self._read_json() if method == "POST" else {}

        # 机构 / 复核人
        if path == "/institutions":
            if method == "POST":
                return 201, s.create_institution(body["id"], body["name"])
            if method == "GET":
                return 200, s.list_institutions()
        if path == "/reviewers" and method == "POST":
            return 201, s.create_reviewer(body["id"], body["name"])
        m = re.fullmatch(r"/reviewers/([^/]+)/recusals", path)
        if m and method == "POST":
            return 201, s.add_recusal(m.group(1), body["institution_id"], body["reason"])

        # 规则
        if path == "/rules":
            if method == "POST":
                return 201, s.create_rule(
                    body["code"], body["name"], float(body["deduction"]),
                    body["start_date"], kind=body.get("kind", "normal"),
                )
            if method == "GET":
                return 200, s.list_rule_versions(query.get("code", [None])[0])
        m = re.fullmatch(r"/rules/([^/]+)/errata", path)
        if m and method == "POST":
            return 201, s.issue_errata(
                m.group(1), body["name"], float(body["deduction"]),
                body["start_date"], body["reason"],
                retroactive=bool(body.get("retroactive", False)),
            )

        # 证据 / 整改
        if path == "/evidence":
            if method == "POST":
                return 201, s.add_evidence(
                    body["institution_id"], body["rule_code"],
                    body["occurred_at"], body["period"], body.get("detail"),
                )
            if method == "GET":
                return 200, s.list_evidence(query.get("institution_id", [None])[0])
        m = re.fullmatch(r"/evidence/([^/]+)/remediations", path)
        if m and method == "POST":
            return 201, s.submit_remediation(m.group(1), body.get("note", ""))
        if path == "/remediations" and method == "GET":
            return 200, s.list_remediations()
        m = re.fullmatch(r"/remediations/([^/]+)/verify", path)
        if m and method == "POST":
            return 200, s.verify_remediation(
                m.group(1), body["verifier_id"], body["effective_date"],
                body.get("note"),
            )

        # 批次
        if path == "/batches":
            if method == "POST":
                return 201, s.create_batch(
                    body["id"], body["period"], body["name"], body["cutoff_date"]
                )
            if method == "GET":
                return 200, s.list_batches()
        m = re.fullmatch(r"/batches/([^/]+)", path)
        if m and method == "GET":
            return 200, s._require_batch(m.group(1))
        bid: str | None = None
        mb = re.fullmatch(r"/batches/([^/]+)(/.*)?", path)
        if mb:
            bid = mb.group(1)
            sub = mb.group(2) or ""
            if sub == "/trial" and method == "POST":
                return 200, s.trial(bid, body.get("params"))
            if sub == "/versions" and method == "GET":
                return 200, s.list_versions(bid)
            if sub == "/versions" and method == "POST":
                return 201, s.create_version(
                    bid,
                    body["kind"], body["created_by"], body["reason"],
                    appeal_id=body.get("appeal_id"),
                    revoked_evidence_ids=body.get("revoked_evidence_ids"),
                    retroactive_codes=body.get("retroactive_codes"),
                    target_institution_id=body.get("target_institution_id"),
                )
            if sub == "/reopen" and method == "POST":
                return 201, s.reopen_batch(bid, body["reviewer_id"], body["reason"])
            if sub == "/partial-revoke" and method == "POST":
                return 201, s.partial_revoke(
                    bid, body["institution_id"], body["evidence_id"],
                    body["reason"], body["reviewer_id"],
                )
            if sub == "/compare" and method == "GET":
                left = int(query["left"][0]); right = int(query["right"][0])
                return 200, s.compare_versions(bid, left, right)
            mv = re.fullmatch(r"/versions/(\d+)", sub)
            if mv and method == "GET":
                return 200, s.get_version(bid, int(mv.group(1)))
            mv = re.fullmatch(r"/versions/(\d+)/publish", sub)
            if mv and method == "POST":
                return 200, s.publish_version(
                    bid, int(mv.group(1)), body["published_by"],
                    body.get("as_of"),
                )
            mv = re.fullmatch(r"/versions/(\d+)/discard", sub)
            if mv and method == "POST":
                return 200, s.discard_version(
                    bid, int(mv.group(1)), body["reviewer_id"]
                )
            mv = re.fullmatch(r"/versions/(\d+)/recompute", sub)
            if mv and method == "POST":
                return 200, s.recompute_version(bid, int(mv.group(1)))

        # 申诉
        if path == "/appeals":
            if method == "POST":
                return 201, s.create_appeal(
                    body["institution_id"], body["period"], body["batch_id"], body["reason"]
                )
            if method == "GET":
                return 200, s.list_appeals(query.get("institution_id", [None])[0])
        m = re.fullmatch(r"/appeals/([^/]+)/supplement", path)
        if m and method == "POST":
            return 201, s.supplement_appeal(
                m.group(1), body["note"], body.get("attachments", []),
                body["reviewer_id"],
            )
        m = re.fullmatch(r"/appeals/([^/]+)/decision", path)
        if m and method == "POST":
            return 200, s.decide_appeal(
                m.group(1), body["decider_id"], body["decision"], body.get("note", "")
            )

        raise AppError(f"未找到路由：{method} {path}", code="not_found", status=404)


def make_server(host: str, port: int, db_path: str) -> ThreadingHTTPServer:
    """构造带共享 Service（写操作串行化）的 HTTP 服务器。"""
    import threading

    if db_path != ":memory:":
        import os
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    service = Service(Store(db_path))
    lock = threading.RLock()

    class _Handler(ApiHandler):
        pass

    # 包装 service：所有调用持锁，避免并发写造成版本竞争。
    class _LockedService:
        def __getattr__(self, name):
            attr = getattr(service, name)
            if callable(attr):
                def wrapped(*args, **kwargs):
                    with lock:
                        return attr(*args, **kwargs)
                return wrapped
            return attr

    _Handler.service = _LockedService()
    server = ThreadingHTTPServer((host, port), _Handler)
    server.service = service  # type: ignore[attr-defined]
    return server


def main() -> None:
    import argparse
    import os

    parser = argparse.ArgumentParser(description="机构合规评分复核后端")
    parser.add_argument("--host", default=os.environ.get("COMPLIANCE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("COMPLIANCE_PORT", "8080")))
    parser.add_argument(
        "--db", default=os.environ.get("COMPLIANCE_DB", "data/compliance.db"),
        help="SQLite 路径（:memory: 仅用于测试）",
    )
    args = parser.parse_args()
    server = make_server(args.host, args.port, args.db)
    print(f"合规评分复核服务已启动：http://{args.host}:{args.port} (db={args.db})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
