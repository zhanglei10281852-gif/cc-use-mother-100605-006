"""业务规则验证：分级审批、重复申请、许可版本、出车决策、替代车辆、
冻结不篡改已完成记录、路线变更、资质撤回与出车解释。"""
import unittest
from datetime import timedelta

from helpers import BASE, approve_all, bootstrap, make_service, night_trip_times

from road_permits.domain import (
    ApplicationStatus,
    PermitStatus,
    RegistryStatus,
    TripStatus,
    iso,
)
from road_permits.services import ServiceError


class TieredApprovalTests(unittest.TestCase):
    def test_low_risk_segment_needs_single_level(self):
        service, _ = make_service()
        *_, app = bootstrap(service, risk="low")
        app = service.decide_application(app["id"], 1, "审核员", "approve", "材料齐全")
        self.assertEqual(app["status"], ApplicationStatus.APPROVED)
        permits = service.list_permits()
        self.assertEqual(len(permits), 1)
        self.assertEqual(permits[0]["version"], 1)
        self.assertEqual(permits[0]["status"], PermitStatus.ACTIVE)

    def test_high_risk_segment_requires_three_levels_in_order(self):
        service, _ = make_service()
        *_, app = bootstrap(service, risk="high")
        # 跳级被拒
        with self.assertRaises(ServiceError) as ctx:
            service.decide_application(app["id"], 2, "审批员乙", "approve", "跳级")
        self.assertEqual(ctx.exception.code, "invalid_state")
        # 逐级审批
        app = service.decide_application(app["id"], 1, "审批员甲", "approve", "一级通过")
        self.assertEqual(app["status"], ApplicationStatus.PENDING)
        app = service.decide_application(app["id"], 2, "审批员乙", "approve", "二级通过")
        self.assertEqual(app["status"], ApplicationStatus.PENDING)
        app = service.decide_application(app["id"], 3, "审批员丙", "approve", "三级通过")
        self.assertEqual(app["status"], ApplicationStatus.APPROVED)

    def test_same_approver_cannot_approve_twice(self):
        service, _ = make_service()
        *_, app = bootstrap(service, risk="medium")
        service.decide_application(app["id"], 1, "同一审批员", "approve", "一级通过")
        with self.assertRaises(ServiceError) as ctx:
            service.decide_application(app["id"], 2, "同一审批员", "approve", "二级")
        self.assertEqual(ctx.exception.code, "invalid_state")

    def test_rejection_is_terminal_and_decided_app_cannot_be_decided_again(self):
        service, _ = make_service()
        *_, app = bootstrap(service, risk="medium")
        app = service.decide_application(app["id"], 1, "审批员", "reject", "材料不实")
        self.assertEqual(app["status"], ApplicationStatus.REJECTED)
        with self.assertRaises(ServiceError):
            service.decide_application(app["id"], 1, "审批员", "approve", "复议")
        self.assertEqual(service.list_permits(), [])


class DuplicateApplicationTests(unittest.TestCase):
    def test_overlapping_pending_application_is_rejected(self):
        service, _ = make_service()
        ent, veh, drv, seg, app = bootstrap(service)
        with self.assertRaises(ServiceError) as ctx:
            service.submit_application(
                ent["id"], veh["id"], drv["id"], seg["id"],
                app["valid_from"], app["valid_to"], "22:00", "02:00", "重复提交",
            )
        self.assertEqual(ctx.exception.code, "duplicate")
        self.assertIn(app["id"], ctx.exception.message)

    def test_resubmission_allowed_after_rejection(self):
        service, _ = make_service()
        ent, veh, drv, seg, app = bootstrap(service)
        service.decide_application(app["id"], 1, "审批员", "reject", "驳回")
        again = service.submit_application(
            ent["id"], veh["id"], drv["id"], seg["id"],
            app["valid_from"], app["valid_to"], "22:00", "02:00", "驳回后重新提交",
        )
        self.assertEqual(again["status"], ApplicationStatus.PENDING)

    def test_non_overlapping_period_is_not_duplicate(self):
        service, clock = make_service()
        ent, veh, drv, seg, app = bootstrap(service, valid_days=10)
        later = service.submit_application(
            ent["id"], veh["id"], drv["id"], seg["id"],
            iso(BASE + timedelta(days=20)), iso(BASE + timedelta(days=30)),
            "22:00", "02:00", "下一周期申请",
        )
        self.assertEqual(later["status"], ApplicationStatus.PENDING)


