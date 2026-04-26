# Whisper Admission Policy Design

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a conservative admission policy before Whisper so no-caption videos only pay the Whisper cost when they are plausibly spoken-word, while preserving the current one-shot fallback behavior and caching terminal outcomes.

**Architecture:** The transcript chain already has a final Whisper stage and already classifies empty Whisper results with a conservative `no_speech_prob` hint. We will add a small pre-Whisper gate that uses cheap metadata signals to skip obvious non-speech or terminal cases, treat duration only as a weak tiebreaker, and leave the actual Whisper attempt unchanged for the ambiguous remainder. Whisper itself remains the source of truth for terminal fallback outcomes.

**Tech Stack:** Python 3.14, `csf.transcript`, `csf_source` fetch routing, existing transcript cache, pytest.

---

## Problem

We already know Whisper can recover some no-caption videos, but it is expensive enough that we should not spend it on obvious non-speech or terminal cases. At the same time, we cannot know for sure whether a no-caption clip contains speech before we try Whisper. The current behavior needs a narrow admission gate that stays conservative and does not wrongly skip valid speech clips such as short spoken videos.

## Proposed Behavior

- Add a cheap pre-Whisper admission helper.
- Hard-exclude only when metadata already makes the item very likely terminal or non-speech:
  - deleted
  - private
  - removed
  - live / live_stream / premiere
  - explicit non-speech title cues such as:
    - `official audio`
    - `music video`
    - `instrumental`
    - `cover`
    - `remix`
    - `dance`
    - `karaoke`
    - `lyrics`
- Treat duration only as a weak tie-breaker when title/channel cues already lean non-speech.
  - Duration must not skip a clip by itself.
  - Short clips remain eligible when the metadata suggests speech.
- Try Whisper once on the remaining ambiguous no-caption VODs.
- Cache the final fallback outcome by video identity:
  - Whisper success -> cache transcript
  - Whisper empty result with high `no_speech_prob` -> cache as likely music or silence
  - other terminal Whisper failure -> cache that terminal reason
- Never retry the same terminal fallback outcome for the same video.

## Scope

In scope:
- A shared helper for pre-Whisper admission decisions.
- Whisper-admission tests covering the cheap metadata gate and the weak duration rule.
- Cache/outcome tests for the terminal Whisper result path.

Out of scope:
- A new transcript backend.
- A change to the Whisper model itself.
- A hard value-based gate that tries to predict user usefulness instead of speech likelihood.

## Success Criteria

- Obvious non-speech and terminal items are excluded before Whisper.
- Short clips are still eligible when metadata suggests speech.
- Duration alone never blocks a valid ambiguous clip.
- Whisper is still tried once on the ambiguous remainder.
- Empty Whisper results with high `no_speech_prob` are cached as likely music or silence and are not retried.
- Existing successful Whisper recovery behavior remains unchanged.

## Risks

- Over-aggressive title matching could wrongly skip speech clips with musical or performance words in the title. Keep the list narrow and explicit.
- Duration-based filtering could become too broad if it turns into a hard threshold. It should remain only a weak tiebreaker.
- Terminal caching must stay keyed to the specific video identity so we do not accidentally conflate separate videos with similar titles.

