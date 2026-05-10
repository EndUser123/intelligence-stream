"""Targeted NotebookLM source-content probe for a small video set."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from csf import nlm_auth_guard
from csf.youtube_page_inspector import inspect_youtube_watch_page_via_ytdlp


_NLM_CONTENT_READY_THRESHOLD = 100
_DEFAULT_RETRY_DELAYS_S = (0, 30, 60, 120)
_DEFAULT_OUTPUT_ROOT = Path("P:\\\\\\packages/yt-is/.logs/nlm_content_probe")


def _parse_source_ids(stdout: str) -> list[str]:
    source_ids: list[str] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if "Source ID:" not in line:
            continue
        _, _, remainder = line.partition("Source ID:")
        source_id = remainder.strip()
        if source_id:
            source_ids.append(source_id)
    return source_ids


def _parse_notebook_id(stdout: str) -> str:
    for line in (stdout or "").splitlines():
        line = line.strip()
        if "ID:" in line:
            _, _, remainder = line.partition("ID:")
            nb_id = remainder.strip()
            if nb_id:
                return nb_id
    return (stdout or "").strip()


def _probe_status(returncode: int, content_length: int, parse_ok: bool) -> str:
    if returncode != 0:
        return "command_failed"
    if not parse_ok:
        return "parse_failed"
    if content_length > _NLM_CONTENT_READY_THRESHOLD:
        return "ready"
    return "nlm_content_below_threshold"


def _nlm_env(profile: str) -> dict[str, str]:
    env = os.environ.copy()
    env["NOTEBOOKLM_PROFILE"] = profile
    env["YTIS_NLM_AUTH_NONINTERACTIVE"] = "1"
    return env


def _run_nlm(profile: str, args: list[str], *, timeout_s: int) -> subprocess.CompletedProcess[str]:
    return nlm_auth_guard.run_nlm(
        nlm_auth_guard.add_profile_args(args, profile),
        timeout_s=timeout_s,
        env=_nlm_env(profile),
    )


def _fetch_content(profile: str, source_id: str, *, timeout_s: int = 30) -> dict[str, Any]:
    started_at_epoch = time.time()
    started_at_perf = time.perf_counter()
    res = _run_nlm(profile, ["source", "content", source_id, "--json"], timeout_s=timeout_s)
    content = ""
    content_length = 0
    parse_ok = False
    if res.returncode == 0:
        try:
            data = json.loads(res.stdout or "")
            if isinstance(data, dict):
                content = data.get("value", {}).get("content", "")
                if not content:
                    content = data.get("content", "")
            content_length = len(content)
            parse_ok = True
        except Exception:
            parse_ok = False
    status = _probe_status(res.returncode, content_length, parse_ok)
    return {
        "profile": profile,
        "source_id": source_id,
        "status": status,
        "returncode": res.returncode,
        "content_length": content_length,
        "nlm_content_chars": content_length,
        "usable_text_chars": content_length if status == "ready" else 0,
        "stdout": res.stdout or "",
        "stderr": res.stderr or "",
        "started_at_epoch": started_at_epoch,
        "completed_at_epoch": time.time(),
        "elapsed_s": round(time.perf_counter() - started_at_perf, 3),
    }


def _create_probe_notebook(profile: str, notebook_name: str) -> str:
    res = _run_nlm(profile, ["notebook", "create", notebook_name], timeout_s=120)
    if res.returncode != 0:
        raise RuntimeError(f"notebook create failed for {profile}: {res.stderr or res.stdout or res.returncode}")
    nb_id = _parse_notebook_id(res.stdout or "")
    if not nb_id:
        raise RuntimeError(f"could not parse notebook id for {profile}: {res.stdout or res.stderr}")
    return nb_id


def _add_video_source(profile: str, notebook_id: str, video_id: str) -> dict[str, Any]:
    url = f"https://www.youtube.com/watch?v={video_id}"
    started_at_perf = time.perf_counter()
    res = _run_nlm(profile, ["source", "add", notebook_id, "--wait", "--url", url], timeout_s=600)
    source_ids = _parse_source_ids(res.stdout or "")
    return {
        "profile": profile,
        "notebook_id": notebook_id,
        "video_id": video_id,
        "url": url,
        "returncode": res.returncode,
        "stdout": res.stdout or "",
        "stderr": res.stderr or "",
        "elapsed_s": round(time.perf_counter() - started_at_perf, 3),
        "source_ids": source_ids,
        "source_id": source_ids[0] if source_ids else None,
    }


@dataclass
class ProbeVideoResult:
    profile: str
    notebook_id: str
    video_id: str
    source_id: str | None
    video_duration_s: int | None
    ytdlp_classification: str | None
    add_result: dict[str, Any]
    preloads: list[dict[str, Any]]
    attempts: list[dict[str, Any]]


def run_probe(
    profiles: list[str],
    video_ids: list[str],
    *,
    output_root: Path = _DEFAULT_OUTPUT_ROOT,
    retry_delays_s: tuple[int, ...] = _DEFAULT_RETRY_DELAYS_S,
    continue_after_ready: bool = False,
    preload_video_ids: list[str] | None = None,
    target_video_id: str | None = None,
) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    run_dir = output_root / started_at.strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    notebooks: dict[str, str] = {}
    for profile in profiles:
        notebook_name = f"yt-is-content-probe-{profile}-{started_at.strftime('%Y%m%d-%H%M%S')}"
        notebooks[profile] = _create_probe_notebook(profile, notebook_name)

    probe_video_ids = [target_video_id] if target_video_id else list(video_ids)
    preload_video_ids = list(preload_video_ids or [])

    def _run_profile(profile: str) -> list[dict[str, Any]]:
        profile_results: list[dict[str, Any]] = []
        notebook_id = notebooks[profile]
        preloads: list[dict[str, Any]] = []
        for preload_video_id in preload_video_ids:
            preload_probe = inspect_youtube_watch_page_via_ytdlp(preload_video_id)
            preload_add_result = _add_video_source(profile, notebook_id, preload_video_id)
            preloads.append(
                {
                    "profile": profile,
                    "video_id": preload_video_id,
                    "source_id": preload_add_result.get("source_id"),
                    "add_result": preload_add_result,
                    "ytdlp_classification": preload_probe.get("classification"),
                    "ytdlp_available": preload_probe.get("available"),
                    "ytdlp_availability": preload_probe.get("availability"),
                }
            )
        for video_id in probe_video_ids:
            ytdlp_probe = inspect_youtube_watch_page_via_ytdlp(video_id)
            duration = ytdlp_probe.get("duration")
            add_result = _add_video_source(profile, notebook_id, video_id)
            attempts: list[dict[str, Any]] = []
            source_id = add_result.get("source_id")
            if source_id:
                for index, delay_s in enumerate(retry_delays_s, start=1):
                    if delay_s > 0:
                        time.sleep(delay_s)
                    attempt = _fetch_content(profile, str(source_id))
                    attempt.update(
                        {
                            "attempt_index": index,
                            "scheduled_delay_s": delay_s,
                            "video_id": video_id,
                            "notebook_id": notebook_id,
                            "source_id": source_id,
                            "video_duration_s": duration,
                            "ytdlp_classification": ytdlp_probe.get("classification"),
                            "ytdlp_available": ytdlp_probe.get("available"),
                            "ytdlp_availability": ytdlp_probe.get("availability"),
                        }
                    )
                    attempts.append(attempt)
                    if attempt["status"] == "ready" and not continue_after_ready:
                        break
            profile_results.append(
                asdict(
                    ProbeVideoResult(
                        profile=profile,
                        notebook_id=notebook_id,
                        video_id=video_id,
                        source_id=source_id,
                        video_duration_s=duration if isinstance(duration, int) else None,
                        ytdlp_classification=str(ytdlp_probe.get("classification") or None),
                        add_result=add_result,
                        preloads=preloads,
                        attempts=attempts,
                    )
                )
            )
        return profile_results

    for profile in profiles:
        results.extend(_run_profile(profile))

    summary = {
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "output_root": str(run_dir),
        "profiles": profiles,
        "video_ids": probe_video_ids,
        "preload_video_ids": preload_video_ids,
        "target_video_id": target_video_id,
        "notebooks": notebooks,
        "results": results,
    }
    (run_dir / "probe_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    with (run_dir / "probe_results.jsonl").open("w", encoding="utf-8") as handle:
        for item in results:
            handle.write(json.dumps(item) + "\n")
    return summary


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a targeted NotebookLM source-content probe.")
    parser.add_argument("--profile", action="append", dest="profiles", required=True, help="NotebookLM profile to use. Repeat for multiple profiles.")
    parser.add_argument("--video-id", action="append", dest="video_ids", required=True, help="YouTube video ID to probe. Repeat for multiple videos.")
    parser.add_argument(
        "--preload-video-id",
        action="append",
        dest="preload_video_ids",
        help="Video ID to add before the target probe, to simulate notebook load pressure.",
    )
    parser.add_argument(
        "--target-video-id",
        dest="target_video_id",
        help="Single target video ID to fetch after any preloads are added.",
    )
    parser.add_argument(
        "--retry-delay-s",
        action="append",
        dest="retry_delays_s",
        type=int,
        help="Delay in seconds before each fetch attempt. Repeat to override the default 0,30,60,120 schedule.",
    )
    parser.add_argument(
        "--continue-after-ready",
        action="store_true",
        help="Keep running the configured retry delays even after the first ready result.",
    )
    parser.add_argument("--output-root", type=Path, default=_DEFAULT_OUTPUT_ROOT, help="Directory for JSON artifacts.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    retry_delays_s = tuple(args.retry_delays_s) if args.retry_delays_s else _DEFAULT_RETRY_DELAYS_S
    summary = run_probe(
        list(args.profiles),
        list(args.video_ids),
        output_root=args.output_root,
        retry_delays_s=retry_delays_s,
        continue_after_ready=bool(args.continue_after_ready),
        preload_video_ids=list(args.preload_video_ids or []),
        target_video_id=args.target_video_id,
    )
    print(json.dumps(summary, indent=2))
    return 0