class PermitVersionTests(unittest.TestCase):
    def test_renewal_supersedes_old_version_and_freezes_its_future_trips(self):
        service, _ = make_service()
        ent, veh, drv, seg, app1 = bootstrap(service)
        approve_all(service, app1, "medium")
        permit_v1 = service.list_permits()[0]

        start, end = night_trip_times()
        old_trip = service.schedule_trip(permit_v1["id"], drv["id"], start, end)
        self.assertEqual(old_trip["status"], TripStatus.APPROVED)

        # 续期申请（旧申请已审结，不构成重复）
        app2 = service.submit_application(
            ent["id"], veh["id"], drv["id"], seg["id"],
            app1["valid_from"], app1["valid_to"], "22:00", "02:00", "到期续期",
        )
        approve_all(service, app2, "medium")

        permits = {p["version"]: p for p in service.list_permits(vehicle_id=veh["id"])}
        self.assertEqual(permits[1]["status"], PermitStatus.SUPERSEDED)
        self.assertEqual(permits[2]["status"], PermitStatus.ACTIVE)
        # 旧版本上的未来出车被冻结，且可解释
        trip = service.store.get("trips", old_trip["id"])
        self.assertEqual(trip["status"], TripStatus.FROZEN)
        self.assertIn("版本更替", service.explain_trip(old_trip["id"])["summary"])


class TripDecisionTests(unittest.TestCase):
    def setUp(self):
        self.service, self.clock = make_service()
        ent, veh, drv, seg, app = bootstrap(self.service)
        approve_all(self.service, app, "medium")
        self.ent, self.veh, self.drv, self.seg = ent, veh, drv, seg
        self.permit = self.service.list_permits()[0]

    def test_approved_trip_records_full_check_list(self):
        start, end = night_trip_times()
        trip = self.service.schedule_trip(self.permit["id"], self.drv["id"], start, end)
        self.assertEqual(trip["status"], TripStatus.APPROVED)
        self.assertTrue(all(c["ok"] for c in trip["decision"]["checks"]))
        self.assertIn("获准", self.service.explain_trip(trip["id"])["summary"])

    def test_trip_outside_cross_day_window_is_denied_with_reason(self):
        start, end = night_trip_times(hour=12)  # 正午，不在 22:00-02:00 窗口内
        trip = self.service.schedule_trip(self.permit["id"], self.drv["id"], start, end)
        self.assertEqual(trip["status"], TripStatus.DENIED)
        self.assertTrue(any("窗口" in f for f in trip["decision"]["failures"]))
        summary = self.service.explain_trip(trip["id"])["summary"]
        self.assertIn("被拒", summary)
        self.assertIn("窗口", summary)

    def test_trip_beyond_permit_validity_is_denied(self):
        start = iso(BASE + timedelta(days=40))
        end = iso(BASE + timedelta(days=40, hours=1))
        trip = self.service.schedule_trip(self.permit["id"], self.drv["id"], start, end)
        self.assertEqual(trip["status"], TripStatus.DENIED)
        self.assertTrue(any("有效期" in f for f in trip["decision"]["failures"]))

    def test_expired_vehicle_qualification_is_denied(self):
        now = self.service.clock()
        stale = self.service.register_vehicle(self.ent["id"], "辽A·T0009",
                                              iso(now + timedelta(hours=2)))
        # 车辆资质在出车结束前到期
        start, end = night_trip_times()
        trip = self.service.schedule_trip(self.permit["id"], self.drv["id"], start, end,
                                          vehicle_id=stale["id"])
        self.assertEqual(trip["status"], TripStatus.DENIED)
        self.assertTrue(any("资质" in f or "替代" in f for f in trip["decision"]["failures"]))

    def test_expired_driver_authorization_is_denied(self):
        now = self.service.clock()
        stale_drv = self.service.register_driver(self.ent["id"], "安全员乙", "LIC-0002",
                                                 iso(now + timedelta(hours=2)))
        start, end = night_trip_times()
        trip = self.service.schedule_trip(self.permit["id"], stale_drv["id"], start, end)
        self.assertEqual(trip["status"], TripStatus.DENIED)
        self.assertTrue(any("授权" in f for f in trip["decision"]["failures"]))

    def test_completed_trip_is_terminal(self):
        start, end = night_trip_times()
        trip = self.service.schedule_trip(self.permit["id"], self.drv["id"], start, end)
        done = self.service.complete_trip(trip["id"])
        self.assertEqual(done["status"], TripStatus.COMPLETED)
        with self.assertRaises(ServiceError):
            self.service.complete_trip(trip["id"])


