---
name: social-research
description: Research what people actually say about any topic, person, or product right now across Reddit, YouTube, X (Twitter), and LinkedIn. Pulls real posts, comments, and engagement and synthesizes a cited briefing ranked by upvotes/likes/views — not SEO. Use when the user asks "what are people saying about X", "research <person/company>", "find discussion on <topic>", or invokes /social-research.
argument-hint: 'social-research nvidia earnings reaction | social-research "Peter Steinberger" | social-research best local LLM tools'
allowed-tools: Bash, Read, Write, WebSearch
homepage: https://github.com/warewe-raunit/Scraper
repository: https://github.com/warewe-raunit/Scraper
author: warewe-raunit
license: MIT
user-invocable: true
---

# social-research

Research a topic, person, or product across **Reddit, YouTube, X, and LinkedIn**
using the Stealth Scraper API, then synthesize a grounded, cited briefing ranked
by real engagement.

## Scraper access

Two ways to reach the scraper — use whichever is available, in this order:

1. **MCP tools** (preferred, present when the `stealth-scraper` plugin/MCP is
   installed). Tool names map 1:1 to endpoints:
   - `get_subreddit_posts`, `search_posts`, `get_post_comments`, `get_user_posts`
   - `search_youtube`, `get_video_details`
   - `search_x_tweets`, `get_x_profile`, `get_x_thread`
   - `search_linkedin_blended`, `get_linkedin_profile`, `get_linkedin_company`
2. **REST fallback** — if no MCP tools, curl the API directly with `Bash`:
   ```bash
   curl -s -H "X-API-Key: $API_KEY" "$SCRAPER_BASE_URL/api/v1/<endpoint>?<params>"
   ```
   `$SCRAPER_BASE_URL` defaults to `http://127.0.0.1:8000`. Full endpoint
   reference: [API.md](../../API.md).

If neither is reachable (no MCP, curl to `/health` fails), STOP and tell the user
the scraper API isn't running — don't fabricate results.

## Workflow

### 1. Parse the request
Extract the **topic** and the **intent**:
- `PERSON` — a named individual (research their recent activity/reputation)
- `PRODUCT/TOOL` — opinions, comparisons, complaints
- `TOPIC/NEWS` — what's being discussed and the prevailing take
A `vs` in the query (`A vs B`) means COMPARISON — run the per-entity research
below once per entity, then contrast.

### 2. Resolve entities (pre-flight)
Before searching, figure out *where* to look. Use `WebSearch` only to resolve
handles/communities, not to answer the question:
- X handle(s) and LinkedIn profile/company slug for a PERSON or PRODUCT
- The 2–4 most relevant subreddits
- A focused YouTube query (add "review"/"explained"/year as needed)

Skip resolution you're already confident about.

### 3. Search in parallel
Issue the searches together (one assistant turn, multiple tool calls). Default
breadth — tune to intent:
- **Reddit**: `search_posts` (topic) + `get_subreddit_posts` on each resolved
  subreddit (`sort=top`, `time=month`). Pull `get_post_comments` on the 1–2
  highest-signal posts — the comments are the actual opinion.
- **YouTube**: `search_youtube` (`sort=views` or `date`, `limit=10`); for a
  standout video pull `get_video_details` for the top comments.
- **X**: `search_x_tweets` (`min_likes` floor to cut noise, e.g. 50–100). For a
  PERSON also `get_x_profile`.
- **LinkedIn**: `search_linkedin_blended`; for a PERSON `get_linkedin_profile`,
  for a company `get_linkedin_company`. (LinkedIn is the professional/official
  layer — often empty for memes, essential for people/companies.)

### 4. Rank and merge
Score items by **real engagement**: Reddit upvotes/comment counts, YouTube
views, X likes/retweets. Drop low-engagement noise. Merge the same story when it
appears on multiple platforms — note it surfaced in several places (that's
signal), don't list it twice.

### 5. Synthesize
Write a briefing, not a data dump. Lead with **what you learned**, in prose,
weaving in what specific high-signal comments/posts actually said. Then per-source
highlights. Every claim links to its source inline as a markdown link. End with a
one-line "confidence" note: how much real engagement backs this vs. thin/early.

## Output contract

- Open with a one-line summary of the finding.
- **State the fact, THEN cite. The link supports a complete sentence — it never
  replaces the information.** The reader must understand the point without
  clicking. Pull the actual substance (who/what/the claim/the number) out of the
  post or comment and write it in prose, then append the source link.
  - WRONG: `Trump publicly stated [link] (19k upvotes).`
  - RIGHT: `Trump publicly stated Iran has a right to enrich uranium for civilian
    use [link] (19k upvotes).`
  - WRONG: `Iran is [link], contradicting VP Vance.`
  - RIGHT: `Iran is still operating its centrifuges and denies agreeing to halt
    enrichment [link], contradicting VP Vance.`
  A sentence whose verb is immediately followed by a bare link is a bug — finish
  the clause first. If you don't actually know what the linked item says, don't
  cite it.
- Body in prose; quote or paraphrase real comments, attribute them inline with a
  link. No invented quotes, no invented engagement numbers — only what the tools
  returned.
- Order sections by where the signal actually was, strongest first. Omit a
  platform entirely if it returned nothing useful (don't pad with "no results").
- For COMPARISON: a short contrast paragraph or table at the end after the
  per-entity sections.
- No trailing bare "Sources:" block — links live inline next to claims.

## Notes
- Each scraper call costs accounts/proxies. Be deliberate: resolve first, then a
  focused batch — don't fan out 50 searches for a simple question.
- X and LinkedIn are heavier (browser/session backed) and can fail transiently
  (`502`). If one source errors, report from the others rather than aborting.
