"""Tests for concurrent NotebookLM lane sharding benchmarks."""

from __future__ import annotations

import json
from pathlib import Path

from csf.sharded_lane_series import (
    DEFAULT_POLICY,
    LaneConfig,
    compute_combined_hot_path_vph,
    load_lane_configs,
    preflight_lane_auth_profiles,
    run_sharded_lane_series,
)


def test_load_lane_configs_requires_distinct_profile_and_state_namespaces(tmp_path):
    config_path = tmp_path / "lanes.json"
    config_path.write_text(
        json.dumps(
            [
                {
                    "lane": "pro",
                    "account_class": "pro",
                    "workers": 4,
                    "notebooklm_profile_prefix": "ytis-worker",
                    "browser_profile_root": "P:/.data/yt-is/browser/notebooklm-pro",
                    "worker_state_root": "P:/.logs/shards/pro/worker_states",
                    "notebook_prefix": "benchmark-shard-pro",
                },
                {
                    "lane": "free",
                    "account_class": "free",
                    "workers": 4,
                    "notebooklm_profile_prefix": "ytis-worker",
                    "browser_profile_root": "P:/.data/yt-is/browser/notebooklm-free",
                    "worker_state_root": "P:/.logs/shards/free/worker_states",
                    "notebook_prefix": "benchmark-shard-free",
                },
            ]
        ),
        encoding="utf-8",
    )

    try:
        load_lane_configs(config_path)
    except ValueError as exc:
        assert "notebooklm_profile_namespace" in str(exc)
    else:
        raise AssertionError("duplicate profile prefixes must be rejected")


def test_load_lane_configs_accepts_same_browser_root_with_distinct_directories(tmp_path):
    config_path = tmp_path / "lanes.json"
    config_path.write_text(
        json.dumps(
            [
                {
                    "lane": "pro",
                    "account_class": "pro",
                    "workers": 1,
                    "notebooklm_profiles": ["alt"],
                    "browser_profile_root": "P:/.data/yt-is/browser/notebooklm",
                    "browser_profile_directory": "Profile 2",
                    "worker_state_root": "P:/.logs/shards/pro/worker_states",
                    "notebook_prefix": "benchmark-shard-pro",
                },
                {
                    "lane": "free",
                    "account_class": "free",
                    "workers": 1,
                    "notebooklm_profiles": ["default"],
                    "browser_profile_root": "P:/.data/yt-is/browser/notebooklm",
                    "browser_profile_directory": "Default",
                    "worker_state_root": "P:/.logs/shards/free/worker_states",
                    "notebook_prefix": "benchmark-shard-free",
                },
            ]
        ),
        encoding="utf-8",
    )

    lanes = load_lane_configs(config_path)

    assert lanes[0].coordinator_profile == "alt"
    assert lanes[1].coordinator_profile == "default"


def test_pro_free_lane_config_uses_dedicated_browser_roots():
    config_path = Path("P:/packages/yt-is/.logs/sharded_lane_series/pro_free_lanes.json")
    lanes = load_lane_configs(config_path)

    assert len(lanes) == 2
    assert lanes[0].lane == "a_hominidae_pro"
    assert lanes[1].lane == "troup_hominidae_free"
    assert str(lanes[0].browser_profile_root).replace("\\", "/").endswith("browser/notebooklm-pro")
    assert str(lanes[1].browser_profile_root).replace("\\", "/").endswith("browser/notebooklm-free")
    assert lanes[0].browser_profile_directory == "Profile"
    assert lanes[0].browser_profile_root != lanes[1].browser_profile_root


def test_pro_free_hotmail_lane_config_includes_second_free_account():
    config_path = Path("P:/packages/yt-is/.logs/sharded_lane_series/pro_free_hotmail_lanes.json")
    lanes = load_lane_configs(config_path)

    assert len(lanes) == 3
    assert lanes[2].lane == "brsthomson_hotmail_free"
    assert lanes[2].account_class == "free"
    assert lanes[2].notebooklm_profiles == (
        "ytis-free2-worker-01",
        "ytis-free2-worker-02",
        "ytis-free2-worker-03",
        "ytis-free2-worker-04",
    )
    assert lanes[0].browser_profile_directory == "Profile"
    assert str(lanes[2].browser_profile_root).replace("\\", "/").endswith("browser/notebooklm-free-2")
    assert lanes[2].browser_profile_directory == "Default"


