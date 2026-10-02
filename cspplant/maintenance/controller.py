"""检修避让协调器。

它把作业台账、几何规划、现场执行与落盘串成一条链，并决定两件时序上的事：

* **位置变化何时生效由作业类型决定。** 动火类（``hot_work``）一上报位置就
  立即重算并退出；常规类（``routine``）只登记位置，等下一个
  :meth:`run_cycle` 统一重算。哪种类型走哪条路在
  :data:`~cspplant.maintenance.job.REPOSITION_POLICY` 里钉死。
* **退出与放回严格有序。** 每轮先执行新增退出（先把镜子压到安全姿态），确认
  到位后再执行放回（解除避让标记，由下一轮追日重新对焦）；放回集合在规划阶段
  就保证与退出集合不相交。

现场怎么转镜子、怎么落审计，由构造时注入的执行器/事件回调完成，避免
``maintenance`` 反向依赖 ``field``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from ..heliostat.sun import SunModel
from .footprint import BeamSweepModel
from .persist import MaintenancePersistence
from .registry import JobRegistry
from .sweep import AvoidancePlan, AvoidancePlanner

# 事件回调：(action, severity, message, payload)。
EventSink = Callable[[str, str, str, dict], None]


class MaintenanceActuator(Protocol):
    """协调器需要镜场提供的最小执行接口。"""

    def exit_focus(self, mirrors: list[str], job_id: str, zone: str, basis: dict) -> dict: ...

    def return_focus(self, mirrors: list[str], job_ids: list[str]) -> dict: ...

    def held_maintenance(self) -> list[str]: ...

    def manual_mirrors(self) -> list[str]: ...


@dataclass(frozen=True)
class SyncOutcome:
    """一轮避让同步的执行结果（供编排与审计）。"""

    plan: AvoidancePlan
    exits_report: dict
    returns_report: dict

    @property
    def ok(self) -> bool:
        return bool(self.exits_report.get("ok", True)) and bool(
            self.returns_report.get("ok", True)
        )

    @property
    def partial(self) -> bool:
        """是否属于部分失败（有镜子没退出/没放回到位）。"""

        return not self.ok

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "partial": self.partial,
            "plan": self.plan.as_dict(),
            "exits": self.exits_report,
            "returns": self.returns_report,
        }


class MaintenanceController:
    """检修作业避让协调器。"""

    def __init__(
        self,
        actuator: MaintenanceActuator,
        sun_model: SunModel,
        sweep_model: BeamSweepModel,
        persistence: MaintenancePersistence,
        *,
        position_stale_after_s: float = 60.0,
        event_sink: EventSink | None = None,
    ) -> None:
        self._actuator = actuator
        self._sun = sun_model
        self._model = sweep_model
        self._store = persistence
        self._planner = AvoidancePlanner(sweep_model, position_stale_after_s=position_stale_after_s)
        self._jobs = JobRegistry()
        self._sink = event_sink
        self._held: set[str] = set()
        # 最近一次用于太阳几何的当日分钟；几何判定（太阳分钟）与作业时间窗
        # （平台秒）是两条独立时间轴，太阳分钟由调用方按 sun_at 口径显式给出。
        self._minute: float | None = None
        # 每张票由检修避让退出的镜子（放回时用；与规划器持有集合分开记账）。
        self._job_mirrors: dict[str, set[str]] = {}
        # 几何上应退出、但因手动接管暂未被动执行的镜子。它们释放后要补退出。
        self._blocked: set[str] = set()

    # ---- 属性 ---------------------------------------------------------

    @property
    def jobs(self) -> JobRegistry:
        return self._jobs

    def held(self) -> list[str]:
        """返回当前由检修避让持有的镜子。"""

        return sorted(self._held)

    # ---- 事件 ---------------------------------------------------------

    def _emit(self, action: str, severity: str, message: str, **payload: object) -> None:
        if self._sink is not None:
            self._sink(action, severity, message, dict(payload))

    # ---- 作业生命周期 -------------------------------------------------

    def open_job(
        self,
        job_id: str,
        *,
        scheduled_start: float,
        scheduled_end: float,
        start_x_m: float,
        start_y_m: float,
        radius_m: float,
        zone: str = "maintenance",
        kind: str = "routine",
        now: float,
        minute_of_day: float | None = None,
    ) -> MaintenanceJob:
        """开一张检修作业票。"""

        if minute_of_day is not None:
            self._minute = float(minute_of_day)
        job = self._jobs.open(
            job_id,
            scheduled_start=scheduled_start,
            scheduled_end=scheduled_end,
            start_x_m=start_x_m,
            start_y_m=start_y_m,
            radius_m=radius_m,
            zone=zone,
            kind=kind,
            now=now,
        )
        self._job_mirrors.setdefault(job_id, set())
        self._emit(
            "maintenance_job_opened",
            "notice",
            f"检修作业 {job_id} 已开票（{kind}）",
            job=job.as_dict(),
        )
        # 开票即落在窗内（已武装）时，立即算一轮，抢出提前量。
        if job.state == "active":
            self.run_cycle(now)
        return job

    def report_position(
        self,
        job_id: str,
        x_m: float,
        y_m: float,
        now: float,
        minute_of_day: float | None = None,
    ) -> MaintenanceJob:
        """上报作业推进到的实际位置。

        动火类立即重算并退出；常规类只登记，等下一轮 :meth:`run_cycle` 生效。
        """

        if minute_of_day is not None:
            self._minute = float(minute_of_day)
        job = self._jobs.report_position(job_id, x_m, y_m, now)
        self._emit(
            "maintenance_position",
            "notice",
            f"作业 {job_id} 推进到 ({x_m:.1f}, {y_m:.1f})",
            job_id=job_id,
            x_m=round(x_m, 3),
            y_m=round(y_m, 3),
            immediate=job.immediate_reposition,
        )
        if job.immediate_reposition:
            self.run_cycle(now, reason="immediate_reposition")
        return job

    def complete_job(
        self, job_id: str, now: float, minute_of_day: float | None = None
    ) -> MaintenanceJob:
        """正常完成作业；镜子在本轮求值确认镜斑离开后才放回。"""

        if minute_of_day is not None:
            self._minute = float(minute_of_day)
        job = self._jobs.complete(job_id, now)
        self._emit(
            "maintenance_job_completed",
            "notice",
            f"作业 {job_id} 正常完成",
            job_id=job_id,
        )
        self.run_cycle(now, reason="job_completed")
        return job

    def interrupt_job(
        self, job_id: str, now: float, minute_of_day: float | None = None
    ) -> MaintenanceJob:
        """中断作业——按未完成处理，镜子维持退出，绝不自动放回。"""

        if minute_of_day is not None:
            self._minute = float(minute_of_day)
        job = self._jobs.interrupt(job_id, now)
        self._emit(
            "maintenance_job_interrupted",
            "warning",
            f"作业 {job_id} 中断，按未完成处理，镜子维持退出",
            job_id=job_id,
            position=job.position().as_dict(),
        )
        self.run_cycle(now, reason="job_interrupted")
        return job

    def resume_job(
        self, job_id: str, now: float, minute_of_day: float | None = None
    ) -> MaintenanceJob:
        """把中断作业重新置为在役。"""

        if minute_of_day is not None:
            self._minute = float(minute_of_day)
        job = self._jobs.reopen(job_id, now)
        self._emit(
            "maintenance_job_resumed",
            "notice",
            f"作业 {job_id} 恢复",
            job_id=job_id,
        )
        self.run_cycle(now, reason="job_resumed")
        return job

    # ---- 周期求值与执行 ----------------------------------------------

    def run_cycle(
        self,
        now: float,
        *,
        reason: str = "cycle",
        minute_of_day: float | None = None,
    ) -> SyncOutcome:
        """推进一个避让周期：武装到期票 → 求值 → 先退后回 → 落盘。"""

        if minute_of_day is not None:
            self._minute = float(minute_of_day)
        minute = 720.0 if self._minute is None else self._minute
        self._jobs.arm_due(now)
        held = frozenset(self._actuator.held_maintenance())
        manual = frozenset(self._actuator.manual_mirrors())
        plan = self._planner.evaluate(
            self._jobs.all(),
            now=now,
            minute_of_day=minute,
            held=held,
            manual=manual,
        )

        # 先退出、后放回。待退出集合 = 本轮新增退出 + 上一轮因手动被挡、这一轮
        # 已不再手动且仍需避让（几何并集仍含它）的镜子——手动释放后必须补退出。
        newly_unblocked = {
            name for name in self._blocked if name in plan.needed and name not in manual
        }
        pending_exits = set(plan.exits) | newly_unblocked
        exits_report: dict = {"ok": True, "mirrors": [], "failed": []}
        if pending_exits:
            zone = self._zone_for(plan.basis)
            exits_report = self._actuator.exit_focus(
                sorted(pending_exits), self._primary_job_id(plan), zone, plan.as_dict()
            )
            # 执行器会把仍在手动的剔到 blocked；据此更新记账。
            blocked_now = set(exits_report.get("blocked", []))
            for name in pending_exits:
                if name in blocked_now:
                    self._blocked.add(name)
                else:
                    self._blocked.discard(name)
                    self._held.add(name)
            self._retag_mirrors(plan)
            self._emit(
                "maintenance_exit",
                "warning" if exits_report.get("failed") else "notice",
                f"检修避让退出 {len(pending_exits)} 面镜子",
                mirrors=sorted(pending_exits - blocked_now),
                failed=list(exits_report.get("failed", [])),
                reason=reason,
                basis=plan.as_dict(),
            )

        # 本轮几何新发现、但因手动接管不能动的镜子：登记到 _blocked 并告警，
        # 同时计入持有与票账本，释放后由上面的 newly_unblocked 补退出。
        fresh_blocked = set(plan.manual_blocked) - newly_unblocked
        if fresh_blocked:
            self._blocked |= fresh_blocked
            for name in fresh_blocked:
                self._held.add(name)
            self._retag_mirrors(plan)
            self._emit(
                "maintenance_manual_blocked",
                "alarm",
                f"{len(fresh_blocked)} 面手动接管镜位于光斑路径，自动避让无法退出，需人工处置",
                mirrors=sorted(fresh_blocked),
                basis=plan.as_dict(),
            )
        # 之前被挡、现在几何上已不需要的镜子，从待办里清掉。
        self._blocked &= plan.needed

        if plan.stale_jobs:
            self._emit(
                "maintenance_position_stale",
                "alarm",
                f"作业 {list(plan.stale_jobs)} 位置过期，按危险维持退出",
                jobs=list(plan.stale_jobs),
            )

        returns_report: dict = {"ok": True, "mirrors": [], "failed": []}
        if plan.returns:
            returns_report = self._actuator.return_focus(
                sorted(plan.returns), list(plan.completed_job_ids)
            )
            for name in plan.returns:
                self._held.discard(name)
            self._untag_mirrors(plan.returns)
            self._emit(
                "maintenance_return",
                "warning" if returns_report.get("failed") else "notice",
                f"光斑离开后放回 {len(plan.returns)} 面镜子",
                mirrors=sorted(plan.returns),
                completed_jobs=list(plan.completed_job_ids),
            )

        outcome = SyncOutcome(plan, exits_report, returns_report)
        if exits_report.get("failed") or returns_report.get("failed"):
            self._emit(
                "maintenance_partial_failure",
                "alarm",
                "检修避让存在未到位镜子，按部分失败/未完成处理",
                exit_failed=list(exits_report.get("failed", [])),
                return_failed=list(returns_report.get("failed", [])),
            )
        self._persist()
        return outcome

    # ---- 记账与落盘 ---------------------------------------------------

    def _retag_mirrors(self, plan: AvoidancePlan) -> None:
        for basis in plan.basis:
            bucket = self._job_mirrors.setdefault(basis.job_id, set())
            bucket.update(basis.swept)

    def _untag_mirrors(self, mirrors: frozenset[str]) -> None:
        for name in mirrors:
            for bucket in self._job_mirrors.values():
                bucket.discard(name)

    def _zone_for(self, bases: tuple) -> str:
        for basis in bases:
            if basis.state == "active":
                return self._jobs.get(basis.job_id).zone
        return "maintenance"

    def _primary_job_id(self, plan: AvoidancePlan) -> str:
        for basis in plan.basis:
            if basis.state == "active" and basis.swept:
                return basis.job_id
        return plan.basis[0].job_id if plan.basis else ""

    def _persist(self) -> None:
        self._store.write(self._jobs.unfinished(), self._held)

    def restore(self, now: float | None = None) -> dict:
        """重启后恢复在役作业与持有镜子，并把镜子重新压回退出姿态。"""

        stored = self._store.read()
        if stored is None:
            return {"jobs": 0, "held": 0}
        known = {job.job_id for job in self._jobs.all()}
        manual = set(self._actuator.manual_mirrors())
        for job in stored["jobs"]:
            if job.job_id not in known:
                self._jobs.restore_job(job)
            self._job_mirrors.setdefault(job.job_id, set())
            self._planner.restore_job(job.job_id, stored["held"])

        all_held = stored["held"]
        actuated = [name for name in all_held if name not in manual]
        self._held.update(all_held)
        if actuated:
            report = self._actuator.exit_focus(
                sorted(actuated), "__restored__", "maintenance", {"reason": "restart_restore"}
            )
            self._emit(
                "maintenance_restored",
                "warning" if report.get("failed") else "notice",
                f"重启恢复：{len(actuated)} 面镜子重新压回退出姿态",
                mirrors=sorted(actuated),
                failed=list(report.get("failed", [])),
            )
        blocked = sorted(set(all_held) & manual)
        if blocked:
            self._emit(
                "maintenance_manual_blocked",
                "alarm",
                "重启恢复时部分应退出镜子处于手动接管，需人工处置",
                mirrors=blocked,
            )
        return {"jobs": len(stored["jobs"]), "held": len(all_held)}

    # ---- 查询 ---------------------------------------------------------

    def status(self) -> dict:
        """汇总检修避让状态，供控制台展示。"""

        return {
            "jobs": [job.as_dict() for job in self._jobs.all()],
            "active": [job.job_id for job in self._jobs.active()],
            "unfinished": [job.job_id for job in self._jobs.unfinished()],
            "held": sorted(self._held),
            "held_count": len(self._held),
            "sweep": self._model.config.as_dict(),
            "job_mirrors": {
                job_id: sorted(names) for job_id, names in sorted(self._job_mirrors.items())
            },
        }
