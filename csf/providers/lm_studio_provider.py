"""LocalModelProvider + OllamaVisionProvider — Tier 1.5 local inference via LM Studio or Ollama."""

from __future__ import annotations

import os
import re
from typing import Any

import httpx

from csf.providers import AnalysisProvider, VideoAnalysisResult, NonFatalAnalysisError
from csf.providers import TranscriptProvider

DEFAULT_LM_STUDIO_URL = "http://localhost:1234/v1"
LM_STUDIO_URL = os.environ.get("LM_STUDIO_URL", DEFAULT_LM_STUDIO_URL).rstrip("/")


class LocalModelProvider:
    __slots__ = ()

    def analyze(self, video_id: str, video_url: str, **kwargs: Any) -> VideoAnalysisResult:
        try:
            transcript = TranscriptProvider().analyze(video_id, video_url)
        except NonFatalAnalysisError:
            raise NonFatalAnalysisError(
                f"LocalModelProvider: transcript fetch failed for {video_id}"
            )

        system_prompt = (
            "You are a video analysis assistant. Given the transcript, produce a JSON object "
            "with: title (string), summary (string, 1-2 sentences), key_topics (list of 3-5 strings), "
            "key_points (list of 3-5 strings). Respond ONLY with valid JSON."
        )
        model = os.environ.get("LM_STUDIO_MODEL", "google/gemma-4-31b")
        user_prompt = f"Video URL: {video_url}\n\nTranscript:\n{transcript.summary[:8000]}"

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.3,
            "max_tokens": 1024,
        }

        try:
            with httpx.Client(timeout=httpx.Timeout(120.0)) as client:
                resp = client.post(f"{LM_STUDIO_URL}/chat/completions", json=payload)
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            raise NonFatalAnalysisError(f"LocalModelProvider: LM Studio call failed: {e}")

        import json
        try:
            match = re.search(r"\{.*\}", content, re.DOTALL)
            data = json.loads(match.group()) if match else json.loads(content)
            return VideoAnalysisResult(
                title=data.get("title", ""),
                summary=data.get("summary", ""),
                key_topics=data.get("key_topics", []),
                key_points=data.get("key_points", []),
                mode="local_model",
            )
        except Exception as e:
            raise NonFatalAnalysisError(f"LocalModelProvider: JSON parse failed: {e}")


_OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
_OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma3:12b")


class OllamaVisionProvider:
    __slots__ = ()

    def analyze(self, video_id: str, video_url: str, **kwargs: Any) -> VideoAnalysisResult:
        try:
            transcript = TranscriptProvider().analyze(video_id, video_url)
        except NonFatalAnalysisError:
            raise NonFatalAnalysisError(
                f"OllamaVisionProvider: transcript fetch failed for {video_id}"
            )

        system_prompt = (
            "You are a video analysis assistant. Given the transcript, produce a JSON object "
            "with: title, summary, key_topics, key_points. Respond ONLY with valid JSON."
        )
        payload = {
            "model": _OLLAMA_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Video: {video_url}\nTranscript: {transcript.summary[:8000]}"},
            ],
            "stream": False,
            "options": {"temperature": 0.3, "num_predict": 1024},
        }

        try:
            with httpx.Client(timeout=httpx.Timeout(120.0)) as client:
                resp = client.post(f"{_OLLAMA_URL}/api/chat", json=payload)
                resp.raise_for_status()
                content = resp.json()["message"]["content"]
        except Exception as e:
            raise NonFatalAnalysisError(f"OllamaVisionProvider: Ollama call failed: {e}")

        import json
        try:
            match = re.search(r"\{.*\}", content, re.DOTALL)
            data = json.loads(match.group()) if match else json.loads(content)
            return VideoAnalysisResult(
                title=data.get("title", ""),
                summary=data.get("summary", ""),
                key_topics=data.get("key_topics", []),
                key_points=data.get("key_points", []),
                mode="ollama_vision",
            )
        except Exception as e:
            raise NonFatalAnalysisError(f"OllamaVisionProvider: JSON parse failed: {e}")