class SubstituteVehicleTests(unittest.TestCase):
    def setUp(self):
        self.service, _ = make_service()
        ent, veh, drv, seg, app = bootstrap(self.service)
        approve_all(self.service, app, "medium")
        self.ent, self.veh, self.drv, self.seg = ent, veh, drv, seg
        self.permit = self.service.list_permits(vehicle_id=veh["id"])[0]

    def _register_substitute(self, plate, with_permit=True):
        now = self.service.clock()
        sub = self.service.register_vehicle(self.ent["id"], plate,
                                            iso(now + timedelta(days=180)))
        if with_permit:
            app = self.service.submit_application(
                self.ent["id"], sub["id"], self.drv["id"], self.seg["id"],
                iso(now + timedelta(hours=1)), iso(now + timedelta(days=30)),
                "22:00", "02:00", "替代车辆备案",
            )
            approve_all(self.service, app, "medium")
        return sub

    def test_substitute_with_covering_permit_is_approved(self):
        sub = self._register_substitute("辽A·T0002")
        start, end = night_trip_times()
        trip = self.service.schedule_trip(self.permit["id"], self.drv["id"], start, end,
                                          vehicle_id=sub["id"])
        self.assertEqual(trip["status"], TripStatus.APPROVED)
        self.assertEqual(trip["decision"]["substitute_for"], self.veh["id"])
        coverage = next(c for c in trip["decision"]["checks"]
                        if c["code"] == "substitute_coverage")
        self.assertTrue(coverage["ok"])
        self.assertIn("覆盖许可", coverage["message"])

    def test_substitute_without_covering_permit_is_denied(self):
        sub = self._register_substitute("辽A·T0003", with_permit=False)
        start, end = night_trip_times()
        trip = self.service.schedule_trip(self.permit["id"], self.drv["id"], start, end,
                                          vehicle_id=sub["id"])
        self.assertEqual(trip["status"], TripStatus.DENIED)
        self.assertTrue(any("替代" in f for f in trip["decision"]["failures"]))


