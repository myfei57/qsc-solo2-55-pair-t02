"""检修避让持久化。

跨重启要保住两样东西：在役（含中断）的作业票，以及当前正由检修避让持有的
镜子集合。重启后控制器据此把镜子**继续压在退出姿态**——绝不能因为进程重启
就把还在干活区域上方的镜子放回追日。
"""

from __future__ import annotations

from ..store.jsonfile import JsonStore
from .job import (
    ACTIVE,
    COMPLETED,
    INTERRUPTED,
    JOB_STATES,
    ROUTINE,
    MaintenanceJob,
)

MAINTENANCE_DOC = "maintenance/active"


class MaintenancePersistence:
    """在役作业票与持有镜子的读写入口。"""

    def __init__(self, store: JsonStore) -> None:
        self._store = store

    @property
    def store(self) -> JsonStore:
        return self._store

    def write(self, jobs: list[MaintenanceJob], held: list[str] | tuple[str, ...]) -> str:
        """落盘未收尾的作业票与当前持有镜子。"""

        payload = {
            "jobs": [self._dump_job(job) for job in jobs if job.unfinished],
            "held": sorted(set(held)),
        }
        return self._store.write(MAINTENANCE_DOC, payload)

    def read(self) -> dict | None:
        """读回在役作业与持有镜子；没有文档返回 ``None``。"""

        raw = self._store.read(MAINTENANCE_DOC)
        if not isinstance(raw, dict):
            return None
        jobs_raw = raw.get("jobs")
        held_raw = raw.get("held")
        if not isinstance(jobs_raw, list) or not isinstance(held_raw, list):
            return None
        jobs: list[MaintenanceJob] = []
        for item in jobs_raw:
            job = self._load_job(item)
            if job is not None:
                jobs.append(job)
        return {"jobs": jobs, "held": [str(name) for name in held_raw]}

    def clear(self) -> bool:
        """删除检修文档。"""

        return self._store.remove(MAINTENANCE_DOC)

    def _dump_job(self, job: MaintenanceJob) -> dict:
        return {
            "job_id": job.job_id,
            "kind": job.kind,
            "zone": job.zone,
            "scheduled_start": job.scheduled_start,
            "scheduled_end": job.scheduled_end,
            "radius_m": job.radius_m,
            "start_x_m": job.start_x_m,
            "start_y_m": job.start_y_m,
            "x_m": job.x_m,
            "y_m": job.y_m,
            "reported_at": job.reported_at,
            "state": job.state,
            "completed_at": job.completed_at,
        }

    def _load_job(self, raw: object) -> MaintenanceJob | None:
        if not isinstance(raw, dict):
            return None
        try:
            state = str(raw.get("state", ACTIVE))
            if state not in JOB_STATES or state == COMPLETED:
                state = INTERRUPTED  # 落盘时不应有完成票；异常数据按中断（保守）处理。
            kind = str(raw.get("kind", ROUTINE))
            if kind not in ("hot_work", "routine"):
                kind = ROUTINE
            return MaintenanceJob(
                job_id=str(raw["job_id"]),
                kind=kind,
                zone=str(raw.get("zone", "maintenance")),
                scheduled_start=float(raw.get("scheduled_start", 0.0)),
                scheduled_end=float(raw.get("scheduled_end", 0.0)),
                radius_m=float(raw.get("radius_m", 1.0)),
                start_x_m=float(raw.get("start_x_m", 0.0)),
                start_y_m=float(raw.get("start_y_m", 0.0)),
                x_m=float(raw.get("x_m", 0.0)),
                y_m=float(raw.get("y_m", 0.0)),
                reported_at=float(raw.get("reported_at", 0.0)),
                state=state,
                completed_at=None
                if raw.get("completed_at") is None
                else float(raw["completed_at"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
