## Task
Audit the YouTube service for missing/misleading count data (views, subs, likes)
across all endpoints and fix it so every response carries whole, normalized data.

## Why this task
User report: YouTube endpoints miss data or give misleading numbers. Confirmed by
reading youtube.py:
- search/channel/playlist videos return `views` text but no numeric view count.
- get_video_details `view_count` is mixed-format (raw "12345" from player vs
  "12,345 views" from next); `like_count`/`subscribers` are text-only, no int.
- lockup parser leaves `channel_name` blank and uses a fragile `"new"` substring
  heuristic that mislabels metadata.
- get_channel_videos never resolves the channel's subscriber count.

## Steps
1. Add `parse_count_text(text) -> Optional[int]` (general K/M/B + comma parser);
   make `parse_subscriber_text` delegate to it (keeps existing tests/behavior).
2. parse_video_renderer + parse_lockup_view_model: add numeric `view_count`.
3. parse_lockup_view_model: fill `channel_name` (best-effort from metadata rows),
   replace the `"new"` heuristic with a word-boundary/premiere check.
4. get_video_details: `view_count`/`like_count` -> int, keep raw as
   `view_count_text`/`like_count_text`; add channel `subscriber_count` int.
5. get_channel_videos: extract channel subscriber text+count from the header,
   include in the response and the saved channel row.

## Definition of Done
Every video object exposes a numeric `view_count`; get_video_details exposes
numeric `view_count`/`like_count` plus `*_text` and channel `subscriber_count`;
lockup videos carry a `channel_name` when present; channel endpoint returns the
channel's `subscriber_count`. New unit tests cover the parser and each shape.
Full suite green. Existing keys preserved (no consumer break).

## Risk
view_count/like_count change from string to int in get_video_details — the
requested correctness fix; raw text preserved under `*_text`, and DB `_parse_int`
already accepts ints. Lockup channel_name is best-effort; never worse than the
current "" (only set when confidently found).
