# Indicative Channel Filtering Rubric

This document captures the current, non-final channel filtering approach for `yt-is`.
It is intentionally descriptive, not a locked policy.

## Goal

Separate channels into three operational buckets:

- `keep` when the channel is useful to learn from
- `block` when the channel is mostly consumption, storytelling, lore, or entertainment and does not add enough learnable value
- `review` when the signal is mixed or not yet strong enough to decide

## Canonical Identity

Channel filtering and channel-state operations should use canonical `channel_id` storage identity.

- `channel_metadata` stores the tracked channel state
- `channel_blocklist` stores durable exclusions
- `channel_url` is display/alias data
- malformed handle forms such as `youtube.com@handle` should not persist

## Strong Signals We Have Observed

### Block candidates

- Channels that are primarily storytelling, lore, fiction, or entertainment when they do not add learnable value
- Channels that are broad institutional / broadcaster / news feeds when they are not useful for the backlog
- Channels that read like a content dump, archive, or brand vault with no visible learnable angle
- Channels that were manually reviewed and decided to be not useful

### Keep candidates

- Channels that are informative, technical, analytical, educational, or reference-oriented
- Channels that are narrative-heavy but still teach something useful
- Channels that the user explicitly wants to keep

### Review-only signals

- Very low video count
- Stale publishing activity
- Channels with mixed learnable and storytelling content
- Channels where count or recency suggests suspicion but usefulness is not obvious
- Podcasts, shows, interviews, and talks that do not show clear learnable cues and read as consumption-first
- Music / dance / performance channels that might be instructional, but do not make that clear yet

If a channel has no clear learnable value and the visible pattern is mostly consumption, prefer `block` over `review`.

## Signals That Are Weak or Non-Blocking

- Subscriber count by itself
- Slow fetches or temporary technical failures
- Trading / finance / crypto / similar topic tags by themselves

These are useful context, but they are not sufficient on their own to block a channel.

## Practical Review Heuristics

Use these only as review cues, not automatic hard rules:

- `video_count_estimate <= 15`
- `video_count_estimate < 5`
- last publish older than 3 months
- last publish older than 1 year

The time-based rules are useful for suspicion, but they should stay subordinate to the real question:
is the channel useful to learn from?

## Current Operational Reading

From the manual review done so far, the strongest distinctions are:

- The real separator is whether the channel is useful to learn from.
- Pure storytelling / lore / entertainment channels are strong block candidates when they do not add learnable value.
- Channels that still expose the user to useful information should generally be kept, even if they are narrative-heavy or creator-led.
- `block` when the channel is mostly a story / lore / entertainment stream and not instructional
- `keep` when the channel tends to expose the user to useful information, even if it is narrative or creator-led
- `review` when the channel sits between those two, or when the count / recency signal is suspicious but not decisive

## Policy Snapshot

This is the concise operational rule set currently in use:

### Keep

- The channel is meaningfully useful to learn from.
- It is instructional, analytical, technical, educational, or reference-oriented.
- It may be narrative or creator-led, but it still exposes the user to useful information.

### Review

- The channel is ambiguous.
- It mixes learnable value with storytelling or entertainment.
- It is very low volume or stale enough to be suspicious, but not enough to block by itself.
- It may be a podcast, show, interview, talk, music, dance, or performance channel, but there is still a plausible instructional or analytical angle.
- Podcasts, shows, interviews, and talks should lean to `block` when they do not show clear learnable cues and read as consumption-first.

### Block

- The channel is mostly consumption rather than learning.
- It is primarily storytelling, lore, or fiction without useful information.
- It is broad institutional/news/broadcast noise that does not serve the corpus.
- It was already manually reviewed and decided to be not useful.
- It is a music, dance, or performance channel whose visible pattern is consumption-first and does not appear to teach anything.
- It is a one-word or brand-style channel title with no visible learnable angle and no compensating instructional cues.

## Music / Dance / Performance Channels

Do not block these based on topic words alone.

Treat them as follows:

### Keep

- The channel is instructional or explanatory.
- It teaches technique, composition, production, choreography, analysis, or process.
- It is performance-adjacent, but still provides learnable value.

### Review

- The channel mixes instruction and performance.
- The metadata suggests music/dance/performance, and there is a plausible instructional angle.
- The content style is ambiguous enough that a human should inspect examples, but not so ambiguous that it should override a clear consumption-first pattern.

### Block

- The channel is mostly performance or consumption.
- It is primarily songs, covers, recitals, dance clips, live sets, or similar output with no clear teaching value.
- It does not appear to be useful for learning, even if it is high quality or popular.
- If the channel title, description, or visible video pattern reads as performance-first, prefer `block` rather than leaving it in `review`.
- Brand-archive patterns like `out of context`, `vault`, `clips`, or `highlights` should lean `block` when they do not carry a clear learnable signal.

### Practical signals for music/dance/performance

Use the same conservative rule as the rest of the rubric:

- the title and description can be enough to flag a review
- video titles can help distinguish tutorials from performances
- topic tags are optional and may be absent
- low video count or stale activity should only increase suspicion, not force a block on their own
- if the evidence points to consumptive or performance-only behavior, that is enough to block even without tags

## Notes

- This rubric should be treated as indicative and revisable.
- It is meant to guide review of the current tracked corpus, not to permanently encode every possible block reason.
- The durable storage contract is already `channel_id`; the rubric is about human filtering policy on top of that identity model.
