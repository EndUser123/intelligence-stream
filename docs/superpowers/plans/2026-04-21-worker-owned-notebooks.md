# Worker-Owned NotebookLM Notebooks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every NotebookLM path reuse exactly one notebook per worker title, clean up duplicate same-title notebooks on startup, and keep the worker notebook reused across batches.

**Architecture:** Centralize notebook ownership in `csf/nlm_batch.py` so every NotebookLM entrypoint resolves notebook identity the same way: exact-title lookup, reuse when there is one match, clean up duplicates through CDP and resolve back to one notebook, and create when there is no match. The worker harness and serial fetch path should both flow through that same ownership rule so the notebook inventory stays deterministic.

**Tech Stack:** Python 3.14, NotebookLM CLI (`nlm`), Chrome DevTools Protocol cleanup via `bin/nlm-puppeteer.js`, pytest.

---

### Task 1: Make notebook ownership title-based everywhere

**Files:**
- Modify: `csf/nlm_batch.py:25-260, 978-1180, 1298-1411, 1410-1520, 1726-1760`
- Test: `tests/test_nlm_batch.py`

- [ ] **Step 1: Write the failing tests**

Add or update tests that pin the ownership contract:

```python
def test_ensure_notebook_reuses_single_exact_title_match():
    # list() returns exactly one notebook titled yt-is-worker-01
    # ensure() should reuse that notebook id and not create a new one
    ...


def test_ensure_notebook_deletes_duplicate_exact_title_matches_and_recreates():
    # list() returns multiple notebooks with the same exact worker title
    # ensure() should call the CDP delete-title cleanup path and then resolve back to one notebook
    ...


def test_create_batch_notebook_uses_owner_title_not_reusable_title():
    # create_batch_notebook() should create the worker-owned title, not the old reusable notebook title
    ...
```

- [ ] **Step 2: Run the tests and confirm the current behavior is still wrong**

Run:

```powershell
PYTHONPATH=P:\packages\yt-is python -m pytest P:\packages\yt-is\tests\test_nlm_batch.py -k "ensure_notebook_reuses_single_exact_title_match or ensure_notebook_deletes_duplicate_exact_title_matches_and_recreates or create_batch_notebook_uses_owner_title_not_reusable_title" -q
```

Expected:
- the new/updated tests fail until the owner-title resolution is implemented

- [ ] **Step 3: Implement the owner-title resolution helper**

Add a small ownership helper in `csf/nlm_batch.py` so the title comes from one place:

```python
_DEFAULT_OWNER_NOTEBOOK_TITLE = "yt-is-worker-01"

def _get_owner_notebook_title() -> str:
    override = os.getenv("YTIS_NLM_OWNER_NOTEBOOK_TITLE", "").strip()
    legacy = os.getenv("YTIS_NLM_REUSABLE_NOTEBOOK_TITLE", "").strip()
    return override or legacy or _DEFAULT_OWNER_NOTEBOOK_TITLE
```

Then update the create/reuse flow so the notebook decision is based on exact-title matches:

```python
def _ensure_owned_notebook(self) -> tuple[bool, str]:
    owner_title = _get_owner_notebook_title()
    notebooks = self._list_notebooks()
    matches = _find_notebooks_with_title(notebooks, owner_title)
    if len(matches) == 1:
        self._nb_id = _notebook_entry_id(matches[0])
        return False, "reuse"
    if len(matches) > 1:
        _delete_worker_notebooks_by_title_with_cdp(owner_title)
        self._nb_id = None
    self._nb_id = self._ingestor.create_batch_notebook(batch_ids, notebook_title=owner_title)
    return True, "create"
```

Update `_rotate_notebook()`, `create_batch_notebook()`, and `_save_reusable_notebook_id()` call sites so the saved notebook title is the owner title, not the old reusable title.

- [ ] **Step 4: Run the focused tests again**

Run:

```powershell
PYTHONPATH=P:\packages\yt-is python -m pytest P:\packages\yt-is\tests\test_nlm_batch.py -q
```

Expected:
- the ownership tests pass
- the existing NotebookLM batch regression tests still pass

