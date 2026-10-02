"""控制台接口、页面与 HTTP 服务。"""

from __future__ import annotations

from cspplant.console.api import Api
from cspplant.console.pages import PAGE_PATHS, render_page
from cspplant.console.server import ConsoleServer, probe_endpoints


def test_api_returns_status_and_routes(platform):
    api = Api(platform)
    status, payload = api.handle("GET", "/api/status", {}, {})
    assert status == 200
    assert payload["platform"] == platform.config.name
    paths = [route["path"] for route in api.routes()]
    assert "/api/track" in paths
    assert "/api/mirror/{name}" in paths


def test_api_unknown_path_is_404(platform):
    api = Api(platform)
    status, payload = api.handle("GET", "/api/does-not-exist", {}, {})
    assert status == 404
    assert payload["error"] == "not_found"


def test_api_mirror_detail_and_groups(platform):
    api = Api(platform)
    status, payload = api.handle("GET", "/api/mirror/m021", {}, {})
    assert status == 200
    assert payload["mirror"]["group"] == "a"
    status, grouped = api.handle("GET", "/api/mirrors", {"group": "b"}, {})
    assert status == 200
    assert grouped["count"] == 41


def test_api_track_requires_numeric_dni(platform):
    api = Api(platform)
    status, payload = api.handle("POST", "/api/track", {}, {"dni_w_m2": 820.0, "minute_of_day": 720.0})
    assert status == 200
    assert payload["mode"] == "focus"
    bad_status, bad = api.handle("POST", "/api/cycle", {}, {"dt": "not-a-number"})
    assert bad_status == 409
    assert bad["component"] == "console"


def test_api_calibrate_rejects_impossible_value(platform):
    api = Api(platform)
    status, payload = api.handle("POST", "/api/calibrate", {}, {"measured_kg_s": -5})
    assert status == 400
    assert payload["error"] == "bad_request"


def test_api_timeline_journal_and_store(platform):
    api = Api(platform)
    platform.start()
    platform.track(820.0, 720.0)
    status, timeline = api.handle("GET", "/api/timeline", {"asset": "m021"}, {})
    assert status == 200
    assert timeline["timeline"] == [] or timeline["timeline"][0]["step"] == 1
    status, journal = api.handle("GET", "/api/journal", {}, {})
    assert status == 200
    assert journal["count"] > 0
    status, store = api.handle("GET", "/api/store", {}, {})
    assert status == 200
    assert "field/irradiance" in store["documents"]


def test_api_readiness_and_forecast(platform):
    api = Api(platform)
    platform.start()
    platform.track(820.0, 720.0)
    platform.circulate()
    status, ready = api.handle("GET", "/api/readiness", {}, {})
    assert status == 200
    assert ready["checks"]["irradiance_durable"] is True
    status, forecast = api.handle("GET", "/api/forecast", {"horizon_min": "30"}, {})
    assert status == 200
    assert len(forecast["points"]) >= 3


def test_api_alarm_endpoints(platform):
    api = Api(platform)
    platform.start()
    platform.trip("overtemperature")
    status, latch = api.handle("GET", "/api/latch", {}, {})
    assert status == 200
    assert latch["latched"] is True
    status, alarms = api.handle("GET", "/api/alarms", {}, {})
    assert status == 200
    assert alarms["latched"]["latched"] is True
    status, levels = api.handle("GET", "/api/levels", {}, {})
    assert status == 200
    assert len(levels["levels"]) == len(platform.rules.rules)


def test_pages_render_for_every_path(platform):
    platform.start()
    for path in PAGE_PATHS:
        html = render_page(path, platform)
        assert html is not None
        assert html.startswith("<!doctype html>")
        assert "镜场控制台" in html or "吸热器控制台" in html or "熔盐控制台" in html or "告警控制台" in html or "检修避让控制台" in html
    assert render_page("/nope", platform) is None


def test_server_serves_api_and_pages(platform):
    platform.start()
    platform.track(820.0, 720.0)
    server = ConsoleServer(platform, host="127.0.0.1", port=0)
    base_url = server.start()
    try:
        probe = probe_endpoints(
            base_url,
            ["/api/status", "/api/readiness", "/api/topology", "/receiver", "/", "/api/nope"],
        )
    finally:
        server.stop()
    assert probe["/api/status"]["ok"] is True
    assert probe["/api/readiness"]["ok"] is True
    assert probe["/api/topology"]["ok"] is True
    assert probe["/receiver"]["ok"] is True
    assert probe["/"]["ok"] is True
    assert probe["/api/nope"]["ok"] is False
    assert server.port == 0


def test_api_maintenance_lifecycle(platform):
    """检修作业开/推进/完成经 API 全程可走，状态端点反映退出与放回。"""

    platform.start()
    api = Api(platform)
    api.handle("POST", "/api/track", {}, {"dni_w_m2": 800.0, "minute_of_day": 720.0})

    status, opened = api.handle(
        "POST",
        "/api/maintenance/open",
        {},
        {
            "job_id": "JOB-A",
            "start_minute": 715.0,
            "end_minute": 800.0,
            "x_m": 0.0,
            "y_m": 160.0,
            "radius_m": 20.0,
            "kind": "hot_work",
            "minute_of_day": 720.0,
        },
    )
    assert status == 200 and opened["state"] == "active"

    status, maint = api.handle("GET", "/api/maintenance", {}, {})
    assert status == 200
    assert "m021" in maint["held"] and maint["held_count"] >= 1

    # 动火作业位置变化立即生效。
    north = set(maint["held"])
    status, moved = api.handle(
        "POST",
        "/api/maintenance/position",
        {},
        {"job_id": "JOB-A", "x_m": -152.0, "y_m": 62.0, "minute_of_day": 720.0},
    )
    assert status == 200
    status, maint2 = api.handle("GET", "/api/maintenance", {}, {})
    assert set(maint2["held"]) != north

    status, completed = api.handle(
        "POST", "/api/maintenance/complete", {}, {"job_id": "JOB-A", "minute_of_day": 720.0}
    )
    assert status == 200 and completed["state"] == "completed"
    status, maint3 = api.handle("GET", "/api/maintenance", {}, {})
    assert maint3["held"] == []


def test_api_maintenance_interrupt_holds(platform):
    """作业中断经 API 后镜子维持退出（部分失败/未完成）。"""

    platform.start()
    api = Api(platform)
    api.handle("POST", "/api/track", {}, {"dni_w_m2": 800.0, "minute_of_day": 720.0})
    api.handle(
        "POST",
        "/api/maintenance/open",
        {},
        {
            "job_id": "JOB-B",
            "start_minute": 715.0,
            "end_minute": 800.0,
            "x_m": 0.0,
            "y_m": 160.0,
            "minute_of_day": 720.0,
        },
    )
    status, maint = api.handle("GET", "/api/maintenance", {}, {})
    held = set(maint["held"])
    assert held
    status, interrupted = api.handle(
        "POST", "/api/maintenance/interrupt", {}, {"job_id": "JOB-B", "minute_of_day": 720.0}
    )
    assert status == 200 and interrupted["state"] == "interrupted"
    status, maint2 = api.handle("GET", "/api/maintenance", {}, {})
    assert set(maint2["held"]) == held