class FreezeTests(unittest.TestCase):
    def setUp(self):
        self.service, _ = make_service()
        ent, veh, drv, seg, app = bootstrap(self.service)
        approve_all(self.service, app, "medium")
        self.ent, self.veh, self.drv, self.seg = ent, veh, drv, seg
        self.permit = self.service.list_permits()[0]

    def _approved_trip(self, hour=23):
        start, end = night_trip_times(hour=hour)
        trip = self.service.schedule_trip(self.permit["id"], self.drv["id"], start, end)
        assert trip["status"] == TripStatus.APPROVED
        return trip

    def test_incident_freezes_future_trips_but_keeps_completed_records(self):
        future = self._approved_trip()
        done = self.service.complete_trip(self._approved_trip(hour=22)["id"])

        incident = self.service.report_incident(self.veh["id"], "路口急刹致追尾", reporter="路测员")
        self.assertIn(future["id"], incident["frozen_trips"])
        self.assertIn(self.permit["id"], incident["frozen_permits"])

        # 已完成记录不被篡改
        kept = self.service.store.get("trips", done["id"])
        self.assertEqual(kept["status"], TripStatus.COMPLETED)
        self.assertIsNotNone(kept["completed_at"])
        # 许可冻结期间新的出车申请被拒
        start, end = night_trip_times(hour=23)
        denied = self.service.schedule_trip(self.permit["id"], self.drv["id"], start, end)
        self.assertEqual(denied["status"], TripStatus.DENIED)
        self.assertTrue(any("frozen" in f for f in denied["decision"]["failures"]))
        # 冻结原因可解释
        summary = self.service.explain_trip(future["id"])["summary"]
        self.assertIn("冻结", summary)
        self.assertIn("事故报告", summary)

    def test_resolve_incident_restores_permit_but_not_frozen_trips(self):
        trip = self._approved_trip()
        incident = self.service.report_incident(self.veh["id"], "轻微剐蹭")
        resolved = self.service.resolve_incident(incident["id"], "调查完毕，恢复测试")
        self.assertIn(self.permit["id"], resolved["restored_permits"])
        self.assertEqual(self.service.list_permits()[0]["status"], PermitStatus.ACTIVE)
        # 已冻结的出车安排保持冻结留档，不自动复活
        self.assertEqual(self.service.store.get("trips", trip["id"])["status"],
                         TripStatus.FROZEN)

    def test_route_change_supersedes_permit_and_requires_reapproval(self):
        trip = self._approved_trip()
        new_seg = self.service.register_segment("新开拓路段", "high")
        result = self.service.change_route(self.permit["id"], new_seg["id"], "原路段施工")

        self.assertEqual(self.service.list_permits()[0]["status"], PermitStatus.SUPERSEDED)
        self.assertIn(trip["id"], result["frozen_trips"])
        self.assertEqual(self.service.store.get("trips", trip["id"])["status"],
                         TripStatus.FROZEN)
        # 新路段申请需重新走分级审批（高风险三级）
        new_app = result["new_application"]
        self.assertEqual(new_app["segment_id"], new_seg["id"])
        approve_all(self.service, new_app, "high")
        new_permit = self.service.list_permits(vehicle_id=self.veh["id"],
                                               status=PermitStatus.ACTIVE)[0]
        self.assertEqual(new_permit["segment_id"], new_seg["id"])
        self.assertEqual(new_permit["version"], 1)

    def test_qualification_revocation_is_terminal(self):
        trip = self._approved_trip()
        result = self.service.revoke_vehicle_qualification(self.veh["id"], "年检不合格")
        self.assertIn(self.permit["id"], result["revoked_permits"])
        self.assertIn(trip["id"], result["frozen_trips"])
        self.assertEqual(self.service.list_permits()[0]["status"], PermitStatus.REVOKED)
        self.assertEqual(self.service.store.get("vehicles", self.veh["id"])["status"],
                         RegistryStatus.REVOKED)
        start, end = night_trip_times()
        denied = self.service.schedule_trip(self.permit["id"], self.drv["id"], start, end)
        self.assertEqual(denied["status"], TripStatus.DENIED)

    def test_driver_revocation_freezes_their_trips(self):
        trip = self._approved_trip()
        result = self.service.revoke_driver(self.drv["id"], "授权证书吊销")
        self.assertIn(trip["id"], result["frozen_trips"])


class ExplainTests(unittest.TestCase):
    def test_explain_contains_decision_checks_and_audit_events(self):
        service, _ = make_service()
        ent, veh, drv, seg, app = bootstrap(service)
        approve_all(service, app, "medium")
        permit = service.list_permits()[0]
        start, end = night_trip_times()
        trip = service.schedule_trip(permit["id"], drv["id"], start, end)

        report = service.explain_trip(trip["id"])
        self.assertEqual(report["trip"]["id"], trip["id"])
        self.assertEqual(report["permit"]["id"], permit["id"])
        self.assertEqual(report["decision"]["outcome"], TripStatus.APPROVED)
        self.assertGreaterEqual(len(report["decision"]["checks"]), 6)
        actions = [e["action"] for e in report["events"]]
        self.assertIn("permit.issued", actions)
        self.assertIn("trip.approved", actions)

    def test_explain_unknown_trip_raises_not_found(self):
        service, _ = make_service()
        with self.assertRaises(ServiceError) as ctx:
            service.explain_trip("TRP-9999")
        self.assertEqual(ctx.exception.code, "not_found")


if __name__ == "__main__":
    unittest.main()