def test_free_only_lane_config_uses_free_account_route():
    config_path = Path("P:/packages/yt-is/.logs/sharded_lane_series/free_only_lanes.json")
    (lane,) = load_lane_configs(config_path)

    assert lane.lane == "troup_hominidae_free"
    assert lane.account_class == "free"
    assert lane.notebooklm_profiles == (
        "ytis-free1-worker-01",
        "ytis-free1-worker-02",
        "ytis-free1-worker-03",
        "ytis-free1-worker-04",
    )
    assert str(lane.browser_profile_root).replace("\\", "/").endswith("browser/notebooklm-free")
    assert lane.browser_profile_directory == "Default"


def test_load_lane_configs_accepts_explicit_expected_email(tmp_path):
    config_path = tmp_path / "lanes.json"
    config_path.write_text(
        json.dumps(
            [
                {
                    "lane": "future",
                    "account_class": "future",
                    "workers": 1,
                    "notebooklm_profile_prefix": "ytis-future-worker",
                    "notebooklm_profiles": ["ytis-future-worker-01"],
                    "expected_email": "future.account@example.com",
                    "browser_profile_root": "P:/.data/yt-is/browser/notebooklm-future",
                    "worker_state_root": "P:/.logs/shards/future/worker_states",
                    "notebook_prefix": "benchmark-shard-future",
                }
            ]
        ),
        encoding="utf-8",
    )

    (lane,) = load_lane_configs(config_path)

    assert lane.expected_email == "future.account@example.com"


def test_compute_combined_hot_path_vph_uses_wall_clock_not_sum_of_lane_elapsed():
    lanes = [
        {"lane": "pro", "hot_path_success_count_total": 398, "started_at": 100.0, "finished_at": 470.0},
        {"lane": "free", "hot_path_success_count_total": 196, "started_at": 110.0, "finished_at": 520.0},
    ]

    combined = compute_combined_hot_path_vph(lanes)

    assert combined["hot_path_success_count_total"] == 594
    assert combined["wall_elapsed_s"] == 420.0
    assert combined["hot_path_videos_per_hour"] == 5091.43


def test_preflight_lane_auth_profiles_refreshes_expired_profile_before_run(monkeypatch):
    calls: list[list[str]] = []
    refresh_calls: list[str] = []
    sync_calls: list[dict[str, object]] = []
    repaired = False

    def fake_run(cmd, **kwargs):
        nonlocal repaired
        calls.append(list(cmd))
        if cmd == ["login", "--check", "--profile", "ytis-free1-worker-01"] and not repaired:
            return type("CompletedProcess", (), {"returncode": 1, "stdout": "", "stderr": "expired"})()
        if cmd == ["login", "--check", "--profile", "ytis-free1-worker-01"]:
            return type("CompletedProcess", (), {"returncode": 0, "stdout": "Account: troup.hominidae@gmail.com\n", "stderr": ""})()
        return type("CompletedProcess", (), {"returncode": 1, "stdout": "", "stderr": "unexpected"})()

    monkeypatch.setattr("csf.sharded_lane_series.run_nlm", fake_run)
    monkeypatch.setattr("csf.sharded_lane_series._default_chrome_profile_pids", lambda: set())
    monkeypatch.setattr(
        "csf.sharded_lane_series.refresh_source_profile",
        lambda family, timeout_s: refresh_calls.append(family.source_profile) or True,
    )

    def fake_sync_worker_profiles(**kwargs):
        nonlocal repaired
        repaired = True
        sync_calls.append(kwargs)

    monkeypatch.setattr("csf.sharded_lane_series.sync_worker_profiles", fake_sync_worker_profiles)

    preflight_lane_auth_profiles(
        (
            LaneConfig(
                lane="free",
                account_class="free",
                workers=1,
                notebooklm_profile_prefix="ytis-free1-worker",
                notebooklm_profiles=("ytis-free1-worker-01",),
                browser_profile_root=Path("P:/.data/yt-is/browser/notebooklm-free"),
                worker_state_root=Path("P:/.logs/shards/free/worker_states"),
                notebook_prefix="benchmark-shard-free",
            ),
        )
    )

    assert calls == [
        ["login", "--check", "--profile", "ytis-free1-worker-01"],
        ["login", "--check", "--profile", "ytis-free1-worker-01"],
    ]
    assert refresh_calls == ["ytis-free1-worker-01"]
    assert sync_calls


