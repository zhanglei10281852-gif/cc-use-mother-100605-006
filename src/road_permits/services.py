"""许可台账业务服务层。

所有业务规则集中在这里；每个写操作都在事务内完成并追加审计事件。
时钟通过 ``clock`` 注入，测试可注入固定时间，服务重启后传入真实时钟
执行到期清理（sweep）即可验证。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Callable

from .domain import (
    FREEZABLE_TRIP_STATUSES,
    REQUIRED_LEVELS,
    SWEEPABLE_TRIP_STATUSES,
    ApplicationStatus,
    PermitStatus,
    RegistryStatus,
    TripStatus,
    interval_in_window,
    iso,
    now_utc,
    parse_dt,
    parse_hhmm,
)
from .store import Ledger


class ServiceError(Exception):
    """业务错误：code 用于 API 映射，message 面向操作员。"""

    def __init__(self, code: str, message: str, http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def _require(value: Any, field: str) -> Any:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ServiceError("validation", f"缺少必填字段：{field}")
    return value


class LedgerService:
    def __init__(self, store: Ledger, clock: Callable[[], datetime] = now_utc):
        self.store = store
        self.clock = clock

    # ------------------------------------------------------------ 内部工具

    def _now(self) -> datetime:
        return self.clock()

    def _event(
        self,
        entity_type: str,
        entity_id: str,
        action: str,
        actor: str,
        reason: str,
        payload: dict[str, Any],
    ) -> None:
        self.store.append_event(
            entity_type, entity_id, action, actor, reason, payload, iso(self._now())
        )

    def _get_or_404(self, table: str, entity_id: str, label: str) -> dict[str, Any]:
        row = self.store.get(table, entity_id)
        if row is None:
            raise ServiceError("not_found", f"{label}不存在：{entity_id}", 404)
        return row

    # ------------------------------------------------------------ 注册信息

    def register_enterprise(self, name: str, credit_code: str, actor: str = "system") -> dict:
        _require(name, "name")
        _require(credit_code, "credit_code")
        with self.store.tx():
            ent_id = self.store.next_id("enterprise")
            row = {
                "id": ent_id,
                "name": name.strip(),
                "credit_code": credit_code.strip(),
                "status": RegistryStatus.ACTIVE,
                "created_at": iso(self._now()),
            }
            try:
                self.store.insert("enterprises", row)
            except sqlite3.IntegrityError as exc:
                raise ServiceError("duplicate", f"统一社会信用代码已登记：{credit_code}", 409) from exc
            self._event("enterprise", ent_id, "enterprise.registered", actor, "企业登记", row)
            return row

    def register_vehicle(
        self, enterprise_id: str, plate: str, qualification_expires_at: str, actor: str = "system"
    ) -> dict:
        _require(plate, "plate")
        expires = parse_dt(_require(qualification_expires_at, "qualification_expires_at"))
        with self.store.tx():
            self._get_or_404("enterprises", enterprise_id, "企业")
            veh_id = self.store.next_id("vehicle")
            row = {
                "id": veh_id,
                "enterprise_id": enterprise_id,
                "plate": plate.strip(),
                "qualification_expires_at": iso(expires),
                "status": RegistryStatus.ACTIVE,
                "created_at": iso(self._now()),
            }
            try:
                self.store.insert("vehicles", row)
            except sqlite3.IntegrityError as exc:
                raise ServiceError("duplicate", f"车牌已登记：{plate}", 409) from exc
            self._event("vehicle", veh_id, "vehicle.registered", actor, "车辆登记", row)
            return row

    def register_driver(
        self, enterprise_id: str, name: str, license_no: str,
        authorization_expires_at: str, actor: str = "system",
    ) -> dict:
        _require(name, "name")
        _require(license_no, "license_no")
        expires = parse_dt(_require(authorization_expires_at, "authorization_expires_at"))
        with self.store.tx():
            self._get_or_404("enterprises", enterprise_id, "企业")
            drv_id = self.store.next_id("driver")
            row = {
                "id": drv_id,
                "enterprise_id": enterprise_id,
                "name": name.strip(),
                "license_no": license_no.strip(),
                "authorization_expires_at": iso(expires),
                "status": RegistryStatus.ACTIVE,
                "created_at": iso(self._now()),
            }
            try:
                self.store.insert("drivers", row)
            except sqlite3.IntegrityError as exc:
                raise ServiceError("duplicate", f"驾驶证号已登记：{license_no}", 409) from exc
            self._event("driver", drv_id, "driver.registered", actor, "驾驶员登记", row)
            return row

    def register_segment(self, name: str, risk_level: str, actor: str = "system") -> dict:
        _require(name, "name")
        if risk_level not in REQUIRED_LEVELS:
            raise ServiceError("validation", f"风险等级须为 {sorted(REQUIRED_LEVELS)} 之一")
        with self.store.tx():
            seg_id = self.store.next_id("segment")
            row = {
                "id": seg_id,
                "name": name.strip(),
                "risk_level": risk_level,
                "status": RegistryStatus.ACTIVE,
                "created_at": iso(self._now()),
            }
            self.store.insert("segments", row)
            self._event("segment", seg_id, "segment.registered", actor, "路段登记", row)
            return row

    # ------------------------------------------------------------ 测试申请

    def submit_application(
        self,
        enterprise_id: str,
        vehicle_id: str,
        driver_id: str,
        segment_id: str,
        valid_from: str,
        valid_to: str,
        window_start: str,
        window_end: str,
        purpose: str,
        actor: str = "system",
    ) -> dict:
        start = parse_dt(_require(valid_from, "valid_from"))
        end = parse_dt(_require(valid_to, "valid_to"))
        if end <= start:
            raise ServiceError("validation", "许可有效期 valid_to 必须晚于 valid_from")
        win_start = parse_hhmm(_require(window_start, "window_start"))
        win_end = parse_hhmm(_require(window_end, "window_end"))
        _require(purpose, "purpose")
        with self.store.tx():
            self._get_or_404("enterprises", enterprise_id, "企业")
            vehicle = self._get_or_404("vehicles", vehicle_id, "车辆")
            driver = self._get_or_404("drivers", driver_id, "驾驶员")
            self._get_or_404("segments", segment_id, "路段")
            if vehicle["enterprise_id"] != enterprise_id:
                raise ServiceError("validation", "车辆不属于申请企业")
            if driver["enterprise_id"] != enterprise_id:
                raise ServiceError("validation", "驾驶员不属于申请企业")
            duplicate = self._find_live_application(
                enterprise_id, vehicle_id, segment_id, start, end
            )
            if duplicate is not None:
                raise ServiceError(
                    "duplicate",
                    f"重复申请：与进行中的申请 {duplicate['id']} 在有效期上重叠",
                    409,
                )
            app_id = self.store.next_id("application")
            row = {
                "id": app_id,
                "enterprise_id": enterprise_id,
                "vehicle_id": vehicle_id,
                "driver_id": driver_id,
                "segment_id": segment_id,
                "valid_from": iso(start),
                "valid_to": iso(end),
                "window_start": window_start,
                "window_end": window_end,
                "purpose": purpose.strip(),
                "status": ApplicationStatus.PENDING,
                "created_at": iso(self._now()),
                "decided_at": None,
            }
            self.store.insert("applications", row)
            self._event("application", app_id, "application.submitted", actor, "企业提交测试申请", row)
            return row

    def _find_live_application(
        self, enterprise_id: str, vehicle_id: str, segment_id: str,
        start: datetime, end: datetime,
    ) -> dict | None:
        candidates = self.store.find(
            "applications",
            "enterprise_id = ? AND vehicle_id = ? AND segment_id = ? AND status = ?",
            (enterprise_id, vehicle_id, segment_id, ApplicationStatus.PENDING),
        )
        for item in candidates:
            if parse_dt(item["valid_from"]) < end and start < parse_dt(item["valid_to"]):
                return item
        return None

    # ------------------------------------------------------------ 分级审批

    def decide_application(
        self, application_id: str, level: int, approver: str,
        decision: str, reason: str, actor: str = "system",
    ) -> dict:
        _require(approver, "approver")
        _require(reason, "reason")
        if decision not in ("approve", "reject"):
            raise ServiceError("validation", "decision 须为 approve 或 reject")
        with self.store.tx():
            app = self._get_or_404("applications", application_id, "申请")
            if app["status"] != ApplicationStatus.PENDING:
                raise ServiceError("invalid_state", f"申请已终结（{app['status']}），不能再审批", 409)
            segment = self._get_or_404("segments", app["segment_id"], "路段")
            required = REQUIRED_LEVELS[segment["risk_level"]]
            prior = self.store.find(
                "approvals", "application_id = ?", (application_id,), order="level"
            )
            expected = len(prior) + 1
            if level != expected:
                raise ServiceError(
                    "invalid_state",
                    f"分级审批须逐级进行：当前应审第 {expected} 级，收到第 {level} 级",
                    409,
                )
            if level not in required:
                raise ServiceError(
                    "validation",
                    f"该路段（{segment['risk_level']}）仅需审批级别 {list(required)}",
                )
            if any(p["approver"] == approver for p in prior):
                raise ServiceError(
                    "invalid_state", "同一审批人不得对同一申请重复审批", 409
                )
            record = {
                "application_id": application_id,
                "level": level,
                "approver": approver.strip(),
                "decision": decision,
                "reason": reason.strip(),
                "created_at": iso(self._now()),
            }
            self.store.insert("approvals", record)
            self._event(
                "application", application_id, f"application.level{level}_{decision}d",
                actor, reason, {"level": level, "approver": approver, "decision": decision},
            )
            if decision == "reject":
                self.store.update("applications", application_id, {
                    "status": ApplicationStatus.REJECTED, "decided_at": iso(self._now()),
                })
                self._event("application", application_id, "application.rejected",
                            actor, reason, {"level": level, "approver": approver})
                return self.store.get("applications", application_id)
            if level == required[-1]:
                self.store.update("applications", application_id, {
                    "status": ApplicationStatus.APPROVED, "decided_at": iso(self._now()),
                })
                permit = self._mint_permit(app, actor, reason)
                self._event("application", application_id, "application.approved",
                            actor, reason, {"permit_id": permit["id"], "version": permit["version"]})
            return self.store.get("applications", application_id)

    def _mint_permit(self, app: dict, actor: str, reason: str) -> dict:
        """按（车辆 × 路段）铸造新许可版本；旧的有效版本被取代并冻结其后续出车。"""
        siblings = self.store.find(
            "permits", "vehicle_id = ? AND segment_id = ?",
            (app["vehicle_id"], app["segment_id"]),
        )
        version = max((p["version"] for p in siblings), default=0) + 1
        for old in siblings:
            if old["status"] == PermitStatus.ACTIVE:
                self.store.update("permits", old["id"], {"status": PermitStatus.SUPERSEDED})
                self._event("permit", old["id"], "permit.superseded", actor,
                            f"被新版本取代（{reason}）", {"by_application": app["id"]})
                self._freeze_trips(
                    actor,
                    f"许可版本更替：{old['id']} 被取代",
                    permit_id=old["id"],
                )
        permit_id = self.store.next_id("permit")
        permit = {
            "id": permit_id,
            "application_id": app["id"],
            "enterprise_id": app["enterprise_id"],
            "vehicle_id": app["vehicle_id"],
            "driver_id": app["driver_id"],
            "segment_id": app["segment_id"],
            "version": version,
            "valid_from": app["valid_from"],
            "valid_to": app["valid_to"],
            "window_start": app["window_start"],
            "window_end": app["window_end"],
            "status": PermitStatus.ACTIVE,
            "created_at": iso(self._now()),
        }
        self.store.insert("permits", permit)
        self._event("permit", permit_id, "permit.issued", actor, reason, permit)
        return permit

    # ------------------------------------------------------------ 出车决策

    def schedule_trip(
        self,
        permit_id: str,
        driver_id: str,
        planned_start: str,
        planned_end: str,
        vehicle_id: str | None = None,
        operator: str = "dispatcher",
    ) -> dict:
        """调度出车：同步执行决策引擎，获准或被拒均留档并可解释。"""
        start = parse_dt(_require(planned_start, "planned_start"))
        end = parse_dt(_require(planned_end, "planned_end"))
        with self.store.tx():
            permit = self._get_or_404("permits", permit_id, "许可")
            target_vehicle_id = vehicle_id or permit["vehicle_id"]
            checks: list[dict[str, Any]] = []

            def check(code: str, ok: bool, message: str) -> None:
                checks.append({"code": code, "ok": bool(ok), "message": message})

            now = self._now()
            check("permit_active", permit["status"] == PermitStatus.ACTIVE,
                  f"许可状态为 {permit['status']}")
            check("schedule_in_future", start >= now,
                  f"计划开始 {iso(start)} 须不早于当前 {iso(now)}")
            check("trip_duration", end > start and (end - start).total_seconds() <= 24 * 3600,
                  "计划结束须晚于开始且时长不超过 24 小时")
            within_validity = parse_dt(permit["valid_from"]) <= start and end <= parse_dt(permit["valid_to"])
            check("trip_within_validity", within_validity,
                  f"出车区间须落在许可有效期 {permit['valid_from']} ~ {permit['valid_to']} 内")
            win_start = parse_hhmm(permit["window_start"])
            win_end = parse_hhmm(permit["window_end"])
            in_window = interval_in_window(start, end, win_start, win_end)
            check("daily_window", in_window,
                  f"出车时间须落在每日窗口 {permit['window_start']}-{permit['window_end']} 内")

            segment = self._get_or_404("segments", permit["segment_id"], "路段")
            check("segment_open", segment["status"] == RegistryStatus.ACTIVE,
                  f"路段状态为 {segment['status']}")

            vehicle = self._get_or_404("vehicles", target_vehicle_id, "车辆")
            qual_ok = (vehicle["status"] == RegistryStatus.ACTIVE
                       and parse_dt(vehicle["qualification_expires_at"]) >= end)
            check("vehicle_qualification", qual_ok,
                  f"车辆资质有效期至 {vehicle['qualification_expires_at']}，状态 {vehicle['status']}")

            driver = self._get_or_404("drivers", driver_id, "驾驶员")
            drv_ok = (driver["status"] == RegistryStatus.ACTIVE
                      and driver["enterprise_id"] == permit["enterprise_id"]
                      and parse_dt(driver["authorization_expires_at"]) >= end)
            check("driver_authorization", drv_ok,
                  f"驾驶员授权有效期至 {driver['authorization_expires_at']}，状态 {driver['status']}")

            substitute_for = None
            if target_vehicle_id != permit["vehicle_id"]:
                substitute_for = permit["vehicle_id"]
                check("substitute_same_enterprise",
                      vehicle["enterprise_id"] == permit["enterprise_id"],
                      "临时替代车辆须属于同一企业")
                covering = self._find_covering_permit(
                    target_vehicle_id, permit["segment_id"], start, end
                )
                check("substitute_coverage", covering is not None,
                      "临时替代车辆须持有同路段有效许可"
                      + (f"（覆盖许可 {covering['id']}）" if covering else ""))
            else:
                check("substitute_coverage", True, "使用许可登记车辆，无需替代覆盖")

            approved = all(c["ok"] for c in checks)
            decision = {
                "outcome": TripStatus.APPROVED if approved else TripStatus.DENIED,
                "decided_at": iso(now),
                "permit_id": permit_id,
                "permit_version": permit["version"],
                "vehicle_id": target_vehicle_id,
                "substitute_for": substitute_for,
                "checks": checks,
                "failures": [c["message"] for c in checks if not c["ok"]],
            }
            trip_id = self.store.next_id("trip")
            trip = {
                "id": trip_id,
                "permit_id": permit_id,
                "enterprise_id": permit["enterprise_id"],
                "vehicle_id": target_vehicle_id,
                "substitute_for": substitute_for,
                "driver_id": driver_id,
                "segment_id": permit["segment_id"],
                "planned_start": iso(start),
                "planned_end": iso(end),
                "status": decision["outcome"],
                "decision": json.dumps(decision, ensure_ascii=False),
                "created_at": iso(now),
                "completed_at": None,
            }
            self.store.insert("trips", trip)
            action = "trip.approved" if approved else "trip.denied"
            reason = "全部检查通过，获准出车" if approved else "；".join(decision["failures"])
            self._event("trip", trip_id, action, operator, reason, decision)
            return {**trip, "decision": decision}

    def _find_covering_permit(
        self, vehicle_id: str, segment_id: str, start: datetime, end: datetime
    ) -> dict | None:
        candidates = self.store.find(
            "permits", "vehicle_id = ? AND segment_id = ? AND status = ?",
            (vehicle_id, segment_id, PermitStatus.ACTIVE),
        )
        for permit in candidates:
            if parse_dt(permit["valid_from"]) <= start and end <= parse_dt(permit["valid_to"]):
                win_start = parse_hhmm(permit["window_start"])
                win_end = parse_hhmm(permit["window_end"])
                if interval_in_window(start, end, win_start, win_end):
                    return permit
        return None

    def complete_trip(self, trip_id: str, actor: str = "dispatcher") -> dict:
        with self.store.tx():
            trip = self._get_or_404("trips", trip_id, "出车安排")
            if trip["status"] != TripStatus.APPROVED:
                raise ServiceError(
                    "invalid_state", f"仅获准的出车可登记完成（当前 {trip['status']}）", 409
                )
            self.store.update("trips", trip_id, {
                "status": TripStatus.COMPLETED, "completed_at": iso(self._now()),
            })
            self._event("trip", trip_id, "trip.completed", actor, "测试完成，记录归档", {})
            return self.store.get("trips", trip_id)

    # ------------------------------------------------------------ 冻结与撤销

    def _freeze_trips(self, actor: str, reason: str, **filters: Any) -> list[str]:
        """冻结符合条件的后续出车安排；已完成/被拒/已过期的记录一律不动。"""
        clauses = [f"status IN ({','.join('?' for _ in FREEZABLE_TRIP_STATUSES)})"]
        params: list[Any] = list(FREEZABLE_TRIP_STATUSES)
        for column, value in filters.items():
            clauses.append(f"{column} = ?")
            params.append(value)
        rows = self.store.find("trips", " AND ".join(clauses), tuple(params))
        frozen: list[str] = []
        for row in rows:
            self.store.update("trips", row["id"], {"status": TripStatus.FROZEN})
            self._event("trip", row["id"], "trip.frozen", actor, reason,
                        {"previous_status": row["status"]})
            frozen.append(row["id"])
        return frozen

    def report_incident(
        self, vehicle_id: str, description: str,
        trip_id: str | None = None, reporter: str = "supervisor",
    ) -> dict:
        """事故报告：冻结该车全部有效许可与后续出车，已完成记录保持不变。"""
        _require(description, "description")
        with self.store.tx():
            self._get_or_404("vehicles", vehicle_id, "车辆")
            if trip_id is not None:
                self._get_or_404("trips", trip_id, "出车安排")
            inc_id = self.store.next_id("incident")
            incident = {
                "id": inc_id,
                "vehicle_id": vehicle_id,
                "trip_id": trip_id,
                "description": description.strip(),
                "status": "open",
                "reported_by": reporter,
                "reported_at": iso(self._now()),
                "resolved_at": None,
            }
            self.store.insert("incidents", incident)
            reason = f"事故报告 {inc_id}：{description.strip()}"
            frozen_permits = []
            for permit in self.store.find(
                "permits", "vehicle_id = ? AND status = ?", (vehicle_id, PermitStatus.ACTIVE)
            ):
                self.store.update("permits", permit["id"], {"status": PermitStatus.FROZEN})
                self._event("permit", permit["id"], "permit.frozen", reporter, reason,
                            {"incident_id": inc_id})
                frozen_permits.append(permit["id"])
            frozen_trips = self._freeze_trips(reporter, reason, vehicle_id=vehicle_id)
            self._event("incident", inc_id, "incident.reported", reporter, reason,
                        {"incident": incident, "frozen_permits": frozen_permits,
                         "frozen_trips": frozen_trips})
            return {**incident, "frozen_permits": frozen_permits, "frozen_trips": frozen_trips}

    def resolve_incident(self, incident_id: str, reason: str, actor: str = "supervisor") -> dict:
        """事故结案：解除许可冻结（已过期的转为过期）；被冻结的出车安排不自动恢复。"""
        _require(reason, "reason")
        with self.store.tx():
            incident = self._get_or_404("incidents", incident_id, "事故报告")
            if incident["status"] != "open":
                raise ServiceError("invalid_state", "事故报告已结案", 409)
            now = self._now()
            self.store.update("incidents", incident_id, {
                "status": "resolved", "resolved_at": iso(now),
            })
            restored, expired = [], []
            for permit in self.store.find(
                "permits", "vehicle_id = ? AND status = ?",
                (incident["vehicle_id"], PermitStatus.FROZEN),
            ):
                if parse_dt(permit["valid_to"]) > now:
                    self.store.update("permits", permit["id"], {"status": PermitStatus.ACTIVE})
                    self._event("permit", permit["id"], "permit.unfrozen", actor, reason,
                                {"incident_id": incident_id})
                    restored.append(permit["id"])
                else:
                    self.store.update("permits", permit["id"], {"status": PermitStatus.EXPIRED})
                    self._event("permit", permit["id"], "permit.expired", actor,
                                "结案时许可已过期", {"incident_id": incident_id})
                    expired.append(permit["id"])
            self._event("incident", incident_id, "incident.resolved", actor, reason,
                        {"restored_permits": restored, "expired_permits": expired})
            return {**self.store.get("incidents", incident_id),
                    "restored_permits": restored, "expired_permits": expired}

    def change_route(
        self, permit_id: str, new_segment_id: str, reason: str, actor: str = "dispatcher"
    ) -> dict:
        """路线变更：作废旧许可版本并冻结其后续出车，同时为新路段生成待审批申请。"""
        _require(reason, "reason")
        with self.store.tx():
            permit = self._get_or_404("permits", permit_id, "许可")
            if permit["status"] != PermitStatus.ACTIVE:
                raise ServiceError(
                    "invalid_state", f"仅有效许可可发起路线变更（当前 {permit['status']}）", 409
                )
            self._get_or_404("segments", new_segment_id, "路段")
            if new_segment_id == permit["segment_id"]:
                raise ServiceError("validation", "新路段与原路段相同")
            self.store.update("permits", permit_id, {"status": PermitStatus.SUPERSEDED})
            full_reason = f"路线变更：{reason.strip()}"
            self._event("permit", permit_id, "permit.superseded", actor, full_reason,
                        {"new_segment_id": new_segment_id})
            frozen_trips = self._freeze_trips(actor, full_reason, permit_id=permit_id)
            duplicate = self._find_live_application(
                permit["enterprise_id"], permit["vehicle_id"], new_segment_id,
                parse_dt(permit["valid_from"]), parse_dt(permit["valid_to"]),
            )
            if duplicate is not None:
                raise ServiceError(
                    "duplicate",
                    f"路线变更失败：新路段已存在进行中的申请 {duplicate['id']}",
                    409,
                )
            app_id = self.store.next_id("application")
            application = {
                "id": app_id,
                "enterprise_id": permit["enterprise_id"],
                "vehicle_id": permit["vehicle_id"],
                "driver_id": permit["driver_id"],
                "segment_id": new_segment_id,
                "valid_from": permit["valid_from"],
                "valid_to": permit["valid_to"],
                "window_start": permit["window_start"],
                "window_end": permit["window_end"],
                "purpose": f"路线变更（原许可 {permit_id}）：{reason.strip()}",
                "status": ApplicationStatus.PENDING,
                "created_at": iso(self._now()),
                "decided_at": None,
            }
            self.store.insert("applications", application)
            self._event("application", app_id, "application.submitted", actor,
                        full_reason, application)
            return {"superseded_permit": permit_id, "frozen_trips": frozen_trips,
                    "new_application": application}

    def revoke_vehicle_qualification(
        self, vehicle_id: str, reason: str, actor: str = "regulator"
    ) -> dict:
        """车辆资质撤回：车辆及其许可进入终态，后续出车冻结。"""
        _require(reason, "reason")
        with self.store.tx():
            self._get_or_404("vehicles", vehicle_id, "车辆")
            self.store.update("vehicles", vehicle_id, {"status": RegistryStatus.REVOKED})
            full_reason = f"车辆资质撤回：{reason.strip()}"
            revoked = []
            for permit in self.store.find(
                "permits", "vehicle_id = ? AND status IN (?, ?)",
                (vehicle_id, PermitStatus.ACTIVE, PermitStatus.FROZEN),
            ):
                self.store.update("permits", permit["id"], {"status": PermitStatus.REVOKED})
                self._event("permit", permit["id"], "permit.revoked", actor, full_reason, {})
                revoked.append(permit["id"])
            frozen_trips = self._freeze_trips(actor, full_reason, vehicle_id=vehicle_id)
            self._event("vehicle", vehicle_id, "vehicle.qualification_revoked", actor,
                        full_reason, {"revoked_permits": revoked, "frozen_trips": frozen_trips})
            return {"vehicle_id": vehicle_id, "revoked_permits": revoked,
                    "frozen_trips": frozen_trips}

    def revoke_driver(self, driver_id: str, reason: str, actor: str = "regulator") -> dict:
        """驾驶员授权撤回：该驾驶员的后续出车安排冻结。"""
        _require(reason, "reason")
        with self.store.tx():
            self._get_or_404("drivers", driver_id, "驾驶员")
            self.store.update("drivers", driver_id, {"status": RegistryStatus.REVOKED})
            full_reason = f"驾驶员授权撤回：{reason.strip()}"
            frozen_trips = self._freeze_trips(actor, full_reason, driver_id=driver_id)
            self._event("driver", driver_id, "driver.revoked", actor, full_reason,
                        {"frozen_trips": frozen_trips})
            return {"driver_id": driver_id, "frozen_trips": frozen_trips}

    # ------------------------------------------------------------ 到期清理

    def sweep_expired(self, actor: str = "system") -> dict:
        """到期清理：服务启动时执行，幂等。

        过期的许可、未执行的出车、未审结的申请、到期的车辆资质与驾驶员授权
        分别转入 expired 终态；已完成与已冻结留档的记录不受影响。
        """
        now = self._now()
        summary = {"permits": [], "trips": [], "applications": [],
                   "vehicles": [], "drivers": []}
        with self.store.tx():
            for permit in self.store.find(
                "permits", "status IN (?, ?)", (PermitStatus.ACTIVE, PermitStatus.FROZEN)
            ):
                if parse_dt(permit["valid_to"]) <= now:
                    self.store.update("permits", permit["id"], {"status": PermitStatus.EXPIRED})
                    self._event("permit", permit["id"], "permit.expired", actor,
                                "许可有效期届满", {"valid_to": permit["valid_to"]})
                    summary["permits"].append(permit["id"])
            placeholders = ",".join("?" for _ in SWEEPABLE_TRIP_STATUSES)
            for trip in self.store.find(
                "trips", f"status IN ({placeholders})", tuple(SWEEPABLE_TRIP_STATUSES)
            ):
                if parse_dt(trip["planned_end"]) <= now:
                    self.store.update("trips", trip["id"], {"status": TripStatus.EXPIRED})
                    self._event("trip", trip["id"], "trip.expired", actor,
                                "计划时间已过且未执行", {"planned_end": trip["planned_end"]})
                    summary["trips"].append(trip["id"])
            for app in self.store.find(
                "applications", "status = ?", (ApplicationStatus.PENDING,)
            ):
                if parse_dt(app["valid_to"]) <= now:
                    self.store.update("applications", app["id"],
                                      {"status": ApplicationStatus.EXPIRED})
                    self._event("application", app["id"], "application.expired", actor,
                                "申请有效期已过且未审结", {})
                    summary["applications"].append(app["id"])
            for vehicle in self.store.find(
                "vehicles", "status = ?", (RegistryStatus.ACTIVE,)
            ):
                if parse_dt(vehicle["qualification_expires_at"]) <= now:
                    self.store.update("vehicles", vehicle["id"],
                                      {"status": RegistryStatus.EXPIRED})
                    self._event("vehicle", vehicle["id"], "vehicle.qualification_expired",
                                actor, "车辆资质到期", {})
                    summary["vehicles"].append(vehicle["id"])
            for driver in self.store.find("drivers", "status = ?", (RegistryStatus.ACTIVE,)):
                if parse_dt(driver["authorization_expires_at"]) <= now:
                    self.store.update("drivers", driver["id"],
                                      {"status": RegistryStatus.EXPIRED})
                    self._event("driver", driver["id"], "driver.authorization_expired",
                                actor, "驾驶员授权到期", {})
                    summary["drivers"].append(driver["id"])
            self._event("ledger", "sweep", "sweep.completed", actor,
                        "到期清理完成", {k: len(v) for k, v in summary.items()})
        return summary

    def startup(self, actor: str = "system") -> dict:
        """服务启动钩子：重开台账后先做到期清理，保证过期许可不再被安排。"""
        return self.sweep_expired(actor=actor)

    # ------------------------------------------------------------ 查询与解释

    def explain_trip(self, trip_id: str) -> dict:
        """解释某次出车为何获准、被拒或被冻结/撤销。"""
        trip = self._get_or_404("trips", trip_id, "出车安排")
        permit = self._get_or_404("permits", trip["permit_id"], "许可")
        decision = json.loads(trip["decision"])
        events = self.store.find(
            "events", "(entity_type = 'trip' AND entity_id = ?)"
            " OR (entity_type = 'permit' AND entity_id = ?)",
            (trip_id, trip["permit_id"]), order="id",
        )
        summary = self._summarize_trip(trip, decision, events)
        return {
            "trip": trip,
            "permit": permit,
            "decision": decision,
            "events": events,
            "summary": summary,
        }

    @staticmethod
    def _summarize_trip(trip: dict, decision: dict, events: list[dict]) -> str:
        status = trip["status"]
        if status == TripStatus.APPROVED:
            return (f"出车 {trip['id']} 获准：许可 {trip['permit_id']} "
                    f"v{decision['permit_version']} 全部 {len(decision['checks'])} 项检查通过。")
        if status == TripStatus.DENIED:
            return (f"出车 {trip['id']} 被拒：" + "；".join(decision["failures"]))
        if status == TripStatus.COMPLETED:
            return f"出车 {trip['id']} 已于 {trip['completed_at']} 完成，记录归档不可更改。"
        if status == TripStatus.FROZEN:
            freeze = next((e for e in reversed(events) if e["action"] == "trip.frozen"), None)
            reason = freeze["reason"] if freeze else "原因未记录"
            return f"出车 {trip['id']} 的后续安排被冻结：{reason}"
        if status == TripStatus.EXPIRED:
            return f"出车 {trip['id']} 计划时间已过且未执行，按到期清理归档。"
        return f"出车 {trip['id']} 当前状态：{status}"

    def list_permits(self, vehicle_id: str | None = None,
                     status: str | None = None) -> list[dict]:
        clauses, params = [], []
        if vehicle_id:
            clauses.append("vehicle_id = ?")
            params.append(vehicle_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = " AND ".join(clauses) if clauses else "1=1"
        return self.store.find("permits", where, tuple(params))

    def list_trips(self, status: str | None = None,
                   vehicle_id: str | None = None) -> list[dict]:
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if vehicle_id:
            clauses.append("vehicle_id = ?")
            params.append(vehicle_id)
        where = " AND ".join(clauses) if clauses else "1=1"
        return self.store.find("trips", where, tuple(params))

    def list_events(self, entity_type: str | None = None,
                    entity_id: str | None = None) -> list[dict]:
        clauses, params = [], []
        if entity_type:
            clauses.append("entity_type = ?")
            params.append(entity_type)
        if entity_id:
            clauses.append("entity_id = ?")
            params.append(entity_id)
        where = " AND ".join(clauses) if clauses else "1=1"
        return self.store.find("events", where, tuple(params), order="id")

    def get_application(self, application_id: str) -> dict:
        app = self._get_or_404("applications", application_id, "申请")
        approvals = self.store.find(
            "approvals", "application_id = ?", (application_id,), order="level"
        )
        segment = self._get_or_404("segments", app["segment_id"], "路段")
        return {**app, "approvals": approvals,
                "required_levels": list(REQUIRED_LEVELS[segment["risk_level"]])}
