# Scraper API — Reference

A multi-platform scraping API for **Reddit, YouTube, X (Twitter), and LinkedIn**.
It wraps each platform's official/public endpoints behind one authenticated REST
surface, returning clean structured JSON (or CSV/Excel/HTML where noted).

- Interactive docs (Swagger): **`{BASE_URL}/docs`**
- Alternate docs (ReDoc): **`{BASE_URL}/redoc`**
- OpenAPI schema: **`{BASE_URL}/openapi.json`**

---

## Base URL

| Environment | URL |
|-------------|-----|
| Production (systemd / `run_background.sh`) | `http://<host>:18080` |
| Local dev (`python api/main.py`) | `http://127.0.0.1:8000` |

All data endpoints are versioned under **`/api/v1`**. Substitute your host for
`{BASE_URL}` in the examples below.

---

## Authentication

Every `/api/v1/*` endpoint requires an API key. Provide it **either** way:

- Header (preferred): `X-API-Key: <your-key>`
- Query parameter: `?api_key=<your-key>`

```bash
curl -H "X-API-Key: $API_KEY" "{BASE_URL}/api/v1/subreddit/python/posts?limit=5"
```

`GET /health` is the only unauthenticated endpoint.

> Missing/invalid key → `401 Unauthorized`. If the server has no key configured
> at all → `503` (it fails closed; it never ships a default key).

---

## Conventions

**Output format** — most endpoints accept `format`:
- `json` (default) — structured JSON.
- `csv` — file download. (Reddit, X, LinkedIn, YouTube.)
- `excel`, `html` — YouTube only.
- `raw` — YouTube `/video` only (raw InnerTube payload).

**Pagination** — Reddit listing endpoints return an `after` token; pass it back
as `?after=<token>` for the next page.

**Account selection** — Reddit and LinkedIn endpoints accept an optional
`account_id` to pin a specific configured account. Omit it to rotate
automatically across the pool (recommended).

**`limit`** — each endpoint documents its own min/max; values are clamped.

---

## Error responses

Errors return `{"detail": "<message>"}` with an appropriate status:

| Status | Meaning |
|--------|---------|
| `400 Bad Request` | Invalid input (bad URL, missing required param). |
| `401 Unauthorized` | Missing/invalid API key. |
| `502 Bad Gateway` | Upstream scrape failed (X/LinkedIn public sources unreachable or blocked). |
| `503 Service Unavailable` | All accounts temporarily rate-limited (Reddit). Transient — honor the `Retry-After` header and retry. |
| `500 Internal Server Error` | Unexpected server error. |

---

# Endpoints

## System

### `GET /health`
Liveness + how many account sessions are loaded. **No auth.**

```bash
curl "{BASE_URL}/health"
```
```json
{ "status": "healthy", "active_session_count": 9, "available_accounts": ["acc_01", "acc_02"] }
```

---

## Reddit

Authenticated Reddit data via logged-in account sessions with automatic failover
and rate-limit handling.

### `GET /api/v1/subreddit/{subreddit}/posts`
List posts from a subreddit.

| Param | In | Type | Default | Notes |
|-------|----|------|---------|-------|
| `subreddit` | path | string | — | e.g. `python` (with or without `r/`). |
| `sort` | query | `hot`\|`new`\|`top`\|`rising` | `hot` | |
| `time` | query | `hour`\|`day`\|`week`\|`month`\|`year`\|`all` | `all` | Only used when `sort=top`. |
| `limit` | query | int 1–100 | 25 | |
| `after` | query | string | — | Pagination token. |
| `account_id` | query | string | — | Pin an account; omit to rotate. |
| `format` | query | `json`\|`csv` | `json` | |

```bash
curl -H "X-API-Key: $API_KEY" \
  "{BASE_URL}/api/v1/subreddit/python/posts?sort=top&time=week&limit=10"
```
```json
{
  "subreddit": "python", "sort": "top", "timeframe": "week", "limit": 10,
  "after": "t3_1abc23", "before": null, "results_count": 10,
  "posts": [{
    "id": "1abc23", "fullname": "t3_1abc23", "title": "...", "text": "...",
    "username": "someuser", "subreddit": "python", "num_comments": 42,
    "upvotes": 1234, "upvote_ratio": 0.98, "created_utc": 1700000000,
    "published_at": "2026-06-20T12:00:00Z", "published_ago": "2 days ago",
    "url": "https://www.reddit.com/r/python/comments/1abc23/...",
    "is_video": false, "video_url": null, "images": [], "nsfw": false, "score": 1234
  }]
}
```

### `GET /api/v1/posts/{post_id}`
Single post's details by base-36 ID (`1abc23` or `t3_1abc23`).