def test_preflight_lane_auth_profiles_refreshes_wrong_account_before_run(monkeypatch):
    calls: list[list[str]] = []
    refresh_calls: list[str] = []
    sync_calls: list[dict[str, object]] = []
    repaired = False

    def fake_run(cmd, **kwargs):
        nonlocal repaired
        calls.append(list(cmd))
        if cmd == ["login", "--check", "--profile", "ytis-free1-worker-01"] and not repaired:
            return type("CompletedProcess", (), {"returncode": 0, "stdout": "Account: a.hominidae@gmail.com\n", "stderr": ""})()
        if cmd == ["login", "--check", "--profile", "ytis-free1-worker-01"]:
            return type("CompletedProcess", (), {"returncode": 0, "stdout": "Account: troup.hominidae@gmail.com\n", "stderr": ""})()
        return type("CompletedProcess", (), {"returncode": 1, "stdout": "", "stderr": "unexpected"})()

    monkeypatch.setattr("csf.sharded_lane_series.run_nlm", fake_run)
    monkeypatch.setattr("csf.sharded_lane_series._default_chrome_profile_pids", lambda: set())
    monkeypatch.setattr(
        "csf.sharded_lane_series.refresh_source_profile",
        lambda family, timeout_s: refresh_calls.append(family.source_profile) or True,
    )

    def fake_sync_worker_profiles(**kwargs):
        nonlocal repaired
        repaired = True
        sync_calls.append(kwargs)

    monkeypatch.setattr("csf.sharded_lane_series.sync_worker_profiles", fake_sync_worker_profiles)

    preflight_lane_auth_profiles(
        (
            LaneConfig(
                lane="free",
                account_class="free",
                workers=1,
                notebooklm_profile_prefix="ytis-free1-worker",
                notebooklm_profiles=("ytis-free1-worker-01",),
                browser_profile_root=Path("P:/.data/yt-is/browser/notebooklm-free"),
                worker_state_root=Path("P:/.logs/shards/free/worker_states"),
                notebook_prefix="benchmark-shard-free",
            ),
        )
    )

    assert calls == [
        ["login", "--check", "--profile", "ytis-free1-worker-01"],
        ["login", "--check", "--profile", "ytis-free1-worker-01"],
    ]
    assert refresh_calls == ["ytis-free1-worker-01"]
    assert sync_calls


