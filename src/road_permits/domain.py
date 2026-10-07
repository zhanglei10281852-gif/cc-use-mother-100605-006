"""领域常量、时间工具与每日测试窗口（含跨日窗口）判定逻辑。"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone


class ApplicationStatus:
    PENDING = "pending"      # 审批中
    APPROVED = "approved"    # 已通过（已铸许可）
    REJECTED = "rejected"    # 已驳回
    EXPIRED = "expired"      # 审批未完成且申请有效期已过


class PermitStatus:
    ACTIVE = "active"          # 有效
    SUPERSEDED = "superseded"  # 被更新版本取代
    FROZEN = "frozen"          # 因事故报告被冻结（可解除）
    REVOKED = "revoked"        # 因资质撤回被撤销（终态）
    EXPIRED = "expired"        # 过期（终态）


class TripStatus:
    APPROVED = "approved"    # 获准出车
    DENIED = "denied"        # 被拒（留档可解释）
    COMPLETED = "completed"  # 已完成（不可篡改）
    FROZEN = "frozen"        # 后续安排被冻结
    EXPIRED = "expired"      # 未执行且计划时间已过


#: 冻结操作作用的出车状态集合（已完成的记录绝不在内）
FREEZABLE_TRIP_STATUSES = (TripStatus.APPROVED,)

#: 到期清理作用的出车状态集合
SWEEPABLE_TRIP_STATUSES = (TripStatus.APPROVED, TripStatus.FROZEN)


class RegistryStatus:
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"
    CLOSED = "closed"  # 仅路段使用


#: 路段风险等级 -> 需要的审批级别序列（分级审批）
REQUIRED_LEVELS = {"low": (1,), "medium": (1, 2), "high": (1, 2, 3)}


# ---------------------------------------------------------------- 时间工具

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_hhmm(value: str) -> time:
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"时间格式应为 HH:MM：{value!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"时间超出范围：{value!r}")
    return time(hour, minute)


# ------------------------------------------------------- 每日窗口（含跨日）

def time_in_window(moment: time, win_start: time, win_end: time) -> bool:
    """判断某个时刻是否落在每日窗口 [win_start, win_end) 内。

    win_start == win_end 视为全天开放；win_start > win_end 为跨日窗口
    （如 22:00-02:00）。
    """
    if win_start == win_end:
        return True
    if win_start < win_end:
        return win_start <= moment < win_end
    return moment >= win_start or moment < win_end


def interval_in_window(start: datetime, end: datetime, win_start: time, win_end: time) -> bool:
    """判断出车区间 [start, end) 是否完全落在每日窗口内。

    思路：窗口之外的“禁区”每天一段，逐个自然日检查区间是否与禁区相交。
    跨日窗口（22:00-02:00）的禁区是当天 [win_end, win_start)；
    普通窗口（09:00-17:00）的禁区是 [win_end, 次日 win_start)。
    """
    if win_start == win_end:
        return True
    day: date = start.date() - timedelta(days=1)
    last_day = end.date()
    while day <= last_day:
        if win_start < win_end:
            ban_start = datetime.combine(day, win_end, tzinfo=start.tzinfo)
            ban_end = datetime.combine(day + timedelta(days=1), win_start, tzinfo=start.tzinfo)
        else:
            ban_start = datetime.combine(day, win_end, tzinfo=start.tzinfo)
            ban_end = datetime.combine(day, win_start, tzinfo=start.tzinfo)
        if ban_start < end and start < ban_end:
            return False
        day += timedelta(days=1)
    return True