| Param | In | Type | Default |
|-------|----|------|---------|
| `post_id` | path | string | — |
| `account_id` | query | string | — |
| `format` | query | `json`\|`csv` | `json` |

### `GET /api/v1/post/comments`
A post's full comment tree.

| Param | In | Type | Default | Notes |
|-------|----|------|---------|-------|
| `url` | query | string | — | **Required.** Full post URL or ID. |
| `sort` | query | `confidence`\|`top`\|`new`\|`controversial`\|`old`\|`random`\|`qa` | `confidence` | |
| `depth` | query | int 1–10 | — | Max reply-tree depth. |
| `limit` | query | int 1–500 | 100 | |
| `account_id` | query | string | — | |
| `format` | query | `json`\|`csv` | `json` | |

```bash
curl -H "X-API-Key: $API_KEY" \
  "{BASE_URL}/api/v1/post/comments?url=https://www.reddit.com/r/python/comments/1abc23/x/&limit=50"
```

### `GET /api/v1/posts/search`
Search posts globally or within a subreddit.

| Param | In | Type | Default | Notes |
|-------|----|------|---------|-------|
| `q` | query | string | — | **Required.** Search query. |
| `subreddit` | query | string | — | Restrict to one subreddit. |
| `sort` | query | `relevance`\|`hot`\|`top`\|`new`\|`comments` | `relevance` | |
| `time` | query | `hour`…`all` | `all` | Used for `top`/`relevance`. |
| `limit` | query | int 1–100 | 25 | |
| `after` | query | string | — | |
| `account_id` | query | string | — | |
| `format` | query | `json`\|`csv` | `json` | |

### `GET /api/v1/posts/by-url`
Auto-detects a Reddit URL type (subreddit / post / user) and scrapes it.

| Param | In | Type | Notes |
|-------|----|------|-------|
| `url` | query | string | **Required.** Any Reddit URL. |
| `account_id` | query | string | |
| `format` | query | `json`\|`csv` | |

### `GET /api/v1/user/{username}/about`
User profile (karma, creation date, description).

### `GET /api/v1/user/{username}/posts`
A user's submitted posts. Params: `sort` (`new`\|`hot`\|`top`, default `new`), `time`, `limit` (1–100, default 25), `after`, `account_id`, `format`.

### `GET /api/v1/user/{username}/comments`
A user's comments. Same params as `/user/{username}/posts`.

### `GET /api/v1/reddit/accounts/status`
Account-pool health: per-account status (`healthy`/`cool_down`/`needs_relogin`/`no_session`), cooldown remaining, rate-limit budget, session age. *(Ops endpoint.)*

### `POST /api/v1/reddit/accounts/{account_id}/relogin`
Queue a background relogin for one account. Returns `{"queued": true, "account_id": "acc_01"}`.

---

## YouTube

Stealth scraping via YouTube's InnerTube API. **No accounts required** — scales
freely. Supports `json`/`csv`/`excel`/`html` exports.

### `GET /api/v1/youtube/search`
Search videos.

| Param | In | Type | Default | Notes |
|-------|----|------|---------|-------|
| `q` | query | string | — | **Required.** |
| `sort` | query | `relevance`\|`date`\|`views`\|`rating` | `relevance` | |
| `time` | query | `hour`…`all` | `all` | |
| `limit` | query | int 1–500 | 20 | |
| `location` | query | country name | — | Localizes results via proxy geo + `gl/hl`. |
| `max_subscribers` | query | int ≥0 | — | Only channels at/under N subs (slower; resolves each channel). |
| `format` | query | `json`\|`csv`\|`excel`\|`html` | `json` | |

### `GET /api/v1/youtube/video`
Full video metadata + top comments. Provide **`url` OR `video_id`**.

| Param | In | Type | Default | Notes |
|-------|----|------|---------|-------|
| `url` | query | string | — | Full watch URL. |
| `video_id` | query | string | — | 11-char ID. |
| `limit` | query | int 0–500 | 20 | Comments to return. |
| `include_raw` | query | bool | `false` | Include raw InnerTube payload. |
| `format` | query | `json`\|`csv`\|`excel`\|`html`\|`raw` | `json` | |

```bash
curl -H "X-API-Key: $API_KEY" "{BASE_URL}/api/v1/youtube/video?video_id=dQw4w9WgXcQ"
```

### `GET /api/v1/youtube/channel/{channel_id}/videos`
A channel's uploads or live streams. `type` = `videos` (default) \| `live`. `format` = `json`\|`csv`\|`excel`\|`html`.

### `GET /api/v1/youtube/playlist/{playlist_id}`
Videos in a playlist. `format` = `json`\|`csv`\|`excel`\|`html`.