def test_preflight_lane_auth_profiles_rejects_profile_when_refresh_fails(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return type("CompletedProcess", (), {"returncode": 1, "stdout": "", "stderr": "expired"})()

    monkeypatch.setattr("csf.sharded_lane_series.run_nlm", fake_run)
    monkeypatch.setattr("csf.sharded_lane_series._default_chrome_profile_pids", lambda: set())
    monkeypatch.setattr("csf.sharded_lane_series.refresh_source_profile", lambda family, timeout_s: False)

    try:
        preflight_lane_auth_profiles(
            (
                LaneConfig(
                    lane="free",
                    account_class="free",
                    workers=1,
                    notebooklm_profile_prefix="ytis-free1-worker",
                    notebooklm_profiles=("ytis-free1-worker-01",),
                    browser_profile_root=Path("P:/.data/yt-is/browser/notebooklm-free"),
                    worker_state_root=Path("P:/.logs/shards/free/worker_states"),
                    notebook_prefix="benchmark-shard-free",
                ),
            )
        )
    except RuntimeError as exc:
        assert "ytis-free1-worker-01" in str(exc)
    else:
        raise AssertionError("failed refresh should stop the benchmark before lane launch")

    assert calls == [
        ["login", "--check", "--profile", "ytis-free1-worker-01"],
    ]


def test_preflight_lane_auth_profiles_stops_default_profile_before_checks(monkeypatch):
    """Series preflight should self-heal stale default Chrome before validating lane profiles."""
    import csf.sharded_lane_series as mod

    stop_calls: list[set[int]] = []
    check_calls: list[tuple[str, float]] = []

    monkeypatch.setattr(mod, "_default_chrome_profile_pids", lambda: {111, 222}, raising=False)
    monkeypatch.setattr(mod, "_stop_chrome_pids", lambda pids: stop_calls.append(set(pids)), raising=False)
    monkeypatch.setattr(
        mod,
        "_profile_auth_check",
        lambda profile, expected_email, timeout_s: check_calls.append((profile, timeout_s)) or True,
    )

    preflight_lane_auth_profiles(
        (
            LaneConfig(
                lane="free",
                account_class="free",
                workers=1,
                notebooklm_profile_prefix="ytis-free1-worker",
                notebooklm_profiles=("ytis-free1-worker-01",),
                browser_profile_root=Path("P:/.data/yt-is/browser/notebooklm-free"),
                worker_state_root=Path("P:/.logs/shards/free/worker_states"),
                notebook_prefix="benchmark-shard-free",
            ),
        )
    )

    assert stop_calls == [{111, 222}]
    assert check_calls == [("ytis-free1-worker-01", 30.0)]


def test_preflight_lane_auth_profiles_accepts_explicit_expected_email_for_unmapped_profile(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return type("CompletedProcess", (), {"returncode": 0, "stdout": "Account: future.account@example.com\n", "stderr": ""})()

    monkeypatch.setattr("csf.sharded_lane_series.run_nlm", fake_run)
    monkeypatch.setattr("csf.sharded_lane_series._default_chrome_profile_pids", lambda: set())

    preflight_lane_auth_profiles(
        (
            LaneConfig(
                lane="future",
                account_class="future",
                workers=1,
                notebooklm_profile_prefix="ytis-future-worker",
                notebooklm_profiles=("ytis-future-worker-01",),
                expected_email="future.account@example.com",
                browser_profile_root=Path("P:/.data/yt-is/browser/notebooklm-future"),
                worker_state_root=Path("P:/.logs/shards/future/worker_states"),
                notebook_prefix="benchmark-shard-future",
            ),
        )
    )

    assert calls == [["login", "--check", "--profile", "ytis-future-worker-01"]]


def test_preflight_lane_auth_profiles_rejects_unmapped_profile_without_expected_email(monkeypatch):
    monkeypatch.setattr("csf.sharded_lane_series._default_chrome_profile_pids", lambda: set())

    try:
        preflight_lane_auth_profiles(
            (
                LaneConfig(
                    lane="future",
                    account_class="future",
                    workers=1,
                    notebooklm_profile_prefix="ytis-future-worker",
                    notebooklm_profiles=("ytis-future-worker-01",),
                    browser_profile_root=Path("P:/.data/yt-is/browser/notebooklm-future"),
                    worker_state_root=Path("P:/.logs/shards/future/worker_states"),
                    notebook_prefix="benchmark-shard-future",
                ),
            )
        )
    except RuntimeError as exc:
        assert "has no expected email mapping" in str(exc)
        assert "ytis-future-worker-01" in str(exc)
    else:
        raise AssertionError("unmapped profile should fail closed before benchmark start")


def test_run_sharded_lane_series_passes_isolated_lane_env(tmp_path, monkeypatch):
    import csf.sharded_lane_series as mod

    calls: list[dict[str, object]] = []

    def fake_run_lane(*, lane, trace_root, output_root, cohort_json, source_url, policy, limit, batch_size, manifest_json, python_executable, reusable_pipeline_mode, env):
        calls.append(
            {
                "lane": lane.lane,
                "output_root": output_root,
                "cohort_json": cohort_json,
                "env": {
                    key: env.get(key)
                    for key in (
                        "INTELLIGENCE_STREAM_LOG_DIR",
                        "YTIS_INDUSTRIAL_WORKER_STATE_ROOT",
                        "YTIS_INDUSTRIAL_WORKER_NOTEBOOK_PREFIX",
                        "YTIS_INDUSTRIAL_WORKER_NOTEBOOKLM_PROFILE_PREFIX",
                        "YTIS_INDUSTRIAL_WORKER_NOTEBOOKLM_PROFILES",
                        "YTIS_NLM_BROWSER_PROFILE_ROOT",
                        "YTIS_NLM_BROWSER_PROFILE_DIRECTORY",
                        "YTIS_BATCH_STATUS_DB_PATH",
                        "YTIS_REUSABLE_PIPELINE_MODE",
                        "YTIS_NLM_AUTH_NONINTERACTIVE",
                    )
                },
            }
        )
        success = 398 if lane.lane == "pro" else 196
        started = 100.0 if lane.lane == "pro" else 110.0
        finished = 470.0 if lane.lane == "pro" else 520.0
        return {
            "lane": lane.lane,
            "account_class": lane.account_class,
            "started_at": started,
            "finished_at": finished,
            "wall_elapsed_s": round(finished - started, 3),
            "hot_path_success_count_total": success,
            "fail_count_total": 2,
            "transcript_fallback_success_count_total": 0,
            "hot_path_videos_per_hour": round(success / (finished - started) * 3600.0, 2),
        }

    monkeypatch.setattr(mod, "_run_lane", fake_run_lane)

    report = run_sharded_lane_series(
        lanes=(
            LaneConfig(
                lane="pro",
                account_class="pro",
                workers=4,
                notebooklm_profile_prefix="ytis-pro-worker",
                notebooklm_profiles=("alt", "ytis-pro-worker-02", "ytis-pro-worker-03", "ytis-pro-worker-04"),
                browser_profile_root=Path("P:/.data/yt-is/browser/notebooklm-pro"),
                browser_profile_directory="Profile 2",
                worker_state_root=tmp_path / "pro" / "worker_states",
                notebook_prefix="benchmark-shard-pro",
            ),
            LaneConfig(
                lane="free",
                account_class="free",
                workers=4,
                notebooklm_profile_prefix="ytis-free1-worker",
                notebooklm_profiles=("default", "ytis-free1-worker-02", "ytis-free1-worker-03", "ytis-free1-worker-04"),
                browser_profile_root=Path("P:/.data/yt-is/browser/notebooklm-free"),
                browser_profile_directory="Profile 1",
                worker_state_root=tmp_path / "free" / "worker_states",
                notebook_prefix="benchmark-shard-free",
            ),
        ),
        trace_root=tmp_path / "trace",
        output_root=tmp_path / "out",
        cohort_json=tmp_path / "out" / "cohort.json",
        source_url="https://www.youtube.com/channel/UCYTISFALLBACKBMK",
        policy="notebooklm_route_plus_fallback_30s_1w",
        limit=400,
        batch_size=200,
        manifest_json=Path("P:/packages/yt-is/tests/fixtures/shared_benchmark_manifest.json"),
        reusable_pipeline_mode="serial",
    )

    assert [call["lane"] for call in calls] == ["pro", "free"]
    assert calls[0]["env"]["INTELLIGENCE_STREAM_LOG_DIR"].endswith("out\\pro\\logs")
    assert calls[0]["env"]["YTIS_INDUSTRIAL_WORKER_STATE_ROOT"].endswith("pro\\worker_states")
    assert calls[0]["env"]["YTIS_INDUSTRIAL_WORKER_NOTEBOOK_PREFIX"] == "benchmark-shard-pro"
    assert calls[0]["env"]["YTIS_INDUSTRIAL_WORKER_NOTEBOOKLM_PROFILE_PREFIX"] == "ytis-pro-worker"
    assert calls[0]["env"]["YTIS_INDUSTRIAL_WORKER_NOTEBOOKLM_PROFILES"] == "alt,ytis-pro-worker-02,ytis-pro-worker-03,ytis-pro-worker-04"
    assert calls[0]["env"]["YTIS_NLM_BROWSER_PROFILE_ROOT"] == "P:\\.data\\yt-is\\browser\\notebooklm-pro"
    assert calls[0]["env"]["YTIS_NLM_BROWSER_PROFILE_DIRECTORY"] == "Profile 2"
    assert calls[0]["env"]["YTIS_BATCH_STATUS_DB_PATH"].endswith("out\\pro\\batch_status.sqlite")
    assert calls[0]["env"]["YTIS_NLM_AUTH_NONINTERACTIVE"] == "1"
    assert calls[1]["env"]["YTIS_INDUSTRIAL_WORKER_STATE_ROOT"].endswith("free\\worker_states")
    assert calls[1]["env"]["YTIS_INDUSTRIAL_WORKER_NOTEBOOK_PREFIX"] == "benchmark-shard-free"
    assert calls[1]["env"]["YTIS_INDUSTRIAL_WORKER_NOTEBOOKLM_PROFILE_PREFIX"] == "ytis-free1-worker"
    assert calls[1]["env"]["YTIS_INDUSTRIAL_WORKER_NOTEBOOKLM_PROFILES"] == "default,ytis-free1-worker-02,ytis-free1-worker-03,ytis-free1-worker-04"
    assert calls[1]["env"]["INTELLIGENCE_STREAM_LOG_DIR"].endswith("out\\free\\logs")
    assert calls[1]["env"]["YTIS_NLM_BROWSER_PROFILE_ROOT"] == "P:\\.data\\yt-is\\browser\\notebooklm-free"
    assert calls[1]["env"]["YTIS_NLM_BROWSER_PROFILE_DIRECTORY"] == "Profile 1"
    assert calls[1]["env"]["YTIS_BATCH_STATUS_DB_PATH"].endswith("out\\free\\batch_status.sqlite")
    assert calls[1]["env"]["YTIS_NLM_AUTH_NONINTERACTIVE"] == "1"
    assert report["report_version"] == 1
    assert report["combined"]["hot_path_success_count_total"] == 594
    assert report["combined"]["hot_path_videos_per_hour"] == 5091.43
    assert report["report_version"] == 1
    assert Path(report["report_path"]).exists()


def test_run_sharded_lane_series_clears_stale_summary_before_run(tmp_path, monkeypatch):
    import csf.sharded_lane_series as mod

    stale_report = tmp_path / "out" / "sharded_lane_series_summary.json"
    stale_report.parent.mkdir(parents=True, exist_ok=True)
    stale_report.write_text('{"stale": true}', encoding="utf-8")

    def fake_run_lane(*args, **kwargs):
        raise RuntimeError("lane failed before report write")

    monkeypatch.setattr(mod, "_run_lane", fake_run_lane)

    report = run_sharded_lane_series(
        lanes=(
            LaneConfig(
                lane="pro",
                account_class="pro",
                workers=1,
                notebooklm_profile_prefix="ytis-pro-worker",
                notebooklm_profiles=("ytis-pro-worker-01",),
                browser_profile_root=Path("P:/.data/yt-is/browser/notebooklm-pro"),
                worker_state_root=tmp_path / "pro" / "worker_states",
                notebook_prefix="benchmark-shard-pro",
            ),
        ),
        trace_root=tmp_path / "trace",
        output_root=tmp_path / "out",
        cohort_json=tmp_path / "out" / "cohort.json",
        source_url="https://www.youtube.com/channel/UCYTISFALLBACKBMK",
        policy="notebooklm_route_plus_fallback_30s_1w",
        limit=1,
        batch_size=1,
        manifest_json=Path("P:/packages/yt-is/tests/fixtures/shared_benchmark_manifest.json"),
        reusable_pipeline_mode="serial",
    )

    persisted = json.loads(stale_report.read_text(encoding="utf-8"))
    assert report["status"] == "invalidated"
    assert report["report_version"] == 1
    assert report["failure_count"] == 1
    assert report["failures"][0]["lane"] == "pro"
    assert "lane failed before report write" in report["failures"][0]["error"]
    assert report["runs"][0]["status"] == "invalidated"
    assert "RuntimeError: lane failed before report write" in report["runs"][0]["traceback"]
    assert persisted["status"] == "invalidated"
    assert "stale" not in persisted


def test_run_sharded_lane_series_preserves_previous_summary_if_write_fails(tmp_path, monkeypatch):
    import csf.sharded_lane_series as mod

    stale_report = tmp_path / "out" / "sharded_lane_series_summary.json"
    stale_report.parent.mkdir(parents=True, exist_ok=True)
    stale_report.write_text('{"stale": true}', encoding="utf-8")

    def fake_run_lane(*args, **kwargs):
        return {
            "status": "ok",
            "lane": "pro",
            "account_class": "pro",
            "started_at": 100.0,
            "finished_at": 110.0,
            "wall_elapsed_s": 10.0,
            "hot_path_success_count_total": 1,
            "fail_count_total": 0,
            "transcript_fallback_success_count_total": 0,
            "processed_count_total": 1,
            "hot_path_videos_per_hour": 360.0,
        }

    def fake_write_json_atomic(path, payload):
        raise OSError("simulated summary write failure")

    monkeypatch.setattr(mod, "_run_lane", fake_run_lane)
    monkeypatch.setattr(mod, "_write_json_atomic", fake_write_json_atomic)

    try:
        run_sharded_lane_series(
            lanes=(
                LaneConfig(
                    lane="pro",
                    account_class="pro",
                    workers=1,
                    notebooklm_profile_prefix="ytis-pro-worker",
                    notebooklm_profiles=("ytis-pro-worker-01",),
                    browser_profile_root=Path("P:/.data/yt-is/browser/notebooklm-pro"),
                    worker_state_root=tmp_path / "pro" / "worker_states",
                    notebook_prefix="benchmark-shard-pro",
                ),
            ),
            trace_root=tmp_path / "trace",
            output_root=tmp_path / "out",
            cohort_json=tmp_path / "out" / "cohort.json",
            source_url="https://www.youtube.com/channel/UCYTISFALLBACKBMK",
            policy="notebooklm_route_plus_fallback_30s_1w",
            limit=1,
            batch_size=1,
            manifest_json=Path("P:/packages/yt-is/tests/fixtures/shared_benchmark_manifest.json"),
            reusable_pipeline_mode="serial",
        )
    except OSError as exc:
        assert "simulated summary write failure" in str(exc)
    else:
        raise AssertionError("summary write failure should have propagated")

    assert stale_report.exists()
    assert json.loads(stale_report.read_text(encoding="utf-8")) == {"stale": True}


def test_run_lane_rejects_default_profile_contaminated_logs(tmp_path, monkeypatch):
    """A lane that observed the shared default profile must not be accepted as a benchmark result."""
    import csf.sharded_lane_series as mod

    lane = LaneConfig(
        lane="free",
        account_class="free",
        workers=1,
        notebooklm_profile_prefix="ytis-free1-worker",
        notebooklm_profiles=("ytis-free1-worker-01",),
        browser_profile_root=Path("P:/.data/yt-is/browser/notebooklm-free"),
        worker_state_root=tmp_path / "free" / "worker_states",
        notebook_prefix="benchmark-shard-free",
    )

    def fake_build_command(**kwargs):
        return ["fake-benchmark"]

    def fake_run(cmd, **kwargs):
        lane_root = tmp_path / "out" / "free"
        lane_root.mkdir(parents=True, exist_ok=True)
        (lane_root / "benchmark_summary.json").write_text(
            json.dumps(
                {
                    "batches": [
                        {
                            "policies": [
                                {
                                    "policy": DEFAULT_POLICY,
                                    "results": [
                                        {
                                            "success_count": 1,
                                            "fail_count": 0,
                                            "processed_count": 1,
                                            "hot_path_success_count": 1,
                                            "transcript_fallback_success_count": 0,
                                            "elapsed_s": 1.0,
                                        }
                                    ],
                                }
                            ]
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        log_dir = lane_root / "batch_01" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "term.jsonl").write_text(
            json.dumps(
                {
                    "action": "nlm_auth_failed",
                    "data": {
                        "status": "default_profile_running",
                        "notebooklm_profile": "ytis-free1-worker-01",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return type("CompletedProcess", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(mod, "build_fallback_benchmark_command", fake_build_command)
    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    try:
        mod._run_lane(
            lane=lane,
            trace_root=tmp_path / "trace",
            output_root=tmp_path / "out",
            cohort_json=tmp_path / "out" / "cohort.json",
            source_url="https://www.youtube.com/channel/UCYTISFALLBACKBMK",
            policy=DEFAULT_POLICY,
            limit=1,
            batch_size=1,
            manifest_json=Path("P:/packages/yt-is/tests/fixtures/shared_benchmark_manifest.json"),
            python_executable=None,
            reusable_pipeline_mode="serial",
            env={},
        )
    except RuntimeError as exc:
        assert "default_profile_running" in str(exc)
    else:
        raise AssertionError("default-profile contamination should invalidate the lane")


def test_find_invalid_lane_artifacts_flags_zero_growth_terminal(tmp_path):
    import csf.sharded_lane_series as mod

    lane_root = tmp_path / "lane"
    log_dir = lane_root / "batch_01" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "term.jsonl").write_text(
        json.dumps(
            {
                "action": "nlm_batch_subbatch_zero_growth_terminal",
                "data": {
                    "subbatch_size": 50,
                    "source_count_before": 0,
                    "source_count_after": 0,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    findings = mod._find_invalid_lane_artifacts(lane_root)

    assert len(findings) == 1
    assert "zero_growth_source_add" in findings[0]
    assert "sources=0->0" in findings[0]


def test_find_invalid_lane_artifacts_flags_probe_terminal(tmp_path):
    import csf.sharded_lane_series as mod

    lane_root = tmp_path / "lane"
    log_dir = lane_root / "batch_01" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "term.jsonl").write_text(
        json.dumps(
            {
                "action": "nlm_batch_subbatch_source_count_probe_terminal",
                "data": {
                    "subbatch_size": 50,
                    "source_count_before": 0,
                    "source_count_after": 0,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    findings = mod._find_invalid_lane_artifacts(lane_root)

    assert len(findings) == 1
    assert "source_count_probe_failed" in findings[0]
    assert "sources=0->0" in findings[0]


def test_main_refuses_to_start_when_doctor_fails(tmp_path, monkeypatch):
    import csf.sharded_lane_series as mod

    config_path = tmp_path / "lanes.json"
    config_path.write_text(
        json.dumps(
            [
                {
                    "lane": "free",
                    "account_class": "free",
                    "workers": 1,
                    "notebooklm_profile_prefix": "ytis-free1-worker",
                    "notebooklm_profiles": ["ytis-free1-worker-01"],
                    "browser_profile_root": "P:/.data/yt-is/browser/notebooklm-free",
                    "browser_profile_directory": "Default",
                    "worker_state_root": str(tmp_path / "worker_states"),
                    "notebook_prefix": "benchmark-shard-free",
                }
            ]
        ),
        encoding="utf-8",
    )
    output_root = tmp_path / "out"
    called = []

    monkeypatch.setattr(mod, "doctor_lane_setup", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("doctor failed")))
    monkeypatch.setattr(mod, "run_sharded_lane_series", lambda *args, **kwargs: called.append(True))

    result = mod.main([
        "--lane-config",
        str(config_path),
        "--output-root",
        str(output_root),
    ])

    assert result == 1
    assert called == []


def test_main_returns_nonzero_for_invalidated_report(tmp_path, monkeypatch):
    import csf.sharded_lane_series as mod

    config_path = tmp_path / "lanes.json"
    config_path.write_text("[]", encoding="utf-8")
    output_root = tmp_path / "out"
    report_path = output_root / "sharded_lane_series_summary.json"
    lanes = (
        LaneConfig(
            lane="free",
            account_class="free",
            workers=1,
            notebooklm_profile_prefix="ytis-free1-worker",
            notebooklm_profiles=("ytis-free1-worker-01",),
            browser_profile_root=Path("P:/.data/yt-is/browser/notebooklm-free"),
            worker_state_root=tmp_path / "worker_states",
            notebook_prefix="benchmark-shard-free",
        ),
    )

    monkeypatch.setattr(mod, "doctor_lane_setup", lambda *args, **kwargs: lanes)
    monkeypatch.setattr(
        mod,
        "run_sharded_lane_series",
        lambda *args, **kwargs: {
            "report_version": 1,
            "status": "invalidated",
            "failure_count": 1,
            "report_path": str(report_path),
            "combined": {
                "hot_path_videos_per_hour": 0.0,
                "hot_path_success_count_total": 0,
                "fail_count_total": 0,
                "wall_elapsed_s": 0.0,
            },
        },
    )

    result = mod.main([
        "--lane-config",
        str(config_path),
        "--output-root",
        str(output_root),
    ])

    assert result == 1


def test_main_reports_versioned_invalidated_summary(tmp_path, monkeypatch):
    import csf.sharded_lane_series as mod

    config_path = tmp_path / "lanes.json"
    config_path.write_text("[]", encoding="utf-8")
    output_root = tmp_path / "out"
    report_path = output_root / "sharded_lane_series_summary.json"
    lanes = (
        LaneConfig(
            lane="free",
            account_class="free",
            workers=1,
            notebooklm_profile_prefix="ytis-free1-worker",
            notebooklm_profiles=("ytis-free1-worker-01",),
            browser_profile_root=Path("P:/.data/yt-is/browser/notebooklm-free"),
            worker_state_root=tmp_path / "worker_states",
            notebook_prefix="benchmark-shard-free",
        ),
    )

    monkeypatch.setattr(mod, "doctor_lane_setup", lambda *args, **kwargs: lanes)
    monkeypatch.setattr(
        mod,
        "run_sharded_lane_series",
        lambda *args, **kwargs: {
            "report_version": 1,
            "status": "invalidated",
            "failure_count": 1,
            "report_path": str(report_path),
            "combined": {
                "hot_path_videos_per_hour": 0.0,
                "hot_path_success_count_total": 0,
                "fail_count_total": 0,
                "wall_elapsed_s": 0.0,
            },
        },
    )

    result = mod.main([
        "--lane-config",
        str(config_path),
        "--output-root",
        str(output_root),
    ])

    assert result == 1


