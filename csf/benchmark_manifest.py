"""Shared benchmark manifest loader for routing and Whisper coverage."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Iterable

_EXPECTED_KEYS = (
    "hot_path",
    "route_to_fallback",
    "attempt_whisper",
    "skip_whisper",
    "recover_success",
    "terminal_skip",
)

_ALLOWED_SOURCE_TYPES = {"live_trace", "synthetic"}


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    case_id: str
    family: str
    source_type: str
    video_id: str
    source_url: str
    title: str | None
    description: str | None
    duration: int | None
    privacy_status: str | None
    upload_status: str | None
    is_live_content: bool
    unavailable_reason: str | None
    has_captions: bool
    expected: dict[str, bool]

    def to_batch_item(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "family": self.family,
            "source_type": self.source_type,
            "video_id": self.video_id,
            "source_url": self.source_url,
            "title": self.title,
            "description": self.description,
            "duration": self.duration,
            "privacy_status": self.privacy_status,
            "upload_status": self.upload_status,
            "is_live_content": self.is_live_content,
            "unavailable_reason": self.unavailable_reason,
            "has_captions": self.has_captions,
            "expected": dict(self.expected),
        }


@dataclass(frozen=True, slots=True)
class BenchmarkManifest:
    manifest_version: int
    generated_at: str
    cases: tuple[BenchmarkCase, ...]

    def cases_for_benchmark(self, families: Iterable[str] | None = None) -> tuple[BenchmarkCase, ...]:
        allowed_families = {str(family).strip() for family in families or () if str(family).strip()}
        filtered = [
            case
            for case in self.cases
            if case.source_type == "live_trace" and (not allowed_families or case.family in allowed_families)
        ]
        return tuple(filtered)

    def cases_for_family(self, *families: str) -> tuple[BenchmarkCase, ...]:
        allowed_families = {family.strip() for family in families if family.strip()}
        if not allowed_families:
            return self.cases
        return tuple(case for case in self.cases if case.family in allowed_families)


def load_benchmark_manifest(manifest_path: Path) -> BenchmarkManifest:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("manifest root must be an object")
    manifest_version = payload.get("manifest_version")
    if manifest_version != 1:
        raise ValueError("manifest_version must be 1")
    generated_at = payload.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at.strip():
        raise ValueError("generated_at must be a non-empty string")
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list):
        raise ValueError("cases must be a list")

    cases: list[BenchmarkCase] = []
    seen_case_ids: set[str] = set()
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, dict):
            raise ValueError(f"case[{index}] must be an object")
        case = _load_case(raw_case, index=index)
        if case.case_id in seen_case_ids:
            raise ValueError(f"duplicate case_id: {case.case_id}")
        seen_case_ids.add(case.case_id)
        cases.append(case)

    return BenchmarkManifest(
        manifest_version=1,
        generated_at=generated_at,
        cases=tuple(cases),
    )


def _load_case(raw_case: dict[str, Any], *, index: int) -> BenchmarkCase:
    case_id = _require_text(raw_case, "case_id", index=index)
    family = _require_text(raw_case, "family", index=index)
    source_type = _require_text(raw_case, "source_type", index=index)
    if source_type not in _ALLOWED_SOURCE_TYPES:
        raise ValueError(f"case[{index}].source_type must be one of: {', '.join(sorted(_ALLOWED_SOURCE_TYPES))}")
    video_id = _require_text(raw_case, "video_id", index=index)
    source_url = _optional_text(raw_case.get("source_url")) or ""
    title = _optional_text(raw_case.get("title"))
    description = _optional_text(raw_case.get("description"))
    duration = _optional_int(raw_case.get("duration"), field_name="duration", index=index)
    privacy_status = _optional_text(raw_case.get("privacy_status"))
    upload_status = _optional_text(raw_case.get("upload_status"))
    is_live_content = _require_bool(raw_case, "is_live_content", index=index)
    unavailable_reason = _optional_text(raw_case.get("unavailable_reason"))
    has_captions = _require_bool(raw_case, "has_captions", index=index)
    expected = _require_expected(raw_case, index=index)
    return BenchmarkCase(
        case_id=case_id,
        family=family,
        source_type=source_type,
        video_id=video_id,
        source_url=source_url,
        title=title,
        description=description,
        duration=duration,
        privacy_status=privacy_status,
        upload_status=upload_status,
        is_live_content=is_live_content,
        unavailable_reason=unavailable_reason,
        has_captions=has_captions,
        expected=expected,
    )


def _require_text(raw_case: dict[str, Any], key: str, *, index: int) -> str:
    value = raw_case.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"case[{index}].{key} must be a non-empty string")
    return value.strip()


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    raise ValueError("optional text fields must be strings or null")


def _optional_int(value: Any, *, field_name: str, index: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"case[{index}].{field_name} must be an integer or null")
    return value


def _require_bool(raw_case: dict[str, Any], key: str, *, index: int) -> bool:
    value = raw_case.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"case[{index}].{key} must be a boolean")
    return value


def _require_expected(raw_case: dict[str, Any], *, index: int) -> dict[str, bool]:
    expected = raw_case.get("expected")
    if not isinstance(expected, dict):
        raise ValueError(f"case[{index}].expected must be an object")
    missing = [key for key in _EXPECTED_KEYS if key not in expected]
    if missing:
        raise ValueError(f"case[{index}].expected is missing keys: {', '.join(missing)}")
    extra = [key for key in expected if key not in _EXPECTED_KEYS]
    if extra:
        raise ValueError(f"case[{index}].expected contains unexpected keys: {', '.join(extra)}")
    parsed: dict[str, bool] = {}
    for key in _EXPECTED_KEYS:
        value = expected.get(key)
        if not isinstance(value, bool):
            raise ValueError(f"case[{index}].expected.{key} must be a boolean")
        parsed[key] = value
    return parsed
