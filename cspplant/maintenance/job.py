"""检修作业票与作业类型。

作业票把"一段时间 + 一个会移动的现场位置 + 一种作业类型"绑在一起。避让是否
生效看两张表：

* 时间窗（``scheduled_start``/``scheduled_end``）只决定作业在当前时刻是否被
  "武装"——窗口外的票不产生任何避让动作；
* 真正决定哪些镜子退出聚焦的几何依据，只有作业**推进到的实际位置**
  （``x_m``/``y_m``）。时间窗从不参与几何判定，这条口径在
  :mod:`cspplant.maintenance.sweep` 里被钉死。

作业类型决定位置变化后"立即重算还是下一轮生效"：

* :data:`HOT_WORK`（动火、高空、带电这类人就在光斑可能扫到的路径上的作业）
  位置一报上来立即重算并退出，慢一拍就可能晃到人；
* :data:`ROUTINE`（巡检、常规点检这类按轮次推进的作业）位置变化只登记，
  等下一个避让周期统一重算，避免追着抖动的定位反复进退。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import InterlockError

# 作业状态：已排程（窗口未到，未武装）→ 在役 → 正常完成 / 中断。
SCHEDULED = "scheduled"
ACTIVE = "active"
COMPLETED = "completed"
INTERRUPTED = "interrupted"

# 作业类型。
HOT_WORK = "hot_work"
ROUTINE = "routine"

# 作业类型 → 位置变化后是否立即重算并退出。
REPOSITION_POLICY: dict[str, bool] = {
    HOT_WORK: True,
    ROUTINE: False,
}

JOB_TYPES = tuple(REPOSITION_POLICY)
JOB_STATES = (SCHEDULED, ACTIVE, COMPLETED, INTERRUPTED)


@dataclass(frozen=True)
class JobPosition:
    """作业在某一时刻推进到的实际位置。"""

    x_m: float
    y_m: float
    reported_at: float
    stale: bool = False

    def as_dict(self) -> dict:
        return {
            "x_m": round(self.x_m, 3),
            "y_m": round(self.y_m, 3),
            "reported_at": round(self.reported_at, 3),
            "stale": self.stale,
        }


@dataclass(frozen=True)
class MaintenanceJob:
    """一张检修作业票（不可变；推进用 :meth:`moved` 派生新票）。"""

    job_id: str
    kind: str
    zone: str
    scheduled_start: float
    scheduled_end: float
    radius_m: float
    start_x_m: float
    start_y_m: float
    x_m: float
    y_m: float
    reported_at: float
    state: str = SCHEDULED
    completed_at: float | None = None
    history: tuple[JobPosition, ...] = field(default_factory=tuple)

    @property
    def immediate_reposition(self) -> bool:
        """本作业位置变化后是否必须立即重算并退出。"""

        return REPOSITION_POLICY[self.kind]

    @property
    def open(self) -> bool:
        """作业是否仍未收尾（在役；中断也算未完成，见 :meth:`unfinished`）。"""

        return self.state == ACTIVE

    @property
    def unfinished(self) -> bool:
        """作业是否按未完成处理（中断与在役都算；正常完成为假）。"""

        return self.state in (ACTIVE, INTERRUPTED)

    def armed(self, now: float) -> bool:
        """作业在给定时刻是否处于时间窗内（窗口只负责武装，不参与几何判定）。"""

        return float(self.scheduled_start) <= float(now) < float(self.scheduled_end)

    def position(self) -> JobPosition:
        """返回作业当前的实际位置。"""

        return JobPosition(self.x_m, self.y_m, self.reported_at)

    def moved(self, x_m: float, y_m: float, now: float) -> "MaintenanceJob":
        """派生出一张推进到新实际位置的作业票，并追加轨迹。"""

        history = self.history + (JobPosition(float(x_m), float(y_m), float(now)),)
        return self._replace(x_m=float(x_m), y_m=float(y_m), reported_at=float(now), history=history)

    def as_state(self, state: str, now: float | None = None) -> "MaintenanceJob":
        """派生一张切换到指定状态的作业票。"""

        if state not in JOB_STATES:
            raise InterlockError(f"未知作业状态: {state}", component="maintenance")
        completed_at = self.completed_at
        if state in (COMPLETED, INTERRUPTED):
            completed_at = float(now) if now is not None else self.reported_at
        return self._replace(state=state, completed_at=completed_at)

    def _replace(self, **changes: object) -> "MaintenanceJob":
        import dataclasses

        return dataclasses.replace(self, **changes)

    def as_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "zone": self.zone,
            "scheduled_start": round(self.scheduled_start, 3),
            "scheduled_end": round(self.scheduled_end, 3),
            "radius_m": round(self.radius_m, 3),
            "state": self.state,
            "immediate_reposition": self.immediate_reposition,
            "position": self.position().as_dict(),
            "start": {"x_m": round(self.start_x_m, 3), "y_m": round(self.start_y_m, 3)},
            "completed_at": None if self.completed_at is None else round(self.completed_at, 3),
            "track": [point.as_dict() for point in self.history],
        }
