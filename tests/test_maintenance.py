"""检修作业避让：位置依据、作业类型时序、退出/放回顺序、手动豁免、中断与恢复。"""

from __future__ import annotations

import pytest

from cspplant.config import default_config
from cspplant.maintenance.job import HOT_WORK, ROUTINE


def _open(platform, job_id="JOB-1", kind=ROUTINE, x_m=0.0, y_m=160.0, radius_m=20.0):
    return platform.open_maintenance(
        job_id,
        start_minute=715.0,
        end_minute=800.0,
        x_m=x_m,
        y_m=y_m,
        radius_m=radius_m,
        kind=kind,
        minute_of_day=720.0,
    )


def test_job_armed_only_inside_window(platform):
    """时间窗外的作业票不武装、不产生避让。"""

    job = platform.open_maintenance(
        "FUTURE",
        start_minute=900.0,
        end_minute=960.0,
        x_m=0.0,
        y_m=160.0,
        minute_of_day=720.0,
    )
    assert job["state"] == "scheduled"
    assert platform.maintenance.held() == []


def test_swept_mirrors_exit_and_geometry_basis(platform):
    """作业一武装，会被扫到的镜子退出，且每面镜子都有命中几何证据。"""

    platform.start()
    platform.track(800.0, 720.0)
    _open(platform)
    held = platform.maintenance.held()
    assert "m021" in held  # 正午镜斑就在作业点附近
    assert platform.field.drive("m021").state in ("defocus", "idle", "stowed")
    assert platform.field.focusing_count() == len(platform.heliostats.names()) - len(held)

    exits = [e for e in platform.bus.history("maintenance") if e.action == "maintenance_exit"]
    assert exits
    basis = exits[0].payload["basis"]
    hit_mirrors = {
        name
        for job_basis in basis["basis"]
        for name in job_basis["hits"]
    }
    assert set(basis["exits"]) <= hit_mirrors | set(exits[0].payload.get("failed", []))
    assert "m021" in hit_mirrors


def test_routine_reposition_takes_effect_next_cycle_only(platform):
    """常规作业上报位置只登记，不立即重算。"""

    platform.start()
    platform.track(800.0, 720.0)
    _open(platform, kind=ROUTINE)
    before = set(platform.maintenance.held())
    platform.report_maintenance_position("JOB-1", -152.0, 62.0, minute_of_day=720.0)
    assert set(platform.maintenance.held()) == before  # 还没重算
    platform.maintenance_cycle(minute_of_day=720.0)
    assert set(platform.maintenance.held()) != before


def test_hot_work_reposition_is_immediate(platform):
    """动火作业上报位置立即重算并退出。"""

    platform.start()
    platform.track(800.0, 720.0)
    _open(platform, job_id="HOT", kind=HOT_WORK)
    north = set(platform.maintenance.held())
    platform.report_maintenance_position("HOT", -152.0, 62.0, minute_of_day=720.0)
    west = set(platform.maintenance.held())
    assert west != north
    # 每次位置变化都应产生一次带依据的退出/放回事件。
    exits = [e for e in platform.bus.history("maintenance") if e.action == "maintenance_exit"]
    assert len(exits) >= 2


def test_exit_precedes_return_and_no_mirror_in_both(platform):
    """一轮里先退后回：同一轮计划的退出/放回集合互斥，且退出事件先于放回事件。"""

    platform.start()
    platform.track(800.0, 720.0)
    _open(platform, job_id="A", kind=HOT_WORK)
    start_idx = len(platform.bus.history("maintenance"))
    # 动火作业从北区移到西区，立即重算：这一轮内先退（西区）后回（北区）。
    platform.report_maintenance_position("A", -152.0, 62.0, minute_of_day=720.0)
    batch = platform.bus.history("maintenance")[start_idx:]
    actions = [e.action for e in batch]
    if "maintenance_return" in actions:
        assert actions.index("maintenance_exit") < actions.index("maintenance_return")
    exit_event = next(e for e in reversed(batch) if e.action == "maintenance_exit")
    return_events = [e for e in batch if e.action == "maintenance_return"]
    if return_events:
        ret = return_events[-1]
        assert not (set(exit_event.payload["mirrors"]) & set(ret.payload["mirrors"]))
        # 同一轮计划的两个集合天然互斥。
        plan = exit_event.payload["basis"]
        assert not (set(plan["exits"]) & set(plan["returns"]))


