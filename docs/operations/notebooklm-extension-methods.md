# NotebookLM Extension Methods for `yt-is`

Date: 2026-04-20

This note compares the NotebookLM-related browser extensions I found in the local Chromium profiles and calls out the implementation patterns that are most useful for `yt-is`, especially for source transcript download / backup from NotebookLM.

## What I checked

Installed locally in the current browser profiles:

- `hjoonjdnhagnpfgifhjolheimamcafok`
- `idkdddfdfhlgjleoieidjhmgmopacdli`
- `dgenbagabmmjpfjlbcnnlmpopipdapjo`
- `dcmlelgcjhcgaphgheboioaoldggbmej`
- `aipkikdaocjcmcnnkipoeffphafommnn`
- `igboaajnodmioloalklomakeeigipnkh`
- `hiibkpjljigehlnnecbgehkhfibmahjn`
- `ijdefdijdmghafocfmmdojfghnpelnfn`
- `dhbmfgpkkmgbchheknfflhlpfdngkoac`
- `efcbfjjkfckbphmcjpacbgpjknkbebgg`
- `bgacnkhjnoehdckkjpchnlcmkabnglgg`
- `kobncfkmjelbefaoohoblamnbackjggk`

Not found locally in the browser profiles I searched:

- `fhplgheiijiledgfpabdiiheblmjoaog`
- `gejoojiehhghphimkfaccjmnmadahblc`
- `badkjigcpokmligdghppmippgfjhabmp`
- `ildjnemeokopkjjkkhooonanjckcclfj`
- `fjbhcpgodilncafncjpggobiaknjmcbn`
- `afchokljnhhggkhedfbmkcmdagjmjchj`

## What each class of extension is doing

### 1. Source ingest helpers

These are good at getting content into NotebookLM, but they are not the best reference for source transcript export.

- `idkdddfdfhlgjleoieidjhmgmopacdli` - `NotebookLM - Youtube Link (Source) Automator`
  - Popup-driven YouTube-to-NotebookLM ingestion.
  - Uses NotebookLM add-dialog selectors such as `button.add-source-button`, `textarea[formcontrolname="urls"]`, and `button[mat-flat-button][color="primary"]`.
  - Best use: add YouTube sources reliably, not export transcripts.

- `dhbmfgpkkmgbchheknfflhlpfdngkoac` - `NotebookLM: Youtube Easy Copy & Paste`
  - Side-panel-driven ingest helper.
  - Uses NotebookLM add-flow selectors such as `[aria-label="Add source"]`, `input[formcontrolname="newUrl"]`, and `textarea[formcontrolname="urls"]`.
  - Best use: fast add-source UX for YouTube / URLs.

- `bgacnkhjnoehdckkjpchnlcmkabnglgg` - `YouTube NotebookLM`
  - Generic content-script helper for YouTube-to-NotebookLM workflows.
  - Strongly focused on link picking and YouTube normalization.
  - Best use: YouTube ingestion and browser-link extraction.

- `kobncfkmjelbefaoohoblamnbackjggk` - `YouTube to NotebookLM`
  - Similar ingest helper, with popup + all-frame content script support.
  - Good at channel/playlist/video ingestion.
  - Best use: YouTube batch import, not transcript export.

- `ijdefdijdmghafocfmmdojfghnpelnfn` - `NotebookLM Web Importer`
  - Importer for web pages and YouTube videos.
  - Popup-driven, with NotebookLM login and import flows.
  - Best use: web import, not source backup.

- `hjoonjdnhagnpfgifhjolheimamcafok` - `NotebookLM - WebSync Full Site Importer`
  - Importer that captures page content and sends it to NotebookLM.
  - Includes offscreen scraping/sign-in support.
  - Best use: full-site capture and text ingestion.

### 2. Export / backup helpers

These are the most useful references for `yt-is` if the goal is to download source transcripts, archive sources, or back up NotebookLM content.

