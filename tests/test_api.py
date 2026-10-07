"""HTTP API 端到端验证：注册 → 申请 → 分级审批 → 出车 → 解释，以及错误码。"""
import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import timedelta

from helpers import BASE, FakeClock

from road_permits.api import make_server
from road_permits.domain import iso
from road_permits.services import LedgerService
from road_permits.store import Ledger


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = LedgerService(Ledger(":memory:"), clock=FakeClock())
        cls.server = make_server(cls.service, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_flow_and_error_codes(self):
        status, health = self.call("GET", "/health")
        self.assertEqual((status, health["status"]), (200, "ok"))

        future = iso(BASE + timedelta(days=180))
        _, ent = self.call("POST", "/registry/enterprises",
                           {"name": "API 测试企业", "credit_code": "CREDIT-API-1"})
        _, veh = self.call("POST", "/registry/vehicles",
                           {"enterprise_id": ent["id"], "plate": "辽A·API001",
                            "qualification_expires_at": future})
        _, drv = self.call("POST", "/registry/drivers",
                           {"enterprise_id": ent["id"], "name": "安全员",
                            "license_no": "LIC-API-1", "authorization_expires_at": future})
        _, seg = self.call("POST", "/registry/segments",
                           {"name": "API 测试路段", "risk_level": "medium"})

        app_body = {"enterprise_id": ent["id"], "vehicle_id": veh["id"],
                    "driver_id": drv["id"], "segment_id": seg["id"],
                    "valid_from": iso(BASE + timedelta(hours=1)),
                    "valid_to": iso(BASE + timedelta(days=30)),
                    "window_start": "22:00", "window_end": "02:00",
                    "purpose": "API 全流程验证"}
        status, app = self.call("POST", "/applications", app_body)
        self.assertEqual(status, 200)

        # 重复申请 → 409
        status, dup = self.call("POST", "/applications", app_body)
        self.assertEqual(status, 409)
        self.assertEqual(dup["error"], "duplicate")

        # 查看申请：中风险需两级审批
        status, detail = self.call("GET", f"/applications/{app['id']}")
        self.assertEqual(detail["required_levels"], [1, 2])

        # 跳级审批 → 409
        status, skip = self.call("POST", f"/applications/{app['id']}/decisions",
                                 {"level": 2, "approver": "乙", "decision": "approve",
                                  "reason": "跳级"})
        self.assertEqual(status, 409)

        self.call("POST", f"/applications/{app['id']}/decisions",
                  {"level": 1, "approver": "甲", "decision": "approve", "reason": "一级通过"})
        self.call("POST", f"/applications/{app['id']}/decisions",
                  {"level": 2, "approver": "乙", "decision": "approve", "reason": "二级通过"})

        status, permits = self.call("GET", f"/permits?vehicle_id={veh['id']}&status=active")
        self.assertEqual(len(permits), 1)
        permit = permits[0]

        # 窗口内出车 → 获准
        start = (BASE + timedelta(days=1)).replace(hour=23, minute=0, second=0, microsecond=0)
        status, trip = self.call("POST", "/trips",
                                 {"permit_id": permit["id"], "driver_id": drv["id"],
                                  "planned_start": iso(start),
                                  "planned_end": iso(start + timedelta(hours=2))})
        self.assertEqual(trip["status"], "approved")

        # 解释接口：获准理由完整
        status, report = self.call("GET", f"/trips/{trip['id']}/explain")
        self.assertEqual(status, 200)
        self.assertIn("获准", report["summary"])
        self.assertEqual(report["decision"]["outcome"], "approved")

        # 窗口外出车 → 被拒，同样可解释
        status, denied = self.call("POST", "/trips",
                                   {"permit_id": permit["id"], "driver_id": drv["id"],
                                    "planned_start": iso(start + timedelta(hours=13)),
                                    "planned_end": iso(start + timedelta(hours=14))})
        self.assertEqual(denied["status"], "denied")
        _, report = self.call("GET", f"/trips/{denied['id']}/explain")
        self.assertIn("被拒", report["summary"])

        # 事故报告 → 许可与未来出车冻结
        status, incident = self.call("POST", "/incidents",
                                     {"vehicle_id": veh["id"], "description": "传感器异常"})
        self.assertIn(permit["id"], incident["frozen_permits"])
        _, permits = self.call("GET", f"/permits?vehicle_id={veh['id']}")
        self.assertEqual(permits[0]["status"], "frozen")

        # 不存在的出车 → 404；未知路由 → 404
        status, _ = self.call("GET", "/trips/TRP-9999/explain")
        self.assertEqual(status, 404)
        status, _ = self.call("GET", "/no-such-route")
        self.assertEqual(status, 404)

        # 审计事件可查询
        status, events = self.call("GET", f"/events?entity_type=trip&entity_id={trip['id']}")
        self.assertTrue(any(e["action"] == "trip.approved" for e in events))


if __name__ == "__main__":
    unittest.main()
