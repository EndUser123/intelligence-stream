# ADR-20260403: LocalModelProvider — Windows-Native Vision-Language Provider + TurboQuant Outlook

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

### LocalModelProvider (Tier 1.5)

Add a new `LocalModelProvider` implementing the `AnalysisProvider` protocol, running Gemma 4 via **LM Studio** (Windows-native, GPU-accelerated). A separate `OllamaVisionProvider` is also defined as a swap-in backend.

**Provider priority chain:**
```
TranscriptProvider(cached) → LocalModelProvider → GeminiSDKProvider → OcrClipProvider → TranscriptProvider(uncached)
```

### Backend Selection

LM Studio and Ollama both expose an OpenAI-compatible API. The provider is instantiated via a config-driven backend selector:

| Backend | Gemma 4 available | Native Windows | TurboQuant (current) | Notes |
|---------|-----------------|----------------|---------------------|-------|
| **LM Studio** | **YES** — Gemma 4 31B/27B listed in catalog | YES | No (llama.cpp build pending) | Use for Windows today |
| Ollama | No — gemma4 PR pending | YES | PR #15090 open, not merged | Swap-in when Gemma 4 lands |

**TurboQuant path**: Once LM Studio updates its bundled llama.cpp to include TurboQuant KV cache types (`TBQ3_0`/`TBQ4_0`, llama.cpp PRs #21089/#21307), LM Studio gains TurboQuant with no API change — only a model reload with the TurboQuant quant type is needed.

**Dual-provider architecture**: Both backends implement `AnalysisProvider`. The orchestrator selects one via config. This means TurboQuant adoption is a provider swap, not an architecture change.

### TurboQuant Status (2026-04-03)

TurboQuant is 2 weeks old (ICLR 2026, March 25). No desktop platform has shipped it yet.

| Platform | Status |
|----------|--------|
| **MLX (Apple Silicon)** | Shipped — PR merged in `mlx-lm` |
| **llama.cpp** | In progress — PRs #21089 (CPU), #21307 (CUDA/GPU) |
| **vLLM** | In progress — PRs #38280, #38479 |
| **LM Studio** | Feature request #1719 — no timeline |
| **Ollama** | PR #15090 open — not merged |
| **Windows native** | No path yet |

`turboquant-vllm` (third-party) is the fastest on-ramp for TurboQuant but requires vLLM + WSL2, which conflicts with the Windows-native preference.

---

## Implementation

### File: `csf/providers/lm_studio_provider.py`

```python
"""LocalModelProvider — Tier 1.5 local vision-language analysis via LM Studio (OpenAI-compatible API).

LM Studio (lmstudio.ai) is the recommended backend for Windows-native deployment.
Gemma 4 models are available in the LM Studio model catalog today.

Swap backend: set LM_STUDIO_BACKEND_URL to switch between LM Studio and Ollama.
Both expose the same OpenAI-compatible /v1/chat/completions interface.
"""

from __future__ import annotations

import os
import re
from typing import Any

import httpx

from csf.providers import AnalysisProvider, VideoAnalysisResult, NonFatalAnalysisError
from csf.providers import TranscriptProvider

# Default LM Studio server URL (starts automatically when LM Studio app is open)
DEFAULT_LM_STUDIO_URL = "http://localhost:1234/v1"
LM_STUDIO_URL = os.environ.get("LM_STUDIO_URL", DEFAULT_LM_STUDIO_URL).rstrip("/")


class LocalModelProvider:
    """Tier 1.5 — Local Gemma 4 via LM Studio OpenAI-compatible API."""

    __slots__ = ()

    def analyze(self, video_id: str, video_url: str, **kwargs: Any) -> VideoAnalysisResult:
        # 1. Fetch transcript
        try:
            transcript = TranscriptProvider().analyze(video_id, video_url)
        except NonFatalAnalysisError:
            raise NonFatalAnalysisError(
                f"LocalModelProvider: transcript fetch failed for {video_id}"
            )

        # 2. Call LM Studio (OpenAI-compatible API)
        system_prompt = (
            "You are a video analysis assistant. Given the transcript, produce a JSON object "
            "with: title (string), summary (string, 1-2 sentences), key_topics (list of 3-5 strings), "
            "key_points (list of 3-5 strings). Respond ONLY with valid JSON."
        )
        user_prompt = f"Video URL: {video_url}\n\nTranscript:\n{transcript.summary[:8000]}"

        # Model loaded in LM Studio — must match a model in LM Studio's model catalog.
        # Gemma 4: google/gemma-4-31b, google/gemma-4-27b, google/gemma-4-E4B-it (when available)
        model = os.environ.get("LM_STUDIO_MODEL", "google/gemma-4-31b")

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

        # 3. Parse JSON → VideoAnalysisResult
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


class OllamaVisionProvider:
    """Tier 1.5 — Local Gemma via Ollama (swap-in backend when Gemma 4 lands in Ollama)."""

    __slots__ = ()

    OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
    OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma3:12b")  # Gemma 3 until Gemma 4 PR merges

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
            "model": self.OLLAMA_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Video: {video_url}\nTranscript: {transcript.summary[:8000]}"},
            ],
            "stream": False,
            "options": {"temperature": 0.3, "num_predict": 1024},
        }

        try:
            with httpx.Client(timeout=httpx.Timeout(120.0)) as client:
                resp = client.post(f"{self.OLLAMA_URL}/api/chat", json=payload)
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
- LM Studio has Gemma 4 in catalog today — no waiting for upstream PR merges
- Native Windows GPU acceleration — no WSL2 required
- No subprocess management needed — LM Studio runs as a standalone app
- Dual-provider design: TurboQuant adoption is a config swap, not a code change

**Negative:**
- LM Studio GUI must be open (or server started) for the provider to connect
- TurboQuant not yet available on Windows native (llama.cpp PRs still in review)
- 26B A4B MoE on 12GB still marginal without TurboQuant

**Risks:**
- Gemma 4 E4B may not be in LM Studio catalog yet (31B is confirmed available; check for E4B)
- LM Studio's bundled llama.cpp must be updated for TurboQuant support (feature request #1719)

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
- LM Studio Gemma 4 models: lmstudio.ai/models/gemma-4 — Gemma 4 31B and 27B available in catalog
- TurboQuant (Google Research, ICLR 2026): 6x KV cache memory reduction, 8x speedup on H100
- llama.cpp TurboQuant: PRs #21089 (CPU), #21307 (CUDA/GPU) — in review, not merged
- Ollama TurboQuant: Issue #15051, PR #15090 — in review, not merged
- LM Studio TurboQuant: Feature request #1719 on lmstudio-ai/lmstudio-bug-tracker
- IS provider protocol: `csf/providers/__init__.py` — `AnalysisProvider` + `VideoAnalysisResult`
- IS orchestrator: `csf/orchestrator.py` — `select_provider()` with GAUC failure-aware routing