### `GET /api/v1/youtube/download`
Resolve a downloadable video stream. Provide **`url` OR `video_id`**.

| Param | In | Type | Default | Notes |
|-------|----|------|---------|-------|
| `url` / `video_id` | query | string | — | One required. |
| `resolution` | query | `144p`…`2160p` | `360p` | |
| `format` | query | `stream`\|`json`\|`html`\|`redirect`\|`file` | `stream` | `stream`=proxy bytes w/o saving, `json`=direct-link metadata, `redirect`=302 to media, `file`=backend downloads temp MP4 first. |

---

## X (Twitter)

**Unauthenticated** scraping via public Nitter/X-proxy instances + a stealth
browser fallback. No login. Browser-backed, so heavier than the HTTP scrapers.

> `location` only selects the **proxy exit country** — X keyword search is global
> and is *not* localized by IP.

### `GET /api/v1/x/profile/{username}`
Public profile + recent tweets.

| Param | In | Type | Default | Notes |
|-------|----|------|---------|-------|
| `username` | path | string | — | Handle without `@`. |
| `limit` | query | int 1–100 | 20 | Tweets. |
| `format` | query | `json`\|`csv` | `json` | |
| `proxy` | query | string | — | Custom proxy URL for this request. |
| `headless` | query | bool | `true` | |

### `GET /api/v1/x/thread`
A tweet's reply thread.

| Param | In | Type | Default | Notes |
|-------|----|------|---------|-------|
| `url` | query | string | — | **Required.** Full status URL. |
| `limit` | query | int 1–200 | 20 | Replies. |
| `location` | query | country name | — | Proxy exit country only. |
| `format` | query | `json`\|`csv` | `json` | |
| `proxy`, `headless` | query | | | As above. |

### `GET /api/v1/x/search`
Search tweets with advanced filters (compiled into Twitter advanced operators).

| Param | In | Type | Default | Notes |
|-------|----|------|---------|-------|
| `q` | query | string | — | **Required.** |
| `limit` | query | int 1–100 | 20 | |
| `location` | query | country name | — | Proxy exit only. |
| `since` / `until` | query | `YYYY-MM-DD` | — | Date range. |
| `from_user` / `to_user` | query | string | — | Handles. |
| `min_likes` / `min_retweets` / `min_replies` | query | int ≥0 | — | Engagement floors. |
| `filter_links` | query | bool | — | Only tweets with links. |
| `exclude_replies` / `exclude_retweets` | query | bool | — | |
| `latest` | query | bool | `false` | Chronological. |
| `popular` | query | bool | `false` | Preset `min_faves:100`. |
| `trending` | query | bool | `false` | Preset `min_faves:500`. |
| `format` | query | `json`\|`csv` | `json` | |
| `proxy`, `headless` | query | | | As above. |

```bash
curl -H "X-API-Key: $API_KEY" \
  "{BASE_URL}/api/v1/x/search?q=ai&min_likes=100&exclude_replies=true&limit=20"
```

---

## LinkedIn

Authenticated scraping via logged-in account sessions (Voyager API). Account pool
with background relogin. `format` = `json` \| `csv`.

### `GET /api/v1/linkedin/profile/{profile_id}`
Deep profile (experience, education, skills, about).

| Param | In | Type | Notes |
|-------|----|------|-------|
| `profile_id` | path | string | Raw public ID, or URL-encoded full profile URL. |
| `profile_url` | query | string | Full profile URL (overrides `profile_id`). |
| `account_id` | query | string | Pin an account; omit to rotate. |
| `format` | query | `json`\|`csv` | |

### `GET /api/v1/linkedin/company/{company_name}`
Company/organization page. `company_url` query overrides the path. `account_id`, `format` as above.

### `GET /api/v1/linkedin/jobs`
Search job postings.

| Param | In | Type | Default | Notes |
|-------|----|------|---------|-------|
| `keywords` | query | string | — | **Required.** |
| `location` | query | string | — | e.g. `New York`. |
| `workplace_type` | query | multi: `on_site`\|`remote`\|`hybrid` | — | Repeatable. |
| `job_type` | query | multi: `full_time`\|`part_time`\|`contract`\|`temporary`\|`internship`\|`volunteer` | — | Repeatable. |
| `experience_level` | query | multi: `internship`\|`entry_level`\|`associate`\|`mid_senior`\|`director`\|`executive` | — | Repeatable. |
| `date_posted` | query | `past_24h`\|`past_week`\|`past_month` | — | |
| `limit` | query | int 1–1000 | 25 | Paginated. |
| `account_id`, `format` | query | | | |

### `GET /api/v1/linkedin/post/comments`
Comments on a post.

