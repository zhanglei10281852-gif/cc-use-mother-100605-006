"""测试共享工具：固定时钟与一键搭好注册数据。"""
from datetime import datetime, timedelta, timezone

from road_permits.domain import REQUIRED_LEVELS, iso
from road_permits.services import LedgerService
from road_permits.store import Ledger

BASE = datetime(2026, 10, 7, 8, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, now: datetime = BASE):
        self._now = now

    def __call__(self) -> datetime:
        return self._now

    def advance(self, **kwargs) -> None:
        self._now += timedelta(**kwargs)


def make_service(db_path: str = ":memory:", clock: FakeClock | None = None):
    clock = clock or FakeClock()
    return LedgerService(Ledger(db_path), clock=clock), clock


def bootstrap(service, risk="medium", window=("22:00", "02:00"),
              valid_days=30, asset_days=180, seg_name="浑南大道测试段"):
    """登记企业/车辆/驾驶员/路段并提交一份申请，返回各实体。"""
    now = service.clock()
    ent = service.register_enterprise("沈阳测试企业", "CREDIT-0001")
    veh = service.register_vehicle(ent["id"], "辽A·T0001", iso(now + timedelta(days=asset_days)))
    drv = service.register_driver(ent["id"], "安全员甲", "LIC-0001",
                                  iso(now + timedelta(days=asset_days)))
    seg = service.register_segment(seg_name, risk)
    app = service.submit_application(
        ent["id"], veh["id"], drv["id"], seg["id"],
        iso(now + timedelta(hours=1)), iso(now + timedelta(days=valid_days)),
        window[0], window[1], "开放道路无人配送测试",
    )
    return ent, veh, drv, seg, app


def approve_all(service, app, risk):
    """按路段风险等级逐级通过审批，返回最终申请状态。"""
    for level in REQUIRED_LEVELS[risk]:
        app = service.decide_application(
            app["id"], level, f"审批员-{level}级", "approve", f"第 {level} 级审核通过"
        )
    return app


def night_trip_times(hour=23, duration_hours=2):
    """生成 BASE 次日某个夜间出车区间（默认 23:00 起，落在 22:00-02:00 跨日窗口内）。"""
    start = (BASE + timedelta(days=1)).replace(hour=hour, minute=0, second=0, microsecond=0)
    return iso(start), iso(start + timedelta(hours=duration_hours))