- `aipkikdaocjcmcnnkipoeffphafommnn` - `NotebookLM Importer - Source Exporter`
  - This is the strongest direct match for source download / backup.
  - It has a dedicated `download.html` view with:
    - source filtering
    - Markdown export
    - download actions
  - It also has a NotebookLM content script that can fetch page HTML and scrape the current page.
  - Best use: source export, transcript download, and archive generation.

- `efcbfjjkfckbphmcjpacbgpjknkbebgg` - `Paywall Porter for NotebookLM`
  - More of a source-management and content-conversion suite than a pure exporter.
  - Strong features:
    - notebook management UI
    - PDF / HTML / link conversion
    - local save + NotebookLM upload flows
    - source filtering and duplicate review
  - Best use: packaging external content into NotebookLM-friendly formats, and managing notebooks after import.

- `hiibkpjljigehlnnecbgehkhfibmahjn` - `NotebookLM Tools`
  - Broad NotebookLM power-user suite.
  - The local code strongly indicates:
    - bulk import
    - folder management
    - prompt snippets
    - duplicate cleanup
    - source merging
    - ZIP import/export
    - tags / dark mode / side panel
  - Best use: notebook maintenance, source merging, backup/re-import workflows.

### 3. Notebook organization and cross-app helpers

These are useful adjacent patterns, but not the main source transcript export path.

- `dcmlelgcjhcgaphgheboioaoldggbmej` - `NotebookLM Organizer`
  - Focuses on homepage notebook organization, project buttons, and grid cleanup.
  - Best use: notebook-level organization, not source export.

- `igboaajnodmioloalklomakeeigipnkh` - `NotebookLM to Gemini`
  - A handoff bridge from NotebookLM to Gemini.
  - Best use: content transfer between apps, not transcript download.

- `dgenbagabmmjpfjlbcnnlmpopipdapjo` - `NotebookLM AI Sidebar`
  - Side panel + reusable NotebookLM state.
  - Looks like a broader sidebar experience with source tracking, transcript parsing, and action tracking.
  - Best use: assistant sidebar / NotebookLM UI orchestration, not a direct exporter.

## What seems better for `yt-is`

For source transcript download from NotebookLM, the better implementation pattern is:

1. A dedicated export surface, not a popup-only action.
2. A side panel or full page that can:
   - list sources
   - filter sources
   - select subsets
   - export to Markdown / text / ZIP
3. A background worker that handles file generation and browser downloads.
4. NotebookLM DOM scraping only where needed, with stable selectors and retryable waits.
5. Explicit notebook/source state tracking so export operations can resume without re-reading everything.

The best concrete examples in the inspected extensions are:

- `aipkikdaocjcmcnnkipoeffphafommnn` for source export UI and Markdown download
- `hiibkpjljigehlnnecbgehkhfibmahjn` for notebook management, dedupe, merge, and ZIP workflows
- `efcbfjjkfckbphmcjpacbgpjknkbebgg` for content conversion plus notebook management

## What seems worse

For `yt-is`, these are less attractive implementation patterns:

- Popup-only importers for heavy notebook workflows.
- One-shot scripts that assume the NotebookLM page is always ready immediately.
- Direct dynamic extension injection at runtime.
- Anything that depends on a single brittle selector with no fallback or state check.

## Recommendation for `yt-is`

If the goal is to download or archive source transcripts from NotebookLM:

- Prefer the `aipk...` style: explicit export view, source filter, Markdown/text output, and a download path.
- Borrow the `hiib...` style for notebook-level organization and source merging.
- Borrow the `efcb...` style for content conversion and notebook management if we need PDF/HTML or broader archival flows.
- Keep the `yt-is` code self-contained; do not depend on dynamically injected extensions as the primary path.

If we want to turn this into implementation work, the next step is to add a small NotebookLM export abstraction in `yt-is` that can:

- enumerate sources,
- choose export formats,
- and write Markdown / ZIP artifacts without coupling the export logic to one browser extension.