| Param | In | Type | Default | Notes |
|-------|----|------|---------|-------|
| `url` | query | string | — | **Required.** Post URL or `urn:li:activity:...`. |
| `sort` | query | `relevant`\|`recent` | `relevant` | |
| `limit` | query | int 1–1000 | 25 | |
| `account_id`, `format` | query | | | |

### `GET /api/v1/linkedin/search`
Universal blended search.

| Param | In | Type | Default | Notes |
|-------|----|------|---------|-------|
| `q` | query | string | — | **Required.** |
| `category` | query | `all`\|`people`\|`companies`\|`jobs`\|`posts`\|`groups` | `all` | |
| `limit` | query | int 1–1000 | 25 | Not used for `category=all`. |
| `account_id`, `format` | query | | | |

### `GET /api/v1/linkedin/accounts/status`
LinkedIn account-pool health snapshot. *(Ops endpoint.)*

### `POST /api/v1/linkedin/accounts/{account_id}/relogin`
Queue a background relogin for one account.

---

## Quick start for a new integrator

```bash
export API_KEY="<key from the team>"
export BASE_URL="http://<host>:18080"

# 1. Is it up?
curl "$BASE_URL/health"

# 2. Reddit — top posts this week
curl -H "X-API-Key: $API_KEY" \
  "$BASE_URL/api/v1/subreddit/programming/posts?sort=top&time=week&limit=5"

# 3. YouTube — search
curl -H "X-API-Key: $API_KEY" \
  "$BASE_URL/api/v1/youtube/search?q=fastapi&limit=5"

# 4. X — a profile
curl -H "X-API-Key: $API_KEY" \
  "$BASE_URL/api/v1/x/profile/nasa?limit=5"

# 5. CSV instead of JSON
curl -H "X-API-Key: $API_KEY" \
  "$BASE_URL/api/v1/posts/search?q=python&format=csv" -o results.csv
```

For the full live, try-it-in-browser reference, open **`{BASE_URL}/docs`**.

---

## MCP server (Claude Code, Codex, other agents)

`mcp_server.py` exposes every endpoint above as MCP tools over stdio. It is a thin
client to a **running** API instance — it mirrors `{BASE_URL}/openapi.json`, so new
endpoints become tools automatically. Start the API first, then point an agent at it.

```bash
pip install -r requirements.txt          # pulls fastmcp
export SCRAPER_BASE_URL="http://127.0.0.1:8000"   # or your prod host:18080
export API_KEY="<your-key>"
```

**Claude Code** — one command:
```bash
claude mcp add scraper \
  -e SCRAPER_BASE_URL=http://127.0.0.1:8000 -e API_KEY=$API_KEY \
  -- python /abs/path/to/reddit_stealth_scraper/mcp_server.py
```

**Codex** — `~/.codex/config.toml`:
```toml
[mcp_servers.scraper]
command = "python"
args = ["/abs/path/to/reddit_stealth_scraper/mcp_server.py"]
env = { SCRAPER_BASE_URL = "http://127.0.0.1:8000", API_KEY = "your-key" }
```

**Generic MCP host** — `mcp.json` / `.mcp.json`:
```json
{
  "mcpServers": {
    "scraper": {
      "command": "python",
      "args": ["/abs/path/to/reddit_stealth_scraper/mcp_server.py"],
      "env": { "SCRAPER_BASE_URL": "http://127.0.0.1:8000", "API_KEY": "your-key" }
    }
  }
}
```

The agent now sees tools like `search_youtube`, `get_subreddit_posts`,
`get_linkedin_profile`, `search_x_tweets`, etc. — same params as the REST endpoints.

---

## Install as a plugin (Claude Code, Codex, any agent)

This repo is also a **Claude Code plugin** (`.claude-plugin/`) that bundles the
MCP server above plus a `social-research` skill — research a topic/person across
all four sources and get a cited briefing ranked by engagement.

**Prereqs (every method):** the API must be running, and the agent's environment
needs `SCRAPER_BASE_URL` + `API_KEY` set, plus `pip install -r requirements.txt`
(for the `fastmcp` MCP) and `python` on PATH.

**Claude Code** — install the plugin (registers the MCP server + skill):
```
/plugin marketplace add warewe-raunit/Scraper
/plugin install stealth-scraper
```
Then `/social-research <topic>`, or just ask "what are people saying about X".

**Codex / Cursor / Copilot / Gemini / Windsurf** — the skill is portable via the
Agent Skills CLI:
```bash
npx skills add warewe-raunit/Scraper -g
```
Add the MCP server from the "MCP server" section above for the underlying tools.

**Manual (dev):** symlink the skill into your agent's skills dir, e.g.
```bash
ln -s "$(pwd)/skills/social-research" ~/.claude/skills/social-research
```
