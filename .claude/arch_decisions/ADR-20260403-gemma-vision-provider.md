# ADR-20260403: GemmaVisionProvider — Local Vision-Language Provider + TurboQuant KV Compression

**Status:** Proposed
**Date:** 2026-04-03
**Deciders:** Bruce Thomson

---

## Context

Intelligence Stream (IS) uses a tiered provider architecture for video analysis:
- **Tier 1:** Gemini SDK (high quality, API cost, rate limited)
- **Tier 2:** OCR + CLIP + LLM (medium quality, compute-intensive)
- **Tier 3:** Transcript-only (low quality, free, fast)

Two problems motivate this ADR:

1. **Provider availability**: Gemini SDK and yt-dlp both returned HTTP 429 (rate limited) during a transcript capture run, blocking all video analysis. A local provider eliminates external API dependencies.

2. **VRAM constraint**: RTX 5070 has 12GB VRAM. Gemma 4 sizes:
   - E2B (2.3B eff.): ~2.5GB Q4 — fits easily, but least capable
   - **E4B (4.5B eff.): ~5GB Q4_0 — optimal for IS on 12GB**
   - 26B A4B MoE (3.8B active / 25B total): Would need ~9-10GB — marginal with TurboQuant
   - 31B Dense (30.7B): ~32GB — does not fit

**TurboQuant** (Google Research, ICLR 2026) achieves 6x KV cache memory reduction via PolarQuant + QJL, enabling potentially the 26B A4B MoE to fit in 12GB or providing more headroom for E4B.

---

## Decision

### GemmaVisionProvider (Tier 1.5)

Add a new `GemmaVisionProvider` implementing the `AnalysisProvider` protocol, running Gemma 4 via vLLM's OpenAI-compatible API.

**Provider priority chain:**
```
TranscriptProvider(cached) → GemmaVisionProvider → GeminiSDKProvider → OcrClipProvider → TranscriptProvider(uncached)
```

### GemmaServer — Subprocess Lifecycle Manager

`GemmaServer` is a process-supervisor singleton (threading.Lock) managing the vLLM server lifecycle:
- `start()` — launch `vllm serve` subprocess, wait for `/health` endpoint
- `stop()` — kill subprocess
- `is_ready()` — HTTP GET `http://localhost:8000/health`
- Auto-starts on first `GemmaVisionProvider.analyze()` call if not running

### TurboQuant Integration (Future)

`turboquant-vllm` (v1.3.0, third-party) provides Gemma KV cache compression:
- **3.76x KV compression** on vision models (Gemma + Molmo2 validated)
- 6x memory reduction, 8x attention computation speedup on H100
- Zero accuracy loss, training-free, data-oblivious