def test_return_only_after_beam_clears_and_normal_completion(platform):
    """正常完成且镜斑离开后才放回；放回后下一轮追日重新对焦。"""

    platform.start()
    platform.track(800.0, 720.0)
    _open(platform)
    assert platform.maintenance.held()
    platform.complete_maintenance("JOB-1", minute_of_day=720.0)
    assert platform.maintenance.held() == []
    platform.track(800.0, 720.0)
    assert platform.field.focusing_count() == len(platform.heliostats.names())


def test_interruption_is_unfinished_and_holds_mirrors(platform, clock):
    """作业中断按未完成处理，镜子维持退出，不自动放回。"""

    platform.start()
    platform.track(800.0, 720.0)
    _open(platform)
    held = set(platform.maintenance.held())
    job = platform.interrupt_maintenance("JOB-1", minute_of_day=720.0)
    assert job["state"] == "interrupted"
    clock.advance(30)
    platform.maintenance_cycle(minute_of_day=720.0)
    assert set(platform.maintenance.held()) == held
    warning = [
        e for e in platform.bus.history("maintenance")
        if e.action == "maintenance_job_interrupted"
    ]
    assert warning and warning[0].payload["severity"] == "warning"


def test_manual_mirror_is_never_passively_actuated(platform):
    """手动接管镜即使位于光斑路径也不被动退出，只告警交人工处置。"""

    platform.start()
    platform.track(800.0, 720.0)
    platform.manual_aim("m021", 200.0, 30.0, "operator-1", hold_s=3600.0)
    _open(platform)
    drive = platform.field.drive("m021")
    assert drive.manual is True
    assert abs(drive.azimuth_deg - 200.0) < 1e-6
    blocked = [
        e for e in platform.bus.history("maintenance")
        if e.action == "maintenance_manual_blocked"
    ]
    assert blocked and "m021" in blocked[0].payload["mirrors"]
    assert blocked[0].payload["severity"] == "alarm"


def test_stale_position_holds_and_alarms(platform, clock):
    """位置过期按危险处理：维持退出并告警，不放回。"""

    platform.start()
    platform.track(800.0, 720.0)
    _open(platform)
    held = set(platform.maintenance.held())
    clock.advance(default_config().field.maintenance_position_stale_s + 5.0)
    platform.maintenance_cycle(minute_of_day=720.0)
    assert set(platform.maintenance.held()) == held
    stale = [
        e for e in platform.bus.history("maintenance")
        if e.action == "maintenance_position_stale"
    ]
    assert stale and stale[0].payload["severity"] == "alarm"


def test_exit_and_return_visible_on_console_and_audit(platform):
    """退出/放回既上控制台状态，也落审计流水，口径一致。"""

    platform.start()
    platform.track(800.0, 720.0)
    _open(platform)
    status = platform.status()["maintenance"]
    assert status["held_count"] == len(status["held"])
    audit_actions = {e.action for e in platform.bus.history("maintenance")}
    assert "maintenance_exit" in audit_actions
    platform.complete_maintenance("JOB-1", minute_of_day=720.0)
    audit_actions = {e.action for e in platform.bus.history("maintenance")}
    assert "maintenance_return" in audit_actions


def test_avoidance_survives_restart(platform, clock):
    """重启后在役作业与退出镜子恢复，并重新压回退出姿态。"""

    platform.start()
    platform.track(800.0, 720.0)
    _open(platform)
    held = platform.maintenance.held()
    assert held

    from cspplant.app import build_plant

    restarted = build_plant(default_config(), clock=clock, storage_root=platform.store.root)
    assert set(restarted.maintenance.held()) == set(held)
    assert restarted.field.drive("m021").manual is False
    assert restarted.field.drive("m021").state in ("defocus", "idle", "stowed")


def test_manual_released_mirror_is_subsequently_exited(platform):
    """手动镜释放后，若仍在光斑路径，下一轮必须补退出，不能停在手动角度。"""

    platform.start()
    platform.track(800.0, 720.0)
    platform.manual_aim("m021", 200.0, 30.0, "operator-1", hold_s=3600.0)
    _open(platform)
    drive = platform.field.drive("m021")
    assert drive.manual is True  # 期间不被动
    assert abs(drive.azimuth_deg - 200.0) < 1e-6

    # 释放手动接管，下一轮周期补退出。
    assert platform.release_manual("m021")["released"] is True
    platform.maintenance_cycle(minute_of_day=720.0)
    drive = platform.field.drive("m021")
    assert drive.manual is False
    assert "m021" in platform.maintenance.held()
    assert drive.state in ("defocus", "idle", "stowed")
    assert platform.field.focusing_count() == len(platform.heliostats.names()) - len(
        platform.maintenance.held()
    )
