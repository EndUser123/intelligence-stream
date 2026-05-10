# NotebookLM Auth Family Extension Guide

> For agentic workers: use this guide when adding another NotebookLM auth family or lane. The current implementation keeps auth families in `csf/nlm_worker_auth.py`, so adding a new lane is a code + docs + test change, not a JSON-only change.

**Goal:** Add a new NotebookLM account lane without breaking account pinning, CDP isolation, or worker-profile sync.

**Current constraint:** `csf/nlm_worker_auth.py` is the source of truth for auth families. Until that changes, a new lane must be added there, then mirrored into lane JSON, docs, and tests.

---

## What A New Lane Must Own

Each lane needs its own values for all of these fields:

- `lane` name
- `expected_email`
- `source_profile`
- `sibling_profiles`
- `notebooklm_profile_prefix` or an explicit `notebooklm_profiles` list
- `browser_profile_root`
- `browser_profile_directory`
- `worker_state_root`
- `notebook_prefix`
- `cdp_port`

If any of those collide with an existing lane, `csf/sharded_lane_series.py` should reject the config before the benchmark starts.

If you are staging a future lane before `DEFAULT_FAMILIES` has been updated, keep `expected_email` in the lane JSON and use it as the explicit account contract. The harness propagates that value as `YTIS_NLM_EXPECTED_EMAIL` so `doctor` and lane preflight still fail closed on the intended account.

## Files To Update

- `P:\\\\\\packages/yt-is/csf/nlm_worker_auth.py`
- `P:\\\\\\packages/yt-is/.logs/sharded_lane_series/<new_lane_set>.json`
- `P:\\\\\\packages/yt-is/tests/test_nlm_worker_auth.py`
- `P:\\\\\\packages/yt-is/tests/test_sharded_lane_series.py`
- `P:\\\\\\packages/yt-is/docs/operations/sharded-lane-series.md`
- `P:\\\\\\packages/yt-is/docs/operations/notebooklm-auth-robustness-test-plan.md`
- `P:\\\\\\packages/yt-is/docs/operations/test-registry.md` if the new lane is used in a benchmark result

## Recommended Extension Order

### 1. Add the auth family in code

Add a new `AuthFamily` entry to `DEFAULT_FAMILIES` in `csf/nlm_worker_auth.py`.

Use a unique Chrome root and port. The existing pattern is:

- Pro: `18870`
- Free: `18871`
- Free2: `18872`

A 4th family should follow the next unused port, for example `18873`, and should get its own persistent browser root.

Example:

```python
AuthFamily(
    source_profile="ytis-free3-worker-01",
    sibling_profiles=(
        "ytis-free3-worker-02",
        "ytis-free3-worker-03",
        "ytis-free3-worker-04",
    ),
    expected_email="new.account@example.com",
    cdp_browser_root=r"P:\\\\\\.data\yt-is\browser\notebooklm-free-3",
    cdp_browser_profile_directory="Default",
    cdp_port=18873,
),
```

### 2. Add the lane config

Add the new lane to the sharded lane JSON file that the run will use.

Example:

```json
{
  "lane": "new_account_free",
  "account_class": "free",
  "workers": 4,
  "notebooklm_profile_prefix": "ytis-free3-worker",
  "notebooklm_profiles": [
    "ytis-free3-worker-01",
    "ytis-free3-worker-02",
    "ytis-free3-worker-03",
    "ytis-free3-worker-04"
  ],
  "browser_profile_root": "P:\\\\\\.data/yt-is/browser/notebooklm-free-3",
  "browser_profile_directory": "Default",
  "worker_state_root": "P:\\\\\\packages/yt-is/.logs/sharded_lane_series/new_account_free/worker_states",
  "notebook_prefix": "benchmark-shard-new-account-free"
}
```

### 3. Bootstrap worker `01`

Use the dedicated browser root and CDP port for the new lane. Do not reuse the default NotebookLM Chrome session.

```powershell
$root = 'P:\\\\\\.data\yt-is\browser\notebooklm-free-3'
$port = 18873
$chrome = 'C:\Program Files\Google\Chrome\Application\chrome.exe'

Start-Process -FilePath $chrome -ArgumentList @(
  "--user-data-dir=$root",
  "--profile-directory=Default",
  "--remote-debugging-port=$port",
  "--remote-allow-origins=*",
  "--no-first-run",
  "--no-default-browser-check",
  "https://notebooklm.google.com/"
)

Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$port/json/version"
nlm login --profile ytis-free3-worker-01 --provider openclaw --cdp-url "http://127.0.0.1:$port" --force
nlm login --check --profile ytis-free3-worker-01
```

Expected result:

- `Account:` matches the lane's intended Google account
- `C:\Users\brsth\.notebooklm-mcp-cli\profiles\ytis-free3-worker-01` now exists

### 4. Sync the family

After worker `01` is valid, copy it to the sibling profiles with:

```powershell
python P:\\\\\\packages/yt-is/bin/csf-nlm-worker-auth sync
```

Expected result:

- worker `01` validates against the intended account
- workers `02`-`04` inherit the same account
- the command writes a backup unless `--no-backup` is used

### 5. Verify all profiles

Run explicit checks for every profile in the new family:

```powershell
foreach ($profile in @(
  'ytis-free3-worker-01',
  'ytis-free3-worker-02',
  'ytis-free3-worker-03',
  'ytis-free3-worker-04'
)) {
  nlm login --check --profile $profile
}
```

Expected result:

- every profile returns `Authentication valid`
- every profile reports the same intended account
- no profile reports a different family account

### 6. Update tests

Add or update tests so the new lane is not just documented but enforced:

- `tests/test_nlm_worker_auth.py`
- `tests/test_sharded_lane_series.py`

Minimum coverage:

- `expected_email_for_profile()` maps the new worker profiles to the new account
- `csf-nlm-worker-auth sync` accounts-checks the new family
- lane config loading accepts the new lane
- lane config validation still rejects duplicate browser roots, notebook prefixes, or worker-state roots

### 7. Verify the change

Run the focused checks:

```powershell
$env:PYTHONPATH = 'P:\\\\\\packages\yt-is'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
pytest tests/test_nlm_worker_auth.py tests/test_sharded_lane_series.py -q
python -m py_compile csf/nlm_worker_auth.py csf/sharded_lane_series.py tests/test_nlm_worker_auth.py tests/test_sharded_lane_series.py
```

Expected result:

- tests pass
- `py_compile` exits `0`

## Pitfalls

- Do not use `nlm login switch` in the concurrent worker path.
- Do not point the new lane at `C:\Users\brsth\.notebooklm-mcp-cli\chrome-profile`.
- Do not reuse an existing Chrome root or browser profile directory.
- Do not add the lane only in JSON. Until auth families are externalized, `DEFAULT_FAMILIES` still has to match reality.
- If you intentionally add the lane JSON first, make sure `expected_email` is set and treat it as temporary until the code map is updated.
- Do not assume `nlm login --check` is enough; the sync path must also parse the account line.

## What To Update In The Running Docs

When the lane is real, update the live docs in the same change:

- `docs/operations/sharded-lane-series.md`
- `docs/operations/notebooklm-auth-robustness-test-plan.md`
- `docs/operations/test-registry.md` if the lane produces benchmark evidence

That keeps future agents from having to infer the extension recipe from code alone.
