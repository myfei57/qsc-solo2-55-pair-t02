"""检修作业避让。

按检修作业的**时间段**（武装/解防）与推进到的**实际位置**（几何判据）自动
算出会被镜斑扫到的定日镜，提前让它们退出聚焦，作业走开再放回。

口径速览（详见 :mod:`cspplant.maintenance.sweep`）：

* 几何依据只有实际位置；时间窗只决定作业票是否武装。
* 位置变化立即重算还是下一轮生效，由作业类型
  （:data:`~cspplant.maintenance.job.HOT_WORK` /
  :data:`~cspplant.maintenance.job.ROUTINE`）决定。
* 先退后回、严格有序；放回需镜斑离开 + 作业正常完成，中断按未完成维持退出。
* 手动接管的镜子不被动，只登记告警交人工处置。
* 每次退出/放回的几何依据与审计、控制台展示同源。
"""

from __future__ import annotations

from .controller import MaintenanceController, SyncOutcome
from .footprint import BeamSweepModel, SweepConfig, SweepHit
from .job import (
    ACTIVE,
    COMPLETED,
    INTERRUPTED,
    ROUTINE,
    SCHEDULED,
    HOT_WORK,
    JobPosition,
    MaintenanceJob,
)
from .persist import MaintenancePersistence
from .registry import JobRegistry
from .sweep import AvoidancePlan, AvoidancePlanner, JobBasis

__all__ = [
    "ACTIVE",
    "SCHEDULED",
    "COMPLETED",
    "INTERRUPTED",
    "HOT_WORK",
    "ROUTINE",
    "AvoidancePlan",
    "AvoidancePlanner",
    "BeamSweepModel",
    "JobBasis",
    "JobPosition",
    "JobRegistry",
    "MaintenanceController",
    "MaintenanceJob",
    "MaintenancePersistence",
    "SweepConfig",
    "SweepHit",
    "SyncOutcome",
]
