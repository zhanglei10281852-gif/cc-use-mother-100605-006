"""服务重启后的到期清理：过期许可/出车/申请/资质在重启时被归档，且幂等。"""
import os
import tempfile
import unittest
from datetime import timedelta

from helpers import BASE, FakeClock, approve_all, bootstrap

from road_permits.domain import (
    ApplicationStatus,
    PermitStatus,
    RegistryStatus,
    TripStatus,
    iso,
)
from road_permits.services import LedgerService
from road_permits.store import Ledger


class RestartSweepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "ledger.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _phase_one(self):
        """T0 时刻：建立数据，含将过期的许可、出车、申请与资质。"""
        clock = FakeClock(BASE)
        service = LedgerService(Ledger(self.db_path), clock=clock)
        ent, veh, drv, seg, app = bootstrap(service, valid_days=1, asset_days=1)
        approve_all(service, app, "medium")
        permit = service.list_permits()[0]

        # 未来出车（获准但未执行）与已完成出车（均在 22:00-02:00 跨日窗口内）
        start = BASE.replace(hour=23, minute=0, second=0, microsecond=0)
        pending_trip = service.schedule_trip(
            permit["id"], drv["id"], iso(start), iso(start + timedelta(hours=1)))
        self.assertEqual(pending_trip["status"], TripStatus.APPROVED)
        done_trip = service.schedule_trip(
            permit["id"], drv["id"], iso(start), iso(start + timedelta(hours=1)))
        service.complete_trip(done_trip["id"])

        # 一个始终不审结的申请（有效期同样 1 天）
        stale_app = service.submit_application(
            ent["id"], veh["id"], drv["id"], seg["id"],
            iso(BASE + timedelta(days=2)), iso(BASE + timedelta(days=3)),
            "22:00", "02:00", "搁置的申请",
        )
        service.store.close()
        return {
            "permit": permit, "pending_trip": pending_trip, "done_trip": done_trip,
            "stale_app": stale_app, "veh": veh, "drv": drv, "ent": ent,
        }

    def test_sweep_runs_on_restart_and_is_idempotent(self):
        ids = self._phase_one()

        # 模拟 3 天后服务重启：新进程打开同一台账文件，启动时执行清理
        clock = FakeClock(BASE + timedelta(days=3))
        service = LedgerService(Ledger(self.db_path), clock=clock)
        summary = service.startup()

        self.assertIn(ids["permit"]["id"], summary["permits"])
        self.assertIn(ids["pending_trip"]["id"], summary["trips"])
        self.assertIn(ids["stale_app"]["id"], summary["applications"])
        self.assertIn(ids["veh"]["id"], summary["vehicles"])
        self.assertIn(ids["drv"]["id"], summary["drivers"])

        # 过期许可不再有效，不能再安排出车
        permit = service.list_permits()[0]
        self.assertEqual(permit["status"], PermitStatus.EXPIRED)
        # 未执行的出车被归档为过期
        trip = service.store.get("trips", ids["pending_trip"]["id"])
        self.assertEqual(trip["status"], TripStatus.EXPIRED)
        # 已完成的测试记录保持 completed，不被清理动作篡改
        done = service.store.get("trips", ids["done_trip"]["id"])
        self.assertEqual(done["status"], TripStatus.COMPLETED)
        self.assertIsNotNone(done["completed_at"])
        # 搁置申请与到期资质进入终态
        app = service.store.get("applications", ids["stale_app"]["id"])
        self.assertEqual(app["status"], ApplicationStatus.EXPIRED)
        self.assertEqual(service.store.get("vehicles", ids["veh"]["id"])["status"],
                         RegistryStatus.EXPIRED)
        self.assertEqual(service.store.get("drivers", ids["drv"]["id"])["status"],
                         RegistryStatus.EXPIRED)

        # 幂等：再次清理（模拟连续重启）不再产生任何变更
        again = service.sweep_expired()
        self.assertTrue(all(not v for v in again.values()))
        service.store.close()

        # 第三次打开：数据保持一致
        reopened = LedgerService(Ledger(self.db_path), clock=clock)
        self.assertEqual(reopened.list_permits()[0]["status"], PermitStatus.EXPIRED)
        self.assertEqual(reopened.store.get("trips", ids["done_trip"]["id"])["status"],
                         TripStatus.COMPLETED)
        reopened.store.close()


if __name__ == "__main__":
    unittest.main()
