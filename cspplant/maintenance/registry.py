"""检修作业票台账。

台账只管票的生命周期：开票（排程）、按时间窗把票置为在役、上报实际位置、
正常完成、作业中断。它**不算**哪些镜子要退出——几何判定在
:mod:`cspplant.maintenance.sweep`，台账只保证"位置从哪来、票现在什么状态"
这两件事有唯一出处，审计与控制台都从这里取同一份数据。
"""

from __future__ import annotations

from ..errors import InterlockError
from .job import (
    ACTIVE,
    COMPLETED,
    INTERRUPTED,
    JOB_TYPES,
    ROUTINE,
    SCHEDULED,
    MaintenanceJob,
)


class JobRegistry:
    """检修作业票台账。"""

    def __init__(self) -> None:
        self._jobs: dict[str, MaintenanceJob] = {}

    def open(
        self,
        job_id: str,
        *,
        scheduled_start: float,
        scheduled_end: float,
        start_x_m: float,
        start_y_m: float,
        radius_m: float,
        zone: str = "maintenance",
        kind: str = ROUTINE,
        now: float | None = None,
    ) -> MaintenanceJob:
        """开一张作业票。时间窗非法、半径非正或重名都拒绝。"""

        if not job_id:
            raise InterlockError("作业票必须有编号", component="maintenance")
        if job_id in self._jobs:
            raise InterlockError(f"作业票已存在: {job_id}", component="maintenance")
        if kind not in JOB_TYPES:
            raise InterlockError(f"未知作业类型: {kind}", component="maintenance")
        if float(radius_m) <= 0.0:
            raise InterlockError("作业避让半径必须为正", component="maintenance")
        if float(scheduled_end) <= float(scheduled_start):
            raise InterlockError("作业结束时刻必须晚于开始时刻", component="maintenance")
        if not zone:
            raise InterlockError("作业票必须绑定避让区", component="maintenance")
        moment = float(now) if now is not None else float(scheduled_start)
        # 开票时若已经落在时间窗内，直接置为在役；否则等 arm() 武装。
        armed_now = float(scheduled_start) <= moment < float(scheduled_end)
        job = MaintenanceJob(
            job_id=str(job_id),
            kind=kind,
            zone=str(zone),
            scheduled_start=float(scheduled_start),
            scheduled_end=float(scheduled_end),
            radius_m=float(radius_m),
            start_x_m=float(start_x_m),
            start_y_m=float(start_y_m),
            x_m=float(start_x_m),
            y_m=float(start_y_m),
            reported_at=moment,
            state=ACTIVE if armed_now else SCHEDULED,
        )
        self._jobs[job_id] = job
        return job

    def restore_job(self, job: MaintenanceJob) -> MaintenanceJob:
        """重启路径：把一张跨重启仍在役/中断的作业票直接灌回台账。"""

        self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> MaintenanceJob:
        """取一张作业票，不存在即报错。"""

        try:
            return self._jobs[job_id]
        except KeyError as error:
            raise InterlockError(f"未登记作业票: {job_id}", component="maintenance") from error

    def _put(self, job: MaintenanceJob) -> MaintenanceJob:
        self._jobs[job.job_id] = job
        return job

    def arm_due(self, now: float) -> list[MaintenanceJob]:
        """把已到开始时刻的排程票置为在役，返回本次武装的票。"""

        armed: list[MaintenanceJob] = []
        for job in self._jobs.values():
            if job.state == SCHEDULED and job.armed(now):
                armed.append(self._put(job.as_state(ACTIVE, now)))
        return armed

    def report_position(self, job_id: str, x_m: float, y_m: float, now: float) -> MaintenanceJob:
        """上报作业推进到的实际位置（不改变状态）。"""

        job = self.get(job_id)
        if not job.open:
            raise InterlockError(f"作业 {job_id} 不在役，不能上报位置", component="maintenance")
        return self._put(job.moved(x_m, y_m, now))

    def complete(self, job_id: str, now: float) -> MaintenanceJob:
        """正常完成作业。"""

        job = self.get(job_id)
        if not job.open:
            raise InterlockError(f"作业 {job_id} 不在役，不能完成", component="maintenance")
        return self._put(job.as_state(COMPLETED, now))

    def interrupt(self, job_id: str, now: float) -> MaintenanceJob:
        """中断作业——按未完成处理，镜子不得自动放回。"""

        job = self.get(job_id)
        if not job.open:
            raise InterlockError(f"作业 {job_id} 不在役，不能中断", component="maintenance")
        return self._put(job.as_state(INTERRUPTED, now))

    def reopen(self, job_id: str, now: float) -> MaintenanceJob:
        """把中断的作业重新置为在役（例如处置完继续干）。"""

        job = self.get(job_id)
        if job.state != INTERRUPTED:
            raise InterlockError(f"作业 {job_id} 未中断，不能恢复", component="maintenance")
        return self._put(job.as_state(ACTIVE, now))

    def active(self) -> list[MaintenanceJob]:
        """返回当前在役的作业票（按编号排序）。"""

        return [self._jobs[j] for j in sorted(self._jobs) if self._jobs[j].state == ACTIVE]

    def unfinished(self) -> list[MaintenanceJob]:
        """返回在役与中断的作业票（按编号排序）。"""

        return [self._jobs[j] for j in sorted(self._jobs) if self._jobs[j].unfinished]

    def all(self) -> list[MaintenanceJob]:
        """返回全部作业票（按编号排序）。"""

        return [self._jobs[j] for j in sorted(self._jobs)]
