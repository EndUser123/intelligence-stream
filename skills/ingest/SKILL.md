---
name: intelligence-stream-ingest
version: "1.0.0"
status: "stable"
category: intelligence
description: Ingest YouTube playlist videos using yt-dlp and store analysis in CKS.
---

# /csf-ingest — YouTube Playlist Ingest

Ingest YouTube playlist videos using yt-dlp and store analysis in CKS.

## Usage

```
/csf-ingest <playlist_url> [--cookies-from-browser=<browser>]
```

## Implementation

Invokes `bin/csf-ingest` script with yt-dlp to download and process playlist videos.