Two integration paths:
1. **Near-term**: `turboquant-vllm` installed alongside vLLM, started with `--quantization turboquant`
2. **Long-term**: Native vLLM TurboQuant support (issue #38171, CUDA/Triton kernels being developed)

With TurboQuant on E4B: VRAM drops from ~5GB to ~0.8GB for KV cache, leaving more headroom for activations or enabling 26B A4B MoE on 12GB.

---

## Implementation

### File: `csf/providers/gemma_vision_provider.py`

```python
"""GemmaVisionProvider — Tier 1.5 local vision-language analysis via vLLM."""

from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

from csf.providers import AnalysisProvider, VideoAnalysisResult, NonFatalAnalysisError
from csf.transcript import fetch_transcript

_health_lock = threading.Lock()
_server_lock = threading.Lock()
_server: subprocess.Popen | None = None
_server_ready = False


def _wait_for_server(timeout: float = 60.0) -> None:
    """Poll /health until ready or timeout."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            resp = httpx.get("http://localhost:8000/health", timeout=2.0)
            if resp.status_code == 200:
                return
        except (httpx.ConnectError, httpx.ReadError):
            pass
        time.sleep(1.0)
    raise RuntimeError("GemmaServer failed to start within 60s")


class GemmaServer:
    """Subprocess manager for vLLM serving Gemma 4."""

    __slots__ = ()

    @staticmethod
    def start() -> None:
        global _server, _server_ready
        with _server_lock:
            if _server is not None and _server.poll() is None:
                return  # already running
            cmd = [
                "vllm", "serve", "google/gemma-4-E4B-it",
                "--tensor-parallel-size", "1",
                "--port", "8000",
                "--quantization", "fp8",      # or "turboquant" when available
                "--max-model-len", "32768",
            ]
            _server = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            _server_ready = False
        _wait_for_server()
        _server_ready = True

    @staticmethod
    def stop() -> None:
        global _server, _server_ready
        with _server_lock:
            if _server is not None:
                _server.terminate()
                _server.wait(timeout=10)
                _server = None
                _server_ready = False

    @staticmethod
    def is_ready() -> bool:
        return _server_ready


class GemmaVisionProvider:
    """Tier 1.5 — Local Gemma 4 E4B via vLLM OpenAI-compatible API."""

    __slots__ = ()

    def analyze(self, video_id: str, video_url: str, **kwargs: Any) -> VideoAnalysisResult:
        # 1. Start server if not running
        GemmaServer.start()

        # 2. Fetch transcript
        from csf.providers import TranscriptProvider
        try:
            transcript = TranscriptProvider().analyze(video_id, video_url)
        except NonFatalAnalysisError:
            raise NonFatalAnalysisError(
                f"GemmaVisionProvider: transcript fetch failed for {video_id}"
            )

        # 3. Call vLLM (OpenAI-compatible API)
        system_prompt = (
            "You are a video analysis assistant. Given the transcript, produce a JSON object "
            "with: title (string), summary (string, 1-2 sentences), key_topics (list of 3-5 strings), "
            "key_points (list of 3-5 strings). Respond ONLY with valid JSON."
        )
        user_prompt = f"Video URL: {video_url}\n\nTranscript:\n{transcript.summary[:8000]}"

        payload = {
            "model": "google/gemma-4-E4B-it",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.3,
            "max_tokens": 1024,
        }

        try:
            with httpx.Client(timeout=httpx.Timeout(60.0)) as client:
                resp = client.post("http://localhost:8000/v1/chat/completions", json=payload)
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            raise NonFatalAnalysisError(f"GemmaVisionProvider: vLLM call failed: {e}")

        # 4. Parse JSON → VideoAnalysisResult
        import json, re
        try:
            # Strip markdown code fences
            match = re.search(r"\{.*\}", content, re.DOTALL)
            data = json.loads(match.group()) if match else json.loads(content)
            return VideoAnalysisResult(
                title=data.get("title", ""),
                summary=data.get("summary", ""),
                key_topics=data.get("key_topics", []),
                key_points=data.get("key_points", []),
                mode="gemma_vision",
            )
        except Exception as e:
            raise NonFatalAnalysisError(f"GemmaVisionProvider: JSON parse failed: {e}")
```

### Insert into orchestrator.py

In `select_provider()` candidate list, insert `"gemma_vision"` after cached transcript check and before `"gemini_sdk"`:

```python
candidates = ["gemini_vision", "gemini_sdk", "ocr_clip", "transcript"]
```

---

## Consequences

**Positive:**
- Zero external API dependency for Tier 1.5 — immune to Gemini SDK 429s
- Local GPU inference — no per-video API cost
- TurboQuant can push 26B A4B MoE onto 12GB (if native vLLM lands)
- Transcript fetch + analysis in one pipeline step

**Negative:**
- Requires vLLM installation on target machine
- Server cold-start adds ~10-15s latency on first call
- Must manage GPU VRAM separately from system RAM

**Risks:**
- vLLM subprocess management on Windows — tested on WSL/Linux only initially
- TurboQuant native vLLM integration is not yet merged (issue #38171)
- Gemma 4 E4B Q4_0 still requires ~5GB VRAM for weights + activations

---

## Hardware Guidance

| Model | Quantization | VRAM (weights) | VRAM (KV cache, no TurboQuant) | VRAM (KV cache, TurboQuant 3.76x) |
|-------|-------------|----------------|-------------------------------|----------------------------------|
| E2B Q4 | Q4_0 | ~1.2GB | ~0.3GB | ~0.08GB |
| **E4B Q4** | Q4_0 | **~2.5GB** | **~0.8GB** | **~0.2GB** |
| 26B A4B MoE | FP8 | ~9GB | ~2.5GB | ~0.7GB |
| 31B Dense | FP8 | ~16GB | ~4GB | ~1.1GB |

**Recommendation for RTX 5070 12GB:** E4B Q4_0 with TurboQuant KV compression — leaves ~10GB for activations and 10+ video frames in CLIP.

---

## References

- Gemma 4 (Google DeepMind, April 2, 2026): apache/license — E2B/E4B/26B A4B MoE/31B Dense
- TurboQuant (Google Research, ICLR 2026): 6x KV cache memory reduction, 8x speedup on H100
- `turboquant-vllm` v1.3.0: github.com/turboderp/turboquant-vllm — Gemma + Molmo2 validated, 3.76x KV compression on vision models
- vLLM issue #38171: Native TurboQuant KV cache support in progress (CUDA/Triton kernels)
- IS provider protocol: `csf/providers/__init__.py` — `AnalysisProvider` + `VideoAnalysisResult`
- IS orchestrator: `csf/orchestrator.py` — `select_provider()` with GAUC failure-aware routing
