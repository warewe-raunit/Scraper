# YouTube Subscriber-Cap Filter — Design

**Date:** 2026-06-18
**Status:** Approved (pending spec review)
**Component:** YouTube scraper service + route

## Problem

Users want to surface videos from *small* channels only. Given a maximum
subscriber count, the YouTube search endpoint should return only videos whose
channel has a subscriber count **at or below** that cap.

## Core constraint

YouTube's InnerTube **search** response does not include subscriber counts. The
count is only available from a separate per-channel/per-video call — today via
`extract_subscriber_count(next_data)` ([api/services/youtube.py](../../../api/services/youtube.py)),
which scans a payload for `subscriberCountText`. So capping by subscribers
requires resolving each result's channel subscriber count with extra InnerTube
calls, then filtering.

## Decisions (locked)

1. **Result-count semantics:** when the cap filters results out, keep walking
   search pages and resolving channels until `limit` passing videos are
   collected **or** a safety ceiling is hit. (Not "filter the first batch only".)
2. **Hidden/unverifiable channels:** **strict exclusion.** If a channel's
   subscriber count is hidden, unparseable, or its channel id can't be resolved,
   its videos are dropped. Every returned video is confirmed ≤ cap.
3. **Sub-count resolution strategy:** one `browse` call per **unique** channel,
   deduped + cached, page channels resolved concurrently. (Not one call per
   video.)

## Interface

Add an optional query param to `GET /api/v1/youtube/search`:

```
max_subscribers: Optional[int] = Query(None, ge=0,
    description="Only return videos from channels with at most this many "
                "subscribers. Channels with hidden/unknown counts are excluded.")
```

- `max_subscribers is None` → **behavior unchanged** (zero regression; no extra
  calls, no new pagination loop).
- `max_subscribers` set → activates the cap filter described below.

The route forwards it to `scraper.search(..., max_subscribers=max_subscribers)`.

## Service flow (`YouTubeScraperService.search` gains `max_subscribers`)

When `max_subscribers` is set:

1. Run the existing InnerTube search + continuation pagination, but the stop
   condition becomes "collected `limit` **passing** videos" rather than "`limit`
   raw videos".
2. For each newly fetched page of videos:
   a. Collect the set of **unique** `channel_id`s not already resolved.
   b. Resolve each unique channel's subscriber count concurrently
      (`asyncio.gather`) via a new helper `get_channel_subscriber_count`.
   c. Parse the subscriber text → int. Cache `channel_id -> count` for the
      request (and a short cross-request TTL cache).
3. Keep a video iff its channel count is known **and** `count <= max_subscribers`.
   Drop hidden/unresolvable/over-cap.
4. Stop when `len(passing) >= limit` **or** the safety ceiling trips; slice to
   `limit`.

### Subscriber-count helper

`async def get_channel_subscriber_count(channel_id) -> Optional[int]`:
- One `_execute_post("browse", {browseId: channel_id, context})` call.
- `extract_subscriber_count` over the response → text like `"12.3K subscribers"`.
- `parse_subscriber_text` → int.
- Returns `None` when hidden/unparseable (caller drops the video).

### Subscriber-text parsing (`parse_subscriber_text`)

Pure function, unit-tested:
- `"1.2M subscribers"` → 1_200_000
- `"12.3K subscribers"` → 12_300
- `"1,234 subscribers"` → 1_234
- `"1.1B subscribers"` → 1_100_000_000
- `"No subscribers"` / `""` / hidden → `None`

Note: K/M/B abbreviations are approximate (YouTube rounds); the cap comparison
is therefore approximate near abbreviation boundaries. Acceptable for "small
channel" intent.

### lockupViewModel channel id (required fix)

Newer search results arrive as `lockupViewModel` and currently parse with an
empty `channel_id` ([api/services/youtube.py](../../../api/services/youtube.py),
`parse_lockup_view_model`). Without a channel id we cannot resolve subs, which
would drop most modern results. Extend `parse_lockup_view_model` to extract the
channel id (and name when available) from the lockup's metadata/navigation
endpoint. Any video still missing a channel id is dropped under the strict rule.

### Safety ceiling (config)

New config in `api/config.py`:
- `YOUTUBE_SUBFILTER_MAX_PAGES` (default ~10) — max search continuation pages to
  walk while filtering.
- `YOUTUBE_SUBFILTER_MAX_CHANNEL_LOOKUPS` (default ~150) — hard cap on channel
  resolutions per request.

When a ceiling trips, return whatever passed so far (may be < `limit`). This
bounds latency/cost so a tiny cap can't run unbounded.

## Output

Same video shape as today, plus per returned video:
- `subscribers`: the resolved subscriber text (e.g. `"12.3K subscribers"`)
- `subscriber_count`: the parsed integer (e.g. `12300`)

So the applied cap is visible and auditable in the response. The top-level shape
(`query`, `sort`, `timeframe`, `results_count`, `videos`) is unchanged.

## Error handling

- A channel lookup that errors/times out → treated as unresolved → video dropped
  (strict rule), logged at warn, does not fail the whole request.
- Ceiling trips → return partial result, log at info with counts.
- `max_subscribers=None` path is completely untouched.

## Testing

- `parse_subscriber_text`: K/M/B, commas, plain, hidden/empty → None.
- Filter keeps ≤ cap, drops over-cap, drops hidden — with stubbed channel counts.
- Pagination-until-limit: stops at `limit` passing; stops at `MAX_PAGES` ceiling
  with a partial result.
- Channel-count caching: same channel resolved once across pages.
- `max_subscribers=None`: no extra calls, identical output to today (regression).
- `parse_lockup_view_model` extracts channel id from a lockup fixture.

## Out of scope

- Minimum-subscriber / range filtering (only a max cap now).
- Subscriber filtering on `/channel`, `/playlist`, `/video` endpoints.
- Exact (non-abbreviated) subscriber counts.
