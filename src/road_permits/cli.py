"""操作员命令行接口：调度员无需走 HTTP 也能核查台账与解释出车。

用法示例：
    python -m road_permits.cli --db ledger.db explain-trip TRP-0001
    python -m road_permits.cli --db ledger.db trips --status frozen
    python -m road_permits.cli --db ledger.db permits --vehicle VEH-0001
    python -m road_permits.cli --db ledger.db events --entity-type trip --entity-id TRP-0001
    python -m road_permits.cli --db ledger.db sweep
"""
from __future__ import annotations

import argparse
import json
import sys

from .services import LedgerService, ServiceError
from .store import Ledger


def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="road_permits.cli", description="许可台账操作接口")
    parser.add_argument("--db", default="ledger.db", help="台账数据库文件路径")
    sub = parser.add_subparsers(dest="command", required=True)

    explain = sub.add_parser("explain-trip", help="解释某次出车为何获准/被拒/被冻结")
    explain.add_argument("trip_id")

    trips = sub.add_parser("trips", help="列出出车安排")
    trips.add_argument("--status")
    trips.add_argument("--vehicle")

    permits = sub.add_parser("permits", help="列出许可版本")
    permits.add_argument("--vehicle")
    permits.add_argument("--status")

    events = sub.add_parser("events", help="查看审计事件")
    events.add_argument("--entity-type")
    events.add_argument("--entity-id")

    sub.add_parser("sweep", help="立即执行一次到期清理")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    service = LedgerService(Ledger(args.db))
    try:
        if args.command == "explain-trip":
            report = service.explain_trip(args.trip_id)
            print(report["summary"])
            print("-" * 60)
            _print({"decision": report["decision"], "permit": report["permit"],
                    "events": report["events"]})
        elif args.command == "trips":
            _print(service.list_trips(status=args.status, vehicle_id=args.vehicle))
        elif args.command == "permits":
            _print(service.list_permits(vehicle_id=args.vehicle, status=args.status))
        elif args.command == "events":
            _print(service.list_events(entity_type=args.entity_type,
                                       entity_id=args.entity_id))
        elif args.command == "sweep":
            _print(service.sweep_expired())
        return 0
    except ServiceError as exc:
        print(f"错误[{exc.code}]：{exc.message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
