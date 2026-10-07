"""端到端演示：申请 → 分级审批 → 许可铸版 → 出车决策 → 解释。

供 run_cli.py 与手工冒烟使用，全部在内存台账上运行，不落盘。
"""
from __future__ import annotations

from datetime import timedelta

from .domain import iso, now_utc
from .services import LedgerService
from .store import Ledger


def run_demo() -> list[str]:
    lines: list[str] = []
    service = LedgerService(Ledger(":memory:"))
    now = now_utc()

    ent = service.register_enterprise("沈阳某配送机器人公司", "91210100MA0DEMO001", actor="demo")
    veh = service.register_vehicle(ent["id"], "辽A·D0001", iso(now + timedelta(days=180)), actor="demo")
    drv = service.register_driver(ent["id"], "王安全", "210100200001",
                                  iso(now + timedelta(days=180)), actor="demo")
    seg = service.register_segment("浑南大道（创新路—全运路）", "medium", actor="demo")
    lines.append(f"登记完成：{ent['id']} / {veh['id']} / {drv['id']} / {seg['id']}")

    app = service.submit_application(
        ent["id"], veh["id"], drv["id"], seg["id"],
        iso(now + timedelta(days=1)), iso(now + timedelta(days=30)),
        "22:00", "02:00", "夜间无人配送开放道路测试", actor="demo",
    )
    lines.append(f"申请 {app['id']} 已提交（中风险路段需 1、2 两级审批）")

    service.decide_application(app["id"], 1, "审核员-李", "approve", "材料齐全", actor="demo")
    app = service.decide_application(app["id"], 2, "审批员-赵", "approve",
                                     "同意夜间窗口测试", actor="demo")
    lines.append(f"申请 {app['id']} 已通过分级审批")

    permit = service.list_permits(vehicle_id=veh["id"])[0]
    lines.append(f"铸造许可 {permit['id']} v{permit['version']}，"
                 f"窗口 {permit['window_start']}-{permit['window_end']}（跨日）")

    start = now + timedelta(days=1)
    start = start.replace(hour=23, minute=0, second=0, microsecond=0)
    trip = service.schedule_trip(permit["id"], drv["id"],
                                 iso(start), iso(start + timedelta(hours=2)),
                                 operator="demo")
    lines.append(f"出车 {trip['id']} 决策：{trip['status']}")

    report = service.explain_trip(trip["id"])
    lines.append(f"解释：{report['summary']}")
    return lines