### Task 2: Stop deleting the notebook on every worker start

**Files:**
- Modify: `dev/worker_pool/worker_main.py:1-340`
- Modify: `bin/csf-source:2030-2065, 2590-2605`
- Modify: `dev/worker_pool/parallel_batches.py:50-90`

- [ ] **Step 1: Write the failing startup/reuse test**

Add or update a test so the worker startup path proves it reuses the existing worker notebook instead of deleting it first:

```python
def test_worker_startup_reuses_existing_worker_notebook(monkeypatch):
    # retire_reusable_notebook_state() should no longer be required to preserve reuse
    # prepare() should resolve the exact worker title and keep the existing notebook id
    ...
```

- [ ] **Step 2: Run the test and confirm the old startup behavior**

Run:

```powershell
PYTHONPATH=P:\packages\yt-is python -m pytest P:\packages\yt-is\tests\test_dev_worker_pool.py -q
```

Expected:
- the new startup/reuse test fails until the startup delete path is removed or replaced

- [ ] **Step 3: Remove the forced retire step from worker startup**

Update `dev/worker_pool/worker_main.py` so startup no longer calls `retire_reusable_notebook_state()` before `prepare()`.

Keep the flow simple:

```python
ingestor = NLMReusableIngestor()
prepared, setup_mode = ingestor.prepare()
set_reusable_ingestor(ingestor)
```

The owner-title resolution in `csf/nlm_batch.py` should handle stale state, missing notebooks, and duplicate exact-title cleanup.

- [ ] **Step 4: Update the serial fetch path to use the same worker-owned notebook rule**

Update `bin/csf-source` so the production fetch path and the worker harness both use the same worker-owned title logic.

The serial path should resolve to the worker-owned notebook title rather than creating or preserving a standalone reusable notebook.

- [ ] **Step 5: Run focused verification**

Run:

```powershell
PYTHONPATH=P:\packages\yt-is python -m pytest P:\packages\yt-is\tests\test_dev_worker_pool.py -q
PYTHONPATH=P:\packages\yt-is python -m pytest P:\packages\yt-is\tests\test_nlm_batch.py -q
```

Expected:
- worker startup reuse passes
- title-based ownership tests pass

### Task 3: Update docs and verify with a clean live run

**Files:**
- Modify: `HANDOFF.md`
- Modify: `dev/worker_pool/README.md`
- Modify: `docs/operations/nlm-canary-capacity-note.md`
- Modify: `README.md` if needed for the fetch-path description

- [ ] **Step 1: Update the docs to describe the worker-owned model**

Replace “canary” as the system concept with:

- worker run
- worker-owned notebook
- one notebook per worker title

Make the steady-state rule explicit:

```text
yt-is-worker-01
yt-is-worker-02
yt-is-worker-03
yt-is-worker-04
```

and worker notebooks are reused across batches in steady state.

- [ ] **Step 2: Run a short live fetch verification**

Run a short live fetch after the code changes:

```powershell
python P:\packages\yt-is\bin\csf-source fetch --workers 4 --limit 20
```

Verify that:

- each worker title resolves to one notebook
- no extra reusable notebook is created
- duplicate same-title notebooks are cleaned up through the title-resolution path

- [ ] **Step 3: Confirm the notebook inventory is clean**

Run:

```powershell
nlm notebook list --json
```

Expected:
- the only `yt-is::industrial::*` notebooks are the four worker titles
- no duplicate same-title notebooks remain

- [ ] **Step 4: Commit the implementation**

When the tests and the short live run pass, stage and commit the code/doc updates with a single focused message.

## Coverage Check

- Exact-title reuse: Task 1
- Duplicate-title cleanup via CDP: Task 1
- Reuse across batches in steady state: Tasks 1-3
- Worker startup no longer deletes notebooks blindly: Task 2
- Docs aligned with the worker-owned model: Task 3

## Notes

- Keep the old env var names only if you need compatibility, but the behavior must no longer depend on a separate reusable notebook in steady state.
- Do not reintroduce a shared notebook across workers.
- Do not widen the batch size or throughput defaults while doing this work.
