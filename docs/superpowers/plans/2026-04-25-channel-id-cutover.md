# Channel ID Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `channel_id` the canonical storage identity for channel-state tables so malformed and drifting channel URLs can never become live state again.

**Architecture:** Keep `channel_url` only as display/alias data, resolve every writer input to a stable `channel_id`, and migrate the live SQLite tables in one cutover so reads, writes, block checks, and replays all use the same identity model. The implementation should preserve current behavior at the command surface while tightening storage semantics underneath.

**Tech Stack:** Python 3, `sqlite3`, `pytest`, existing `yt-is` CLI scripts, existing YouTube URL/ID resolution helpers.

---

### Task 1: Add a shared channel-identity helper and pin the canonical URL contract

**Files:**
- Create: `csf/channel_identity.py`
- Modify: `csf/batch_status.py:1-40, 1619-1688`
- Modify: `csf/source_enumerator.py:223-290, 477-520`
- Test: `tests/test_source_enumerator.py`
- Test: `tests/test_batch_status.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_resolve_channel_identity_returns_same_channel_id_for_handle_and_uc():
    handle = resolve_channel_identity("https://www.youtube.com/@RyanRumsey")
    uc = resolve_channel_identity("https://www.youtube.com/channel/UCZ5zIdFEqD_u9EohzswYI3Q")
    assert handle.channel_id == uc.channel_id
    assert handle.canonical_url == "https://www.youtube.com/@RyanRumsey"


def test_normalize_channel_url_repairs_missing_slash_handle_form():
    assert normalize_channel_url("https://www.youtube.com@ryanrumsey") == "https://www.youtube.com/@ryanrumsey"
```

- [ ] **Step 2: Run the focused tests and confirm they fail**

Run:
```powershell
python -m pytest P:\packages\yt-is\tests\test_source_enumerator.py -q
python -m pytest P:\packages\yt-is\tests\test_batch_status.py -q
```

Expected:
- the new identity tests fail because the helper does not exist yet
- the malformed handle URL case still persists or normalizes incompletely

- [ ] **Step 3: Implement the helper and route all normalization through it**

```python
@dataclass(frozen=True)
class ChannelIdentity:
    channel_id: str
    canonical_url: str
    input_url: str


def resolve_channel_identity(channel_ref: str) -> ChannelIdentity | None:
    parsed = parse_channel_url(channel_ref)
    if not parsed:
        return None
    if parsed.startswith("UC"):
        return ChannelIdentity(
            channel_id=parsed,
            canonical_url=f"https://www.youtube.com/channel/{parsed}",
            input_url=channel_ref,
        )
    uc_id = resolve_to_uc_channel_id(parsed)
    if not uc_id:
        return None
    canonical_url = f"https://www.youtube.com/{parsed}" if parsed.startswith("@") else f"https://www.youtube.com/channel/{uc_id}"
    return ChannelIdentity(channel_id=uc_id, canonical_url=canonical_url, input_url=channel_ref)
```

Also update `batch_status._normalize_channel_url()` so every write/read path repairs:
- `https://www.youtube.com@handle`
- `/channel/@handle`
- bare `@handle`

- [ ] **Step 4: Re-run the focused tests and confirm they pass**

Run:
```powershell
python -m pytest P:\packages\yt-is\tests\test_source_enumerator.py -q
python -m pytest P:\packages\yt-is\tests\test_batch_status.py -q
```

Expected:
- the malformed handle URL normalizes to `https://www.youtube.com/@handle`
- `resolve_channel_identity()` returns the same `channel_id` for `@handle` and `/channel/UC...`

- [ ] **Step 5: Commit the helper layer**

```powershell
git add P:\packages\yt-is\csf\channel_identity.py P:\packages\yt-is\csf\batch_status.py P:\packages\yt-is\csf\source_enumerator.py P:\packages\yt-is\tests\test_source_enumerator.py P:\packages\yt-is\tests\test_batch_status.py
git commit -m "feat: add canonical channel identity helper"
```

### Task 2: Migrate the live SQLite schema to `channel_id`

**Files:**
- Modify: `csf/batch_status.py:250-320, 844-1188, 1551-1808`
- Test: `tests/test_batch_status.py`

- [ ] **Step 1: Write a failing migration test against an old URL-keyed schema**

