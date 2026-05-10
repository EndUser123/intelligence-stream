# NLM Config Centralization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put the NotebookLM runtime knobs in one shared module so the batch policy and auth policy can be changed from a single place.

**Architecture:** Add a shared `csf/nlm_config.py` module that owns the NotebookLM config dataclass and singleton accessors. Update `csf/transcript.py` and `csf/nlm_batch.py` to import that shared config instead of maintaining separate defaults. Keep the config split by concern inside one module so the notebook policy and auth policy stay easy to find without duplicating values.

**Tech Stack:** Python 3.11, dataclasses, pytest

---

### Task 1: Create the shared NotebookLM config module

**Files:**
- Create: `P:\\\\\\packages/yt-is/csf/nlm_config.py`
- Test: `P:\\\\\\packages/yt-is/tests/test_nlm_config.py`

- [ ] **Step 1: Write the failing test**

```python
from csf import nlm_config

def test_shared_defaults_cover_batch_and_auth_policy():
    cfg = nlm_config.get_nlm_config()
    assert cfg.notebook_batch_size == 50
    assert cfg.notebook_source_cap == 50
    assert cfg.notebook_source_materialization_timeout_s == 600
    assert cfg.max_sources_per_notebook == 300
    assert cfg.auth_check_interval == 60.0
    assert cfg.auth_max_calls_per_window == 10
    assert cfg.auth_cooldown == 300.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=P:\\\\\\packages\yt-is python -m pytest $CLAUDE_PLUGIN_ROOT/tests\test_nlm_config.py -q`
Expected: FAIL because `csf.nlm_config` does not exist yet.

- [ ] **Step 3: Write minimal implementation**

```python
from dataclasses import dataclass
import os
import threading

@dataclass(frozen=True)
class NLMConfig:
    notebook_batch_size: int = 50
    notebook_source_cap: int = 50
    notebook_source_materialization_timeout_s: int = 600
    max_sources_per_notebook: int = 300
    auth_check_interval: float = 60.0
    auth_max_calls_per_window: int = 10
    auth_cooldown: float = 300.0

def get_nlm_config() -> NLMConfig:
    ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=P:\\\\\\packages\yt-is python -m pytest $CLAUDE_PLUGIN_ROOT/tests\test_nlm_config.py -q`
Expected: PASS.

### Task 2: Switch transcript auth logic to the shared config

**Files:**
- Modify: `P:\\\\\\packages/yt-is/csf/transcript.py`
- Test: `P:\\\\\\packages/yt-is/tests/test_transcript.py`

- [ ] **Step 1: Write the failing test**

```python
from csf import nlm_config, transcript

def test_transcript_uses_shared_nlm_config():
    cfg = transcript.get_nlm_config()
    assert cfg == nlm_config.get_nlm_config()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=P:\\\\\\packages\yt-is python -m pytest $CLAUDE_PLUGIN_ROOT/tests\test_transcript.py -q`
Expected: FAIL until `transcript.py` imports the shared module.

- [ ] **Step 3: Write minimal implementation**

```python
from csf.nlm_config import NLMConfig, get_nlm_config, set_nlm_config
```

Remove the duplicated `NLMConfig` dataclass and singleton code from `transcript.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=P:\\\\\\packages\yt-is python -m pytest $CLAUDE_PLUGIN_ROOT/tests\test_transcript.py -q`
Expected: PASS.

### Task 3: Switch batch policy defaults to the shared config

**Files:**
- Modify: `P:\\\\\\packages/yt-is/csf/nlm_batch.py`
- Modify: `P:\\\\\\packages/yt-is/bin/csf-source`
- Modify: `P:\\\\\\packages/yt-is/bin/nlm-subbatch-sweep`
- Test: `P:\\\\\\packages/yt-is/tests/test_nlm_batch.py`

- [ ] **Step 1: Write the failing test**

```python
from csf import nlm_config, nlm_batch

def test_batch_defaults_come_from_shared_config():
    cfg = nlm_config.get_nlm_config()
    assert nlm_batch.DEFAULT_NOTEBOOKLM_BATCH_SIZE == cfg.notebook_batch_size
    assert nlm_batch.DEFAULT_NOTEBOOKLM_SOURCE_CAP == cfg.notebook_source_cap
    assert nlm_batch.DEFAULT_NOTEBOOKLM_SOURCE_MATERIALIZATION_TIMEOUT_S == cfg.notebook_source_materialization_timeout_s
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=P:\\\\\\packages\yt-is python -m pytest $CLAUDE_PLUGIN_ROOT/tests\test_nlm_batch.py -q`
Expected: FAIL until `nlm_batch.py` imports the shared config values.

- [ ] **Step 3: Write minimal implementation**

```python
from csf.nlm_config import get_nlm_config

_CONFIG = get_nlm_config()
DEFAULT_NOTEBOOKLM_BATCH_SIZE = _CONFIG.notebook_batch_size
DEFAULT_NOTEBOOKLM_SOURCE_CAP = _CONFIG.notebook_source_cap
DEFAULT_NOTEBOOKLM_SOURCE_MATERIALIZATION_TIMEOUT_S = _CONFIG.notebook_source_materialization_timeout_s
```

Update `bin/csf-source` and `bin/nlm-subbatch-sweep` to import the shared defaults indirectly through `csf.nlm_batch`.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=P:\\\\\\packages\yt-is python -m pytest $CLAUDE_PLUGIN_ROOT/tests\test_nlm_batch.py -q`
Expected: PASS.

### Task 4: Verify the repo-wide regression slice

**Files:**
- Test: `P:\\\\\\packages/yt-is/tests/test_nlm_config.py`
- Test: `P:\\\\\\packages/yt-is/tests/test_nlm_batch.py`
- Test: `P:\\\\\\packages/yt-is/tests/test_transcript.py`

- [ ] **Step 1: Run the focused test slice**

Run: `PYTHONPATH=P:\\\\\\packages\yt-is python -m pytest $CLAUDE_PLUGIN_ROOT/tests\test_nlm_config.py $CLAUDE_PLUGIN_ROOT/tests\test_nlm_batch.py $CLAUDE_PLUGIN_ROOT/tests\test_transcript.py -q`

- [ ] **Step 2: Confirm the new config module is the single edit point**

Check that `csf/nlm_config.py` is the only file that defines the NotebookLM defaults and that both `transcript.py` and `nlm_batch.py` import from it.

