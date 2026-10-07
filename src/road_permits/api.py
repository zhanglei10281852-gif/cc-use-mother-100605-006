"""HTTP API 层：基于标准库 http.server，无第三方依赖。

路由集中在 ``Api.handle``，可脱离网络直接测试；``make_server`` 返回
ThreadingHTTPServer 供部署与端到端测试使用。
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .services import LedgerService, ServiceError

HandlerFn = Callable[[dict[str, str], dict[str, Any]], Any]


class Api:
    """把 HTTP 请求映射到 LedgerService。返回值统一为 (status, body)。"""

    def __init__(self, service: LedgerService):
        self.service = service
        self._routes: list[tuple[str, re.Pattern[str], HandlerFn]] = []
        self._register_routes()

    def _route(self, method: str, pattern: str):
        def decorator(fn: HandlerFn) -> HandlerFn:
            self._routes.append((method, re.compile(f"^{pattern}$"), fn))
            return fn
        return decorator

    def _register_routes(self) -> None:
        svc = self.service

        @self._route("GET", r"/health")
        def health(params, body):
            return {"status": "ok"}

        @self._route("POST", r"/registry/enterprises")
        def register_enterprise(params, body):
            return svc.register_enterprise(body.get("name"), body.get("credit_code"),
                                           actor=body.get("actor", "api"))

        @self._route("POST", r"/registry/vehicles")
        def register_vehicle(params, body):
            return svc.register_vehicle(body.get("enterprise_id"), body.get("plate"),
                                        body.get("qualification_expires_at"),
                                        actor=body.get("actor", "api"))

        @self._route("POST", r"/registry/drivers")
        def register_driver(params, body):
            return svc.register_driver(body.get("enterprise_id"), body.get("name"),
                                       body.get("license_no"),
                                       body.get("authorization_expires_at"),
                                       actor=body.get("actor", "api"))

        @self._route("POST", r"/registry/segments")
        def register_segment(params, body):
            return svc.register_segment(body.get("name"), body.get("risk_level"),
                                        actor=body.get("actor", "api"))

        @self._route("POST", r"/applications")
        def submit_application(params, body):
            return svc.submit_application(
                body.get("enterprise_id"), body.get("vehicle_id"), body.get("driver_id"),
                body.get("segment_id"), body.get("valid_from"), body.get("valid_to"),
                body.get("window_start"), body.get("window_end"), body.get("purpose"),
                actor=body.get("actor", "api"),
            )

        @self._route("GET", r"/applications/(?P<app_id>[^/]+)")
        def get_application(params, body):
            return svc.get_application(params["app_id"])

        @self._route("POST", r"/applications/(?P<app_id>[^/]+)/decisions")
        def decide_application(params, body):
            return svc.decide_application(
                params["app_id"], int(body.get("level")), body.get("approver"),
                body.get("decision"), body.get("reason"), actor=body.get("actor", "api"),
            )

        @self._route("GET", r"/permits")
        def list_permits(params, body):
            return svc.list_permits(vehicle_id=params.get("vehicle_id"),
                                    status=params.get("status"))

        @self._route("POST", r"/permits/(?P<permit_id>[^/]+)/route-changes")
        def change_route(params, body):
            return svc.change_route(params["permit_id"], body.get("new_segment_id"),
                                    body.get("reason"), actor=body.get("actor", "api"))

        @self._route("POST", r"/trips")
        def schedule_trip(params, body):
            return svc.schedule_trip(
                body.get("permit_id"), body.get("driver_id"), body.get("planned_start"),
                body.get("planned_end"), vehicle_id=body.get("vehicle_id"),
                operator=body.get("operator", "dispatcher"),
            )

        @self._route("GET", r"/trips")
        def list_trips(params, body):
            return svc.list_trips(status=params.get("status"),
                                  vehicle_id=params.get("vehicle_id"))

        @self._route("POST", r"/trips/(?P<trip_id>[^/]+)/completion")
        def complete_trip(params, body):
            return svc.complete_trip(params["trip_id"], actor=body.get("actor", "api"))

        @self._route("GET", r"/trips/(?P<trip_id>[^/]+)/explain")
        def explain_trip(params, body):
            return svc.explain_trip(params["trip_id"])

        @self._route("POST", r"/incidents")
        def report_incident(params, body):
            return svc.report_incident(body.get("vehicle_id"), body.get("description"),
                                       trip_id=body.get("trip_id"),
                                       reporter=body.get("reporter", "supervisor"))

        @self._route("POST", r"/incidents/(?P<incident_id>[^/]+)/resolution")
        def resolve_incident(params, body):
            return svc.resolve_incident(params["incident_id"], body.get("reason"),
                                        actor=body.get("actor", "api"))

        @self._route("POST", r"/vehicles/(?P<vehicle_id>[^/]+)/qualification-revocation")
        def revoke_vehicle(params, body):
            return svc.revoke_vehicle_qualification(
                params["vehicle_id"], body.get("reason"), actor=body.get("actor", "regulator")
            )

        @self._route("POST", r"/drivers/(?P<driver_id>[^/]+)/revocation")
        def revoke_driver(params, body):
            return svc.revoke_driver(params["driver_id"], body.get("reason"),
                                     actor=body.get("actor", "regulator"))

        @self._route("POST", r"/admin/sweeps")
        def sweep(params, body):
            return svc.sweep_expired(actor=body.get("actor", "api"))

        @self._route("GET", r"/events")
        def list_events(params, body):
            return svc.list_events(entity_type=params.get("entity_type"),
                                   entity_id=params.get("entity_id"))

    def handle(self, method: str, path: str, query: dict[str, str],
               body: dict[str, Any]) -> tuple[int, Any]:
        for route_method, pattern, fn in self._routes:
            if route_method != method:
                continue
            match = pattern.match(path)
            if match:
                try:
                    result = fn({**match.groupdict(), **query}, body or {})
                    return 200, result
                except ServiceError as exc:
                    return exc.http_status, {"error": exc.code, "message": exc.message}
                except (ValueError, TypeError, KeyError) as exc:
                    return 400, {"error": "validation", "message": str(exc)}
        return 404, {"error": "not_found", "message": f"路由不存在：{method} {path}"}


def make_handler(api: Api) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _dispatch(self, method: str) -> None:
            from urllib.parse import parse_qsl, urlparse

            parsed = urlparse(self.path)
            query = dict(parse_qsl(parsed.query))
            body: dict[str, Any] = {}
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                raw = self.rfile.read(length)
                try:
                    body = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    self._respond(400, {"error": "validation", "message": "请求体不是合法 JSON"})
                    return
            status, payload = api.handle(method, parsed.path, query, body)
            self._respond(status, payload)

        def _respond(self, status: int, payload: Any) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def log_message(self, fmt: str, *args: Any) -> None:
            pass

    return Handler


def make_server(service: LedgerService, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    api = Api(service)
    return ThreadingHTTPServer((host, port), make_handler(api))


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8080) -> None:
    """启动服务：先执行到期清理，再监听请求。"""
    from .store import Ledger

    service = LedgerService(Ledger(db_path))
    summary = service.startup()
    cleaned = sum(len(v) for v in summary.values())
    print(f"到期清理完成，共处理 {cleaned} 条记录；服务监听 http://{host}:{port}")
    make_server(service, host, port).serve_forever()