```python
def test_channel_id_migration_preserves_block_and_metadata(tmp_path):
    old_db = tmp_path / "batch_status.sqlite"
    conn = sqlite3.connect(old_db)
    conn.executescript("""
        CREATE TABLE channel_metadata (
            channel_url TEXT PRIMARY KEY,
            playlist_id TEXT,
            last_checked TEXT NOT NULL,
            video_count_estimate INTEGER DEFAULT 0
        );
        CREATE TABLE channel_blocklist (
            channel_url TEXT PRIMARY KEY,
            blocked_at TEXT NOT NULL
        );
    """)
    conn.execute(
        "INSERT INTO channel_metadata (channel_url, playlist_id, last_checked, video_count_estimate) VALUES (?, ?, ?, ?)",
        ("https://www.youtube.com@ryanrumsey", "PL123", "2026-04-25T00:00:00Z", 11),
    )
    conn.execute(
        "INSERT INTO channel_blocklist (channel_url, blocked_at) VALUES (?, ?)",
        ("https://www.youtube.com@blocked", "2026-04-25T00:00:00Z"),
    )
    conn.commit()
    conn.close()

    migrate_channel_state_to_channel_id(old_db)

    conn = sqlite3.connect(old_db)
    row = conn.execute(
        "SELECT channel_id, channel_url, playlist_id, video_count_estimate FROM channel_metadata WHERE channel_id = ?",
        ("UCZ5zIdFEqD_u9EohzswYI3Q",),
    ).fetchone()
    blocked = conn.execute(
        "SELECT channel_id, channel_url FROM channel_blocklist WHERE channel_id = ?",
        ("UC_BLOCKED",),
    ).fetchone()
    conn.close()

    assert row == ("UCZ5zIdFEqD_u9EohzswYI3Q", "https://www.youtube.com/@ryanrumsey", "PL123", 11)
    assert blocked == ("UC_BLOCKED", "https://www.youtube.com/@blocked")
```

- [ ] **Step 2: Run the migration test and confirm it fails**

Run:
```powershell
python -m pytest P:\packages\yt-is\tests\test_batch_status.py -q -k migration
```

Expected:
- the helper is not present yet
- the old schema cannot be read by channel ID

- [ ] **Step 3: Implement a one-time SQLite cutover in `batch_status.py`**

Implement a migration path that:
- snapshots the live DB first
- creates v2 tables keyed by `channel_id`
- copies old rows into the new tables after resolving IDs
- preserves `channel_url` as the alias/display field
- updates `channel_blocklist`, `provider_score`, and any channel lookups to use `channel_id`

Prefer an atomic swap pattern over piecemeal updates:

```sql
CREATE TABLE channel_metadata_v2 (... channel_id TEXT PRIMARY KEY, channel_url TEXT NOT NULL, ...);
INSERT INTO channel_metadata_v2 (...)
SELECT ... FROM channel_metadata;
ALTER TABLE channel_metadata RENAME TO channel_metadata_legacy;
ALTER TABLE channel_metadata_v2 RENAME TO channel_metadata;
```

- [ ] **Step 4: Re-run the migration test and the batch-status suite**

Run:
```powershell
python -m pytest P:\packages\yt-is\tests\test_batch_status.py -q
```

Expected:
- old rows survive the migration
- malformed handle rows are rewritten to canonical display URLs
- block state survives under the same `channel_id`

- [ ] **Step 5: Commit the schema cutover**

```powershell
git add P:\packages\yt-is\csf\batch_status.py P:\packages\yt-is\tests\test_batch_status.py
git commit -m "feat: migrate channel state to channel id"
```

### Task 3: Update every writer to resolve and persist `channel_id`

**Files:**
- Modify: `bin/csf-source`
- Modify: `csf/playlist_imports.py`
- Modify: `bin/yt-is`
- Modify: `extract_channels.py`
- Modify: `scripts/backfill_channel_metadata.py`
- Modify: `csf/csf_nlm_ingest.py`
- Modify: `tests/test_playlist_imports.py`
- Modify: `tests/test_yt_is_wrapper.py`

- [ ] **Step 1: Write the failing tests for duplicate prevention and canonical writes**

```python
def test_add_same_channel_via_handle_and_uc_creates_one_row(tmp_path, monkeypatch):
    env = os.environ.copy()
    env["YTIS_BATCH_STATUS_DB_PATH"] = str(tmp_path / "live.sqlite")
    subprocess.run(
        ["python", "P:/packages/yt-is/bin/csf-source", "add", "https://www.youtube.com/@RyanRumsey"],
        check=True,
        env=env,
    )
    subprocess.run(
        ["python", "P:/packages/yt-is/bin/csf-source", "add", "https://www.youtube.com/channel/UCZ5zIdFEqD_u9EohzswYI3Q"],
        check=True,
        env=env,
    )
    conn = sqlite3.connect(tmp_path / "live.sqlite")
    count = conn.execute("SELECT COUNT(*) FROM channel_metadata").fetchone()[0]
    stored = conn.execute("SELECT channel_id FROM channel_metadata WHERE channel_title = ?", ("Ryan Rumsey",)).fetchone()[0]
    conn.close()
    assert count == 1
    assert stored == "UCZ5zIdFEqD_u9EohzswYI3Q"
```

