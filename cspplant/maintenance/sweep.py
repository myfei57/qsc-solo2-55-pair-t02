"""检修避让规划器：算镜子、定时序、留依据。

它把需求里最容易出事的几条钉成不可绕过的规则：

1. **几何依据只有实际位置。** 时间窗只在 :class:`~cspplant.maintenance.registry.JobRegistry`
   里决定一张票是否被武装成在役；规划器对在役票只按其推进到的实际位置
   （+ 提前量内的太阳轨迹与人的可能移动）算镜斑。没有任何"到点就放行"的分支。
2. **退出先于放回，且任何一张票需要就不回。** 目标退出集合是全部在役票所需
   镜子的并集；先算出本轮新增退出（``exits``），再从"上一轮持有、这一轮并集
   不再需要、且作业正常收尾"的镜子里算放回（``returns``）。同一面镜子不会在
   一轮里又退又回。
3. **放回是保守方向，三道闸全开才放。** 镜斑在整个提前量窗口外（并集不再含
   它）、危险盘外扩余量仍有富余、且占用它的作业**正常完成**（中断不算）。
4. **位置过期/未知按危险处理。** 该票沿用上一轮算出的镜子集合，只增不减，并
   在依据里标 ``stale``，触发告警；绝不因为"收不到新位置"就把镜子放回来。
5. **手动接管的镜子不被动。** 自动避让绝不下发退出指令，只把它们列入
   ``manual_blocked`` 交人工处置并告警；它们也不计入"已安全退出"。
6. **每次退出的几何证据原样进审计。** :class:`AvoidancePlan.basis` 是不可变
   依据，控制台展示与审计落盘用的是同一个对象，杜绝"退的是一套、记的是另一套"。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .footprint import BeamSweepModel, SweepHit
from .job import ACTIVE, COMPLETED, MaintenanceJob


@dataclass(frozen=True)
class JobBasis:
    """一张作业票在本轮避让判定里留下的完整依据。"""

    job_id: str
    kind: str
    state: str
    armed: bool
    position_stale: bool
    x_m: float
    y_m: float
    reported_at: float
    swept: tuple[str, ...]
    held_over: bool
    hits: dict[str, SweepHit] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "state": self.state,
            "armed": self.armed,
            "position_stale": self.position_stale,
            "x_m": round(self.x_m, 3),
            "y_m": round(self.y_m, 3),
            "reported_at": round(self.reported_at, 3),
            "held_over": self.held_over,
            "swept": list(self.swept),
            "hits": {name: hit.as_dict() for name, hit in self.hits.items()},
        }


@dataclass(frozen=True)
class AvoidancePlan:
    """一轮避让求值的不可变结果：执行集合 + 审计依据。"""

    now: float
    minute_of_day: float
    held: frozenset[str]
    needed: frozenset[str]
    exits: frozenset[str]
    returns: frozenset[str]
    manual_blocked: frozenset[str]
    stale_jobs: tuple[str, ...]
    completed_job_ids: tuple[str, ...]
    basis: tuple[JobBasis, ...]

    def as_dict(self) -> dict:
        return {
            "now": round(self.now, 3),
            "minute_of_day": round(self.minute_of_day, 3),
            "held": sorted(self.held),
            "needed": sorted(self.needed),
            "exits": sorted(self.exits),
            "returns": sorted(self.returns),
            "manual_blocked": sorted(self.manual_blocked),
            "stale_jobs": list(self.stale_jobs),
            "completed_jobs": list(self.completed_job_ids),
            "basis": [item.as_dict() for item in self.basis],
        }


class AvoidancePlanner:
    """按在役作业票的实际位置，每轮算出退出/放回集合与依据。"""

    def __init__(
        self,
        sweep_model: BeamSweepModel,
        *,
        position_stale_after_s: float = 60.0,
    ) -> None:
        if position_stale_after_s <= 0.0:
            raise ValueError("位置过期时间必须为正")
        self._model = sweep_model
        self._stale_after_s = float(position_stale_after_s)
        # 每张票上一轮算出并持有的镜子；位置过期时沿用，保证只增不减。
        self._last_swept: dict[str, frozenset[str]] = {}

    @property
    def config(self):
        return self._model.config

    def restore_job(self, job_id: str, mirrors: list[str] | tuple[str, ...]) -> None:
        """重启后把跨重启仍在避让的镜子灌回某张票的持有集合。"""

        self._last_swept[job_id] = frozenset(mirrors)

    def evaluate(
        self,
        jobs: list[MaintenanceJob],
        *,
        now: float,
        minute_of_day: float,
        held: frozenset[str],
        manual: frozenset[str],
    ) -> AvoidancePlan:
        """对全部作业票求一轮避让计划。

        ``held`` 是当前由检修避让持有的镜子（上一轮的并集），``manual`` 是当前
        处于手动接管的镜子（自动避让不得下发指令）。
        """

        wanted: dict[str, set[str]] = {}
        bases: list[JobBasis] = []
        stale_jobs: list[str] = []
        completed_ids: list[str] = []

        for job in jobs:
            stale = job.state == ACTIVE and (now - job.reported_at) > self._stale_after_s
            if job.state == COMPLETED:
                # 正常收尾：这张票不再占用镜子，清除其持有记录。
                self._last_swept.pop(job.job_id, None)
                completed_ids.append(job.job_id)
                bases.append(
                    JobBasis(
                        job_id=job.job_id,
                        kind=job.kind,
                        state=job.state,
                        armed=False,
                        position_stale=False,
                        x_m=job.x_m,
                        y_m=job.y_m,
                        reported_at=job.reported_at,
                        swept=(),
                        held_over=False,
                    )
                )
                continue

            if job.state != ACTIVE:
                # 中断或已排程未武装：不产生新几何；中断票沿用持有集合（见下）。
                held_over = job.job_id in self._last_swept
                if held_over:
                    wanted[job.job_id] = set(self._last_swept[job.job_id])
                bases.append(
                    JobBasis(
                        job_id=job.job_id,
                        kind=job.kind,
                        state=job.state,
                        armed=False,
                        position_stale=False,
                        x_m=job.x_m,
                        y_m=job.y_m,
                        reported_at=job.reported_at,
                        swept=tuple(sorted(self._last_swept.get(job.job_id, ()))),
                        held_over=held_over,
                    )
                )
                continue

            hits = self._model.swept_for(job, minute_of_day)
            swept = set(hits)
            previous = self._last_swept.get(job.job_id, frozenset())
            if stale:
                # 位置不可信：沿用上一轮集合并保留本轮几何证据仅供参考，
                # 实际占用按上一轮执行——收不到位置绝不松绑。
                stale_jobs.append(job.job_id)
                effective = set(previous)
            else:
                effective = swept
            self._last_swept[job.job_id] = frozenset(effective)
            wanted[job.job_id] = effective
            bases.append(
                JobBasis(
                    job_id=job.job_id,
                    kind=job.kind,
                    state=job.state,
                    armed=True,
                    position_stale=stale,
                    x_m=job.x_m,
                    y_m=job.y_m,
                    reported_at=job.reported_at,
                    swept=tuple(sorted(effective)),
                    held_over=False,
                    hits=hits,
                )
            )

        # 并集：任何一张在役/中断票需要的镜子都不允许放回。
        union_needed: set[str] = set()
        for mirrors in wanted.values():
            union_needed.update(mirrors)

        # 退出：本轮并集相对上一轮持有的新增。手动镜不下发，仅登记告警。
        exits_all = union_needed - set(held)
        exits = frozenset(name for name in exits_all if name not in manual)
        manual_blocked = frozenset(name for name in exits_all if name in manual)

        # 放回：上一轮持有、并集不再需要。正常完成票已从并集剔除；
        # 中断票仍在 wanted 里，故其镜子天然不会进入 returns——退出易、放回难。
        returns = frozenset(set(held) - union_needed - set(exits_all))

        new_held = frozenset((set(held) | union_needed) - set(returns))
        return AvoidancePlan(
            now=float(now),
            minute_of_day=float(minute_of_day),
            held=new_held,
            needed=frozenset(union_needed),
            exits=exits,
            returns=returns,
            manual_blocked=manual_blocked,
            stale_jobs=tuple(sorted(stale_jobs)),
            completed_job_ids=tuple(sorted(completed_ids)),
            basis=tuple(sorted(bases, key=lambda item: item.job_id)),
        )
