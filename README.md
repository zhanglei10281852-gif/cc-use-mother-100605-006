# 道路测试许可台账（沈阳无人配送开放道路）

面向无人配送车从封闭园区驶向开放道路的测试管理场景，从零实现的后端许可台账。
企业提交测试申请，系统按**车辆 × 路段**铸造**有时效的许可版本**；调度出车时由
决策引擎逐项核对车辆资质、路段限制、驾驶员授权与每日测试窗口；发生事故报告、
路线变更或资质撤回时**冻结后续安排，已完成的测试记录不被篡改**。全部状态流转
写入仅追加的审计事件，调度员可通过 API 或命令行解释某次出车为何获准、被拒或被撤销。

纯 Python 3.11 标准库实现（SQLite 存储 + `http.server` API），无第三方依赖。

## 运行

测试命令：

    PYTHONPATH=src python3 -m unittest discover -s tests -v

编译检查：

    python3 -m compileall -q src tests run_cli.py

命令行冒烟（含端到端演示）：

    python3 run_cli.py

启动 API 服务（启动时自动执行到期清理）：

    PYTHONPATH=src python3 -c "from road_permits.api import serve; serve('ledger.db', port=8080)"

操作员命令行（不依赖 HTTP）：

    PYTHONPATH=src python3 -m road_permits.cli --db ledger.db explain-trip TRP-0001
    PYTHONPATH=src python3 -m road_permits.cli --db ledger.db trips --status frozen
    PYTHONPATH=src python3 -m road_permits.cli --db ledger.db permits --vehicle VEH-0001
    PYTHONPATH=src python3 -m road_permits.cli --db ledger.db events --entity-type trip --entity-id TRP-0001
    PYTHONPATH=src python3 -m road_permits.cli --db ledger.db sweep

## 模块结构

| 模块 | 职责 |
| --- | --- |
| `road_permits/domain.py` | 状态枚举、时间工具、每日窗口判定（含跨日窗口） |
| `road_permits/store.py` | SQLite 台账：表结构、事务、序列号、仅追加审计事件 |
| `road_permits/services.py` | 全部业务规则：申请、分级审批、许可铸版、出车决策、冻结、清理、解释 |
| `road_permits/api.py` | HTTP API（标准库实现），启动钩子执行到期清理 |
| `road_permits/cli.py` | 操作员命令行接口 |
| `road_permits/demo.py` | 内存台账端到端演示 |

时钟通过 `clock` 注入服务层，测试用固定时钟，重启清理用真实时钟，行为可复现。

## 核心业务规则

- **测试申请与查重**：同一企业、同一车辆、同一路段，只要存在有效期重叠的
  进行中申请即判重复（409，并指明冲突申请）；被驳回或已审结后可重新提交。
- **分级审批**：按路段风险等级逐级审批——低危 1 级、中危 2 级、高危 3 级；
  不得跳级、同一审批人不得审两级；任一级驳回即终结。最后一级通过时铸造许可。
- **许可版本**：许可按（车辆 × 路段）递增铸版；新版本生效时旧版本转为
  `superseded`，其名下未执行的出车安排被冻结，已完成记录保持 `completed`。
- **每日测试窗口**：支持普通窗口（09:00–17:00）、跨日窗口（22:00–02:00）与
  全天窗口；出车区间必须完整落在窗口内，按"禁区相交"算法判定。
- **出车决策引擎**：调度时同步检查许可状态、计划时间、有效期覆盖、每日窗口、
  路段开放、车辆资质、驾驶员授权、替代车辆覆盖，全部通过才获准；被拒同样
  留档，结构化原因可查询。
- **临时替代车辆**：替代车须属同一企业、资质覆盖出车区间，且自身持有同路段
  有效许可（决策记录注明覆盖许可编号）。
- **冻结与撤销**：事故报告冻结该车全部有效许可与未来出车（结案可解冻许可，
  已冻结出车不自动复活）；路线变更作废旧版许可并为新路段生成待审批申请；
  资质/授权撤回为终态撤销。冻结只作用于未执行的安排。
- **到期清理**：服务启动时（也可手动触发）把过期许可、未执行出车、搁置申请、
  到期资质与授权归档为 `expired`，幂等，重启多次结果一致。
- **可解释性**：`GET /trips/{id}/explain` 返回决策明细（逐项检查与失败原因）、
  许可快照与相关审计事件，并生成中文结论；CLI `explain-trip` 输出同等内容。

## HTTP API 一览

| 方法与路径 | 说明 |
| --- | --- |
| `POST /registry/enterprises` `/vehicles` `/drivers` `/segments` | 登记企业、车辆、驾驶员、路段 |
| `POST /applications` | 提交测试申请（重复返回 409） |
| `GET /applications/{id}` | 查看申请与所需审批级别 |
| `POST /applications/{id}/decisions` | 分级审批 `{level, approver, decision, reason}` |
| `GET /permits` | 许可版本查询（`vehicle_id`、`status` 过滤） |
| `POST /permits/{id}/route-changes` | 路线变更：作废旧版、冻结后续、生成新申请 |
| `POST /trips` | 调度出车，同步返回获准/被拒及原因 |
| `POST /trips/{id}/completion` | 登记测试完成 |
| `GET /trips/{id}/explain` | 解释出车为何获准、被拒或被冻结 |
| `GET /trips` | 出车安排查询（`status`、`vehicle_id` 过滤） |
| `POST /incidents` / `POST /incidents/{id}/resolution` | 事故报告与结案 |
| `POST /vehicles/{id}/qualification-revocation` | 车辆资质撤回 |
| `POST /drivers/{id}/revocation` | 驾驶员授权撤回 |
| `POST /admin/sweeps` | 手动执行到期清理 |
| `GET /events` | 审计事件查询 |

错误统一为 `{"error": code, "message": ...}`；`not_found`→404，
`duplicate`/`invalid_state`→409，`validation`→400。

## 需求与验证对照

| 需求 | 验证位置 |
| --- | --- |
| 重复申请可验证 | `tests/test_services.py::DuplicateApplicationTests` |
| 跨日窗口 | `tests/test_window.py`、`TripDecisionTests` |
| 临时替代车辆 | `tests/test_services.py::SubstituteVehicleTests` |
| 分级审批 | `tests/test_services.py::TieredApprovalTests` |
| 服务重启后的到期清理 | `tests/test_restart.py` |
| 冻结不篡改已完成记录 | `tests/test_services.py::FreezeTests` |
| 获准/被拒/被撤销可解释 | `tests/test_services.py::ExplainTests`、`tests/test_api.py` |