- [ ] **Step 2: Run the tests and confirm the current URL-keyed behavior still duplicates or stores the malformed form**

Run:
```powershell
python -m pytest P:\packages\yt-is\tests\test_playlist_imports.py -q
python -m pytest P:\packages\yt-is\tests\test_yt_is_wrapper.py -q
```

Expected:
- current writer paths are still relying on URL identity in at least one place
- the new duplicate-prevention test fails until the resolver is wired in

- [ ] **Step 3: Resolve channel refs to `channel_id` before any write**

Update each writer to:

1. accept the current URL or handle as input
2. call the new identity resolver
3. store `channel_id` as the key
4. keep `channel_url` as the canonical display alias

Example writer flow:

```python
identity = resolve_channel_identity(source_url)
if identity is None:
    raise SystemExit(f"Could not resolve channel ID for {source_url}")
set_channel_metadata(
    channel_id=identity.channel_id,
    channel_url=identity.canonical_url,
    playlist_id=playlist_id,
    last_checked=now,
    channel_title=channel_title,
)
```

- [ ] **Step 4: Re-run the writer tests and verify no duplicates appear**

Run:
```powershell
python -m pytest P:\packages\yt-is\tests\test_playlist_imports.py -q
python -m pytest P:\packages\yt-is\tests\test_yt_is_wrapper.py -q
```

Expected:
- same channel added via different aliases reuses one stored row
- block/unblock/remove act on the same `channel_id`

- [ ] **Step 5: Commit the writer updates**

```powershell
git add P:\packages\yt-is\bin\csf-source P:\packages\yt-is\csf\playlist_imports.py P:\packages\yt-is\bin\yt-is P:\packages\yt-is\extract_channels.py P:\packages\yt-is\scripts\backfill_channel_metadata.py P:\packages\yt-is\csf\csf_nlm_ingest.py P:\packages\yt-is\tests\test_playlist_imports.py P:\packages\yt-is\tests\test_yt_is_wrapper.py
git commit -m "feat: resolve channel ids before storing channel state"
```

### Task 4: Add a one-shot migration command and clean up the live DB

**Files:**
- Create: `bin/csf-migrate-channel-ids`
- Modify: `HANDOFF.md`
- Modify: `docs/operations/worker-count-trial-run-sheet.md`
- Modify: `docs/operations/worker-owned-notebooks-handoff.md`
- Modify: `skills/yt-nlm/SKILL.md`

- [ ] **Step 1: Write the failing command-level test**

```python
def test_migrate_channel_ids_cli_backups_and_migrates(tmp_path):
    ...
    result = subprocess.run(["python", "P:/packages/yt-is/bin/csf-migrate-channel-ids"], check=True)
    backups = list((tmp_path / "backups").glob("*.sqlite"))
    assert backups
    conn = sqlite3.connect(live_db)
    bad_rows = conn.execute(
        "SELECT COUNT(*) FROM channel_metadata WHERE channel_url LIKE 'https://www.youtube.com@%' OR channel_url LIKE '%/channel/@%'"
    ).fetchone()[0]
    conn.close()
    assert bad_rows == 0
```

- [ ] **Step 2: Run the test and confirm the command does not exist yet**

Run:
```powershell
python -m pytest P:\packages\yt-is\tests\test_batch_status.py -q -k migrate
```

Expected:
- the command-level migration path is missing

- [ ] **Step 3: Implement the CLI and run it against the live `.data` DB**

The CLI should:

1. take a backup snapshot first
2. migrate the live DB in place
3. fail closed if any row cannot be resolved
4. print the before/after row counts for metadata and blocklist tables

- [ ] **Step 4: Re-run the live checks**

Verify:

```powershell
python -m pytest P:\packages\yt-is\tests\test_batch_status.py -q
python -m pytest P:\packages\yt-is\tests\test_playlist_imports.py -q
```

And confirm via direct SQL that:

- `channel_metadata` rows are keyed by `channel_id`
- `channel_blocklist` rows are keyed by `channel_id`
- no malformed `youtube.com@...` rows remain

- [ ] **Step 5: Commit the migration command and doc updates**

```powershell
git add P:\packages\yt-is\bin\csf-migrate-channel-ids P:\packages\yt-is\HANDOFF.md P:\packages\yt-is\docs\operations\worker-count-trial-run-sheet.md P:\packages\yt-is\docs\operations\worker-owned-notebooks-handoff.md P:\packages\yt-is\skills\yt-nlm\SKILL.md
git commit -m "feat: add channel id cutover command"
```

## Review Checklist

Before implementation is considered done:

- every writer resolves to `channel_id`
- every lookup uses `channel_id`
- the live DB has been migrated and backed up
- malformed handle URLs cannot persist
- existing block/allow state survived the cutover
- the command surfaces still accept human-friendly URLs
