"""镜斑扫掠几何模型。

它回答唯一的几何问题：**一面正常追日的镜子，在未来一段提前量内，其镜斑会不
会落进作业所在的圆形危险区。**

口径与 :class:`~cspplant.heliostat.zone.ZoneGuard` 保持一致：镜斑是"镜位 +
镜面法向方位 + 作用距离"在地面上唯一确定的落点（见
:func:`~cspplant.heliostat.zone.ZoneGuard.landing_point`）。镜子追日时法向
平分入射（太阳）与出射（吸热器）方向，因此太阳一走，法向方位就跟着扫，地面
镜斑随之扫出一条弧。作业位置也会移动，所以未来 Δt 时刻的危险盘按
"人在 Δt 内可能走到的距离"外扩，把两条运动都算进去，宁可不漏。

这里只做几何判定，不读驱动状态、不发指令；时序与执行在
:class:`~cspplant.maintenance.sweep.AvoidancePlanner`。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..heliostat.aim import aim_angles_for, AimPoint
from ..heliostat.registry import Heliostat, HeliostatRegistry
from ..heliostat.sun import SunModel, SunPosition
from .job import MaintenanceJob


@dataclass(frozen=True)
class SweepHit:
    """一面镜子被判为会被扫到时留下的几何证据（审计依据）。"""

    mirror: str
    hit_at_offset_s: float
    spot_x_m: float
    spot_y_m: float
    distance_m: float
    danger_radius_m: float
    sun_azimuth_deg: float
    sun_elevation_deg: float

    def as_dict(self) -> dict:
        return {
            "mirror": self.mirror,
            "hit_at_offset_s": round(self.hit_at_offset_s, 3),
            "spot_x_m": round(self.spot_x_m, 3),
            "spot_y_m": round(self.spot_y_m, 3),
            "distance_m": round(self.distance_m, 3),
            "danger_radius_m": round(self.danger_radius_m, 3),
            "sun_azimuth_deg": round(self.sun_azimuth_deg, 3),
            "sun_elevation_deg": round(self.sun_elevation_deg, 3),
        }


@dataclass(frozen=True)
class SweepConfig:
    """扫掠判定参数。"""

    lead_s: float
    reach_m: float
    margin_m: float
    crew_speed_m_s: float
    sample_step_s: float
    max_crew_expansion_m: float = 30.0

    @classmethod
    def default(cls) -> "SweepConfig":
        return cls(
            lead_s=120.0,
            reach_m=24.0,
            margin_m=6.0,
            crew_speed_m_s=0.5,
            sample_step_s=15.0,
            max_crew_expansion_m=30.0,
        )

    def as_dict(self) -> dict:
        return {
            "lead_s": round(self.lead_s, 3),
            "reach_m": round(self.reach_m, 3),
            "margin_m": round(self.margin_m, 3),
            "crew_speed_m_s": round(self.crew_speed_m_s, 3),
            "sample_step_s": round(self.sample_step_s, 3),
            "max_crew_expansion_m": round(self.max_crew_expansion_m, 3),
        }


class BeamSweepModel:
    """按太阳轨迹与作业推进位置计算镜斑是否扫过危险区。"""

    def __init__(
        self,
        registry: HeliostatRegistry,
        aim_point: AimPoint,
        sun_model: SunModel,
        config: SweepConfig,
    ) -> None:
        self._registry = registry
        self._aim = aim_point
        self._sun = sun_model
        self._config = config

    @property
    def config(self) -> SweepConfig:
        return self._config

    def spot_for(self, mirror: Heliostat, sun: SunPosition) -> tuple[float, float]:
        """按追日法向方位与作用距离算镜斑地面落点。"""

        normal = aim_angles_for(mirror.x_m, mirror.y_m, mirror.z_m, sun, self._aim)
        radians = math.radians(normal.azimuth_deg)
        return (
            mirror.x_m + math.sin(radians) * self._config.reach_m,
            mirror.y_m + math.cos(radians) * self._config.reach_m,
        )

    def danger_radius(self, job: MaintenanceJob, offset_s: float) -> float:
        """未来 ``offset_s`` 时刻的危险盘半径。

        作业盘 + 固定余量 + 人在该提前量内可能走的距离（封顶，避免长提前量把
        整片镜场无界吞掉）。
        """

        walk = min(
            self._config.crew_speed_m_s * max(0.0, offset_s),
            self._config.max_crew_expansion_m,
        )
        return job.radius_m + self._config.margin_m + walk

    def check(
        self, mirror: Heliostat, job: MaintenanceJob, minute_of_day: float
    ) -> SweepHit | None:
        """判定一面镜子在提前量窗口内是否被作业扫到；命中返回证据，否则 ``None``。"""

        step = max(1.0, self._config.sample_step_s)
        horizon = max(0.0, self._config.lead_s)
        offsets: list[float] = [0.0]
        cursor = 0.0
        while cursor < horizon:
            cursor = min(horizon, cursor + step)
            offsets.append(cursor)
        for offset_s in offsets:
            minute = minute_of_day + offset_s / 60.0
            sun = self._sun.at(minute)
            if not sun.above_horizon:
                continue
            spot_x, spot_y = self.spot_for(mirror, sun)
            distance = math.hypot(spot_x - job.x_m, spot_y - job.y_m)
            radius = self.danger_radius(job, offset_s)
            if distance <= radius:
                return SweepHit(
                    mirror=mirror.name,
                    hit_at_offset_s=offset_s,
                    spot_x_m=spot_x,
                    spot_y_m=spot_y,
                    distance_m=distance,
                    danger_radius_m=radius,
                    sun_azimuth_deg=sun.azimuth_deg,
                    sun_elevation_deg=sun.elevation_deg,
                )
        return None

    def swept_for(self, job: MaintenanceJob, minute_of_day: float) -> dict[str, SweepHit]:
        """对全场求值，返回会被该作业扫到的镜子及其几何证据。"""

        hits: dict[str, SweepHit] = {}
        for mirror in self._registry.all():
            hit = self.check(mirror, job, minute_of_day)
            if hit is not None:
                hits[mirror.name] = hit
        return hits
