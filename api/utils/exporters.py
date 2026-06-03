"""
api/utils/exporters.py — Centralized data exporters for JSON, CSV, Excel, and HTML dashboards.
Provides unified exporting capability for Reddit, X (Twitter), and YouTube scrapers.
"""

from __future__ import annotations

import csv
import io
import json
import re
from typing import Any, Dict, List, Union
from fastapi import Response

_MOJIBAKE_MARKERS = ("Ã", "Â", "â", "ðŸ", "ð\x9f")

def _escape_html(text: Any) -> str:
    if text is None:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

def _repair_mojibake(value: str) -> str:
    if not any(marker in value for marker in _MOJIBAKE_MARKERS) and not any(0x80 <= ord(char) <= 0x9F for char in value):
        return value

    raw = bytearray()
    for char in value:
        codepoint = ord(char)
        if 0x80 <= codepoint <= 0x9F:
            raw.append(codepoint)
            continue
        try:
            raw.extend(char.encode("cp1252"))
        except UnicodeEncodeError:
            raw.extend(char.encode("utf-8"))

    try:
        repaired = bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        return value

    def score(val: str) -> int:
        return (
            sum(val.count(marker) for marker in _MOJIBAKE_MARKERS)
            + sum(1 for c in val if 0x80 <= ord(c) <= 0x9F)
            + (val.count("") * 3)
        )

    return repaired if score(repaired) < score(value) else value

def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, str):
        return _repair_mojibake(value)
    return str(value)

def _extract_rows(data: Any) -> List[Dict[str, Any]]:
    """Helper to extract flat rows from nested API outputs."""
    if isinstance(data, dict):
        if isinstance(data.get("posts"), list):
            return data["posts"]
        if isinstance(data.get("comments"), list):
            return data["comments"]
        if isinstance(data.get("tweets"), list):
            return data["tweets"]
        if isinstance(data.get("videos"), list):
            return data["videos"]
        if isinstance(data.get("post"), dict):
            return [data["post"]]
        if isinstance(data.get("details"), dict):
            return _extract_rows(data["details"])
        return [data]
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    return [{"value": data}]

_HEADER_MAPPING = {
    "video_id": "Video ID",
    "video_url": "Video URL",
    "channel_id": "Channel ID",
    "channel_url": "Channel URL",
    "comment_id": "Comment ID",
    "id": "ID",
    "url": "URL",
    "nsfw": "NSFW",
    "created_utc": "Created UTC",
    "body_html": "Body HTML",
    "post_id": "Post ID",
    "parent_id": "Parent ID",
    "subreddit_id": "Subreddit ID",
    "author_fullname": "Author Fullname",
}

_PREFERRED_ORDER = [
    # YouTube video keys
    "video_id", "title", "channel_name", "channel_id", "views", "published_time", "duration", "video_url",
    # YouTube comments keys
    "comment_id", "author", "text", "published_time", "like_count",
    # Reddit posts keys
    "id", "fullname", "title", "text", "username", "subreddit", "num_comments", "score", "url",
    # Reddit comments keys
    "id", "username", "body", "points", "subreddit", "url",
    # X tweets keys
    "id", "username", "fullname", "content", "date", "likes", "retweets", "replies", "link",
]

def _format_header(key: str) -> str:
    """Format a snake_case key into a clean Title Case header."""
    if key in _HEADER_MAPPING:
        return _HEADER_MAPPING[key]
    
    # Split by underscore
    words = key.split("_")
    formatted_words = []
    for w in words:
        if not w:
            continue
        w_lower = w.lower()
        if w_lower in ("id", "url", "utc", "html", "nsfw"):
            formatted_words.append(w.upper())
        else:
            formatted_words.append(w.capitalize())
    return " ".join(formatted_words)

# ================= CSV Export =================

def export_to_csv(data: Any, filename: str) -> Response:
    """Generate a downloadable CSV response with formatted headers."""
    rows = _extract_rows(data)
    output = io.StringIO()

    if rows:
        original_keys = list(dict.fromkeys(key for row in rows for key in row.keys()))
        
        # Sort keys based on preferred ordering
        key_to_original_idx = {k: idx for idx, k in enumerate(original_keys)}
        def key_sort_val(k):
            try:
                return (_PREFERRED_ORDER.index(k), key_to_original_idx[k])
            except ValueError:
                return (len(_PREFERRED_ORDER), key_to_original_idx[k])
        original_keys.sort(key=key_sort_val)
        
        mapped_headers = [_format_header(key) for key in original_keys]
        writer = csv.writer(output)
        writer.writerow(mapped_headers)
        for row in rows:
            writer.writerow([_csv_value(row.get(key)) for key in original_keys])

    return Response(
        content=("\ufeff" + output.getvalue()).encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}.csv"},
    )

# ================= Excel (HTML Table) Export =================

def export_to_excel(data: Any, filename: str) -> Response:
    """Generate an Excel-compatible HTML spreadsheet response with formatted headers."""
    rows = _extract_rows(data)
    
    html = '<html xmlns:o="urn:schemas-microsoft-com:office:office" xmlns:x="urn:schemas-microsoft-com:office:excel" xmlns="http://www.w3.org/TR/REC-html40">\n'
    html += '<head><meta http-equiv="Content-type" content="text/html;charset=utf-8" />\n'
    html += '<style>td { border: 0.5pt solid #cccccc; } th { background-color: #f2f2f2; font-weight: bold; border: 0.5pt solid #cccccc; }</style></head>\n'
    html += '<body><table>\n'
    
    if rows:
        original_keys = list(dict.fromkeys(key for row in rows for key in row.keys()))
        
        # Sort keys based on preferred ordering
        key_to_original_idx = {k: idx for idx, k in enumerate(original_keys)}
        def key_sort_val(k):
            try:
                return (_PREFERRED_ORDER.index(k), key_to_original_idx[k])
            except ValueError:
                return (len(_PREFERRED_ORDER), key_to_original_idx[k])
        original_keys.sort(key=key_sort_val)
        
        mapped_headers = [_format_header(key) for key in original_keys]
        html += '<tr>' + ''.join(f'<th>{_escape_html(name)}</th>' for name in mapped_headers) + '</tr>\n'
        for row in rows:
            html += '<tr>' + ''.join(f'<td>{_escape_html(_csv_value(row.get(key)))}</td>' for key in original_keys) + '</tr>\n'
            
    html += '</table></body></html>'
    
    return Response(
        content=html.encode("utf-8"),
        media_type="application/vnd.ms-excel",
        headers={"Content-Disposition": f"attachment; filename={filename}.xls"},
    )

# ================= Premium HTML Dashboards =================

def export_to_html_dashboard(data: Any, source_type: str) -> str:
    """
    Generate a highly polished HTML Dashboard for displaying scraped data.
    Supports Reddit posts/comments/users, X profiles/tweets, and YouTube videos/comments.
    """
    source_type = source_type.lower()
    
    dashboard = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{page_title}</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-dark: #0f172a;
            --card-bg: rgba(30, 41, 59, 0.7);
            --border-glow: rgba(59, 130, 246, 0.3);
            --primary-accent: #3b82f6;
            --accent-glow: #60a5fa;
            --text-main: #f8fafc;
            --text-secondary: #94a3b8;
            --reddit-accent: #ff4500;
            --x-accent: #1da1f2;
            --youtube-accent: #ff0000;
        }
        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }
        body {
            font-family: 'Outfit', sans-serif;
            background: linear-gradient(135deg, #090d16 0%, var(--bg-dark) 100%);
            color: var(--text-main);
            min-height: 100vh;
            padding: 40px 20px;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
        }
        header {
            text-align: center;
            margin-bottom: 50px;
            animation: fadeInDown 0.8s ease-out;
        }
        header h1 {
            font-size: 3rem;
            font-weight: 800;
            background: linear-gradient(to right, #3b82f6, #8b5cf6);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 10px;
        }
        header p {
            color: var(--text-secondary);
            font-size: 1.1rem;
        }
        .dashboard-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
            gap: 30px;
            animation: fadeInUp 0.8s ease-out;
        }
        .card {
            background: var(--card-bg);
            border: 1px solid rgba(255, 255, 255, 0.05);
            border-radius: 16px;
            backdrop-filter: blur(12px);
            overflow: hidden;
            transition: all 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275);
            display: flex;
            flex-direction: column;
        }
        .card:hover {
            transform: translateY(-8px);
            border-color: var(--primary-accent);
            box-shadow: 0 10px 30px rgba(59, 130, 246, 0.15);
        }
        .thumbnail-container {
            position: relative;
            width: 100%;
            padding-top: 56.25%;
            background-color: #000;
            overflow: hidden;
        }
        .thumbnail-container img {
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            object-fit: cover;
            transition: transform 0.6s;
        }
        .card:hover .thumbnail-container img {
            transform: scale(1.05);
        }
        .duration-badge {
            position: absolute;
            bottom: 10px;
            right: 10px;
            background: rgba(0, 0, 0, 0.85);
            color: #fff;
            padding: 4px 8px;
            border-radius: 6px;
            font-size: 0.8rem;
            font-weight: 600;
        }
        .card-content {
            padding: 20px;
            display: flex;
            flex-direction: column;
            flex-grow: 1;
        }
        .card-title {
            font-size: 1.15rem;
            font-weight: 600;
            line-height: 1.4;
            margin-bottom: 12px;
            display: -webkit-box;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
            overflow: hidden;
            color: var(--text-main);
        }
        .card-channel {
            font-size: 0.9rem;
            color: var(--primary-accent);
            margin-bottom: 15px;
            text-decoration: none;
            display: inline-block;
        }
        .card-channel:hover {
            text-decoration: underline;
        }
        .card-meta {
            display: flex;
            justify-content: space-between;
            font-size: 0.85rem;
            color: var(--text-secondary);
            margin-top: auto;
        }
        .video-details-container {
            background: var(--card-bg);
            border: 1px solid rgba(255, 255, 255, 0.05);
            border-radius: 24px;
            backdrop-filter: blur(12px);
            padding: 40px;
            margin-bottom: 40px;
            animation: fadeInUp 0.8s ease-out;
        }
        .video-header {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 40px;
            margin-bottom: 40px;
        }
        @media (max-width: 768px) {
            .video-header {
                grid-template-columns: 1fr;
            }
        }
        .video-player-mockup {
            width: 100%;
            border-radius: 16px;
            overflow: hidden;
            box-shadow: 0 10px 40px rgba(0,0,0,0.5);
        }
        .video-info h2 {
            font-size: 2rem;
            margin-bottom: 15px;
            line-height: 1.3;
        }
        .video-stats {
            display: flex;
            gap: 20px;
            margin-bottom: 25px;
            flex-wrap: wrap;
        }
        .stat-badge {
            background: rgba(255, 255, 255, 0.05);
            padding: 8px 16px;
            border-radius: 30px;
            font-size: 0.95rem;
            font-weight: 600;
            color: var(--accent-glow);
        }
        .video-desc {
            background: rgba(0, 0, 0, 0.2);
            padding: 20px;
            border-radius: 12px;
            color: var(--text-secondary);
            font-size: 0.95rem;
            line-height: 1.6;
            max-height: 200px;
            overflow-y: auto;
            white-space: pre-wrap;
        }
        .comments-section {
            margin-top: 40px;
        }
        .comments-section h3 {
            font-size: 1.5rem;
            margin-bottom: 25px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.1);
            padding-bottom: 10px;
        }
        .comment-item {
            display: flex;
            gap: 15px;
            padding: 20px;
            background: rgba(255, 255, 255, 0.02);
            border-radius: 12px;
            margin-bottom: 15px;
            border: 1px solid rgba(255, 255, 255, 0.02);
        }
        .comment-avatar {
            width: 44px;
            height: 44px;
            border-radius: 50%;
            overflow: hidden;
            background: #fff;
            flex-shrink: 0;
        }
        .comment-avatar img {
            width: 100%;
            height: 100%;
            object-fit: cover;
        }
        .comment-body h4 {
            font-size: 0.95rem;
            font-weight: 600;
            margin-bottom: 6px;
            color: var(--text-main);
        }
        .comment-body p {
            color: var(--text-secondary);
            font-size: 0.95rem;
            line-height: 1.5;
            margin-bottom: 8px;
        }
        .comment-meta {
            font-size: 0.85rem;
            color: var(--text-secondary);
            display: flex;
            gap: 15px;
        }
        .reddit-theme {
            --primary-accent: var(--reddit-accent);
            --accent-glow: #ff5722;
        }
        .x-theme {
            --primary-accent: var(--x-accent);
            --accent-glow: #00b0ff;
        }
        .youtube-theme {
            --primary-accent: var(--youtube-accent);
            --accent-glow: #ff3333;
        }
        @keyframes fadeInDown {
            from { opacity: 0; transform: translateY(-20px); }
            to { opacity: 1; transform: translateY(0); }
        }
        @keyframes fadeInUp {
            from { opacity: 0; transform: translateY(20px); }
            to { opacity: 1; transform: translateY(0); }
        }
    </style>
</head>
<body class="{theme_class}">
    <div class="container">
"""
    source_display = {"youtube": "YouTube", "reddit": "Reddit", "x": "X"}.get(source_type, source_type.title())
    page_title = f"{source_display} Stealth Scraper Export"
    dashboard = dashboard.replace("{theme_class}", f"{source_type}-theme").replace("{page_title}", page_title)

    if source_type == "youtube":
        if isinstance(data, dict) and "videos" in data:
            title = "YouTube Search"
            query = data.get("query", "")
            subtitle = f"Search Results for: {query}" if query else "Videos Extract"
            
            dashboard += f"""
        <header>
            <h1>{title}</h1>
            <p>{subtitle} — {data.get('results_count', 0)} videos extracted</p>
        </header>
        <div class="dashboard-grid">
"""
            for v in data.get("videos", []):
                thumb = v.get("thumbnails", [{}])[0].get("url", "")
                dashboard += f"""
            <div class="card">
                <div class="thumbnail-container">
                    <img src="{_escape_html(thumb)}" alt="thumbnail">
                    <span class="duration-badge">{_escape_html(v.get("duration", ""))}</span>
                </div>
                <div class="card-content">
                    <h3 class="card-title">{_escape_html(v.get("title", ""))}</h3>
                    <a href="{_escape_html(v.get("channel_url", "#"))}" target="_blank" class="card-channel">{_escape_html(v.get("channel_name", ""))}</a>
                    <div class="card-meta">
                        <span>{_escape_html(v.get("views", ""))}</span>
                        <span>{_escape_html(v.get("published_time", ""))}</span>
                    </div>
                </div>
            </div>
"""
            dashboard += "</div>"
            
        elif isinstance(data, dict) and "video_id" in data: # video details report
            thumb = data.get("thumbnails", [{}])[-1].get("url", "")
            channel = data.get("channel", {})
            dashboard += f"""
        <header>
            <h1>Video Insights</h1>
            <p>Stealth Data Extraction Report</p>
        </header>
        <div class="video-details-container">
            <div class="video-header">
                <div>
                    <img src="{_escape_html(thumb)}" class="video-player-mockup" alt="thumbnail">
                </div>
                <div class="video-info">
                    <h2>{_escape_html(data.get("title", ""))}</h2>
                    <div class="video-stats">
                        <span class="stat-badge">{_escape_html(data.get("view_count", "0"))} views</span>
                        <span class="stat-badge">{_escape_html(data.get("like_count", "0"))} likes</span>
                        <span class="stat-badge">{data.get("length_seconds", 0)}s duration</span>
                    </div>
                    <div style="margin-bottom: 20px;">
                        <a href="{_escape_html(channel.get("url", "#"))}" target="_blank" style="color: var(--primary-accent); text-decoration: none; font-weight: 600; font-size: 1.1rem;">
                            {_escape_html(channel.get("name", ""))}
                        </a>
                        <span style="color: var(--text-secondary); margin-left: 10px; font-size: 0.95rem;">{_escape_html(channel.get("subscribers", ""))}</span>
                    </div>
                    <div class="video-desc">{_escape_html(data.get("description", ""))}</div>
                </div>
            </div>
            
            <div class="comments-section">
                <h3>Top Comments ({len(data.get("comments", []))})</h3>
"""
            for c in data.get("comments", []):
                dashboard += f"""
                <div class="comment-item">
                    <div class="comment-avatar">
                        <img src="{_escape_html(c.get("author_thumbnail", ""))}" alt="avatar">
                    </div>
                    <div class="comment-body">
                        <h4>{_escape_html(c.get("author", ""))}</h4>
                        <p>{_escape_html(c.get("text", ""))}</p>
                        <div class="comment-meta">
                            <span>Likes: {_escape_html(c.get("like_count", "0"))}</span>
                            <span>{_escape_html(c.get("published_time", ""))}</span>
                        </div>
                    </div>
                </div>
"""
            dashboard += "</div></div>"
            
        elif isinstance(data, dict) and "comments" in data: # comments list
            dashboard += f"""
        <header>
            <h1>Comment Thread Export</h1>
            <p>{len(data.get("comments", []))} comments extracted</p>
        </header>
        <div class="comments-section" style="max-width: 800px; margin: 0 auto;">
"""
            for c in data.get("comments", []):
                dashboard += f"""
            <div class="comment-item" style="background: var(--card-bg);">
                <div class="comment-avatar">
                    <img src="{_escape_html(c.get("author_thumbnail", ""))}" alt="avatar">
                </div>
                <div class="comment-body">
                    <h4>{_escape_html(c.get("author", ""))}</h4>
                    <p>{_escape_html(c.get("text", ""))}</p>
                    <div class="comment-meta">
                        <span>Likes: {_escape_html(c.get("like_count", "0"))}</span>
                        <span>{_escape_html(c.get("published_time", ""))}</span>
                    </div>
                </div>
            </div>
"""
            dashboard += "</div>"
            
    elif source_type == "reddit":
        rows = _extract_rows(data)
        title = "Reddit Stealth Scraping"
        subtitle = "Scraped Reddit Feed Data"
        if isinstance(data, dict):
            if "subreddit" in data:
                title = f"r/{data['subreddit']}"
                subtitle = f"Subreddit posts sorted by {data.get('sort', 'hot')}"
            elif "query" in data:
                title = f"Search Reddit: {data['query']}"
                subtitle = f"Search results sorted by {data.get('sort', 'relevance')}"
            elif "username" in data:
                title = f"u/{data['username']}"
                subtitle = f"User profile feed summary"
                
        dashboard += f"""
        <header>
            <h1>{title}</h1>
            <p>{subtitle} — {len(rows)} items extracted</p>
        </header>
        <div style="max-width: 800px; margin: 0 auto;">
"""
        # Render posts/comments list
        for r in rows:
            item_title = r.get("title")
            item_text = r.get("text") or r.get("body") or ""
            author = r.get("username")
            sub = r.get("subreddit")
            score = r.get("score") or r.get("upvotes") or r.get("points") or 0
            ago = r.get("published_ago") or r.get("published_at") or ""
            
            # Extract image preview if exists
            img_html = ""
            images = r.get("images")
            if images and isinstance(images, list) and len(images) > 0:
                img_html = f'<img src="{_escape_html(images[0])}" style="max-width: 100%; max-height: 400px; border-radius: 8px; margin: 15px 0; object-fit: cover;">'
                
            dashboard += f"""
            <div class="comment-item" style="background: var(--card-bg); flex-direction: column; gap: 8px;">
                <div style="display: flex; justify-content: space-between; font-size: 0.85rem; color: var(--text-secondary);">
                    <span>Posted by u/{_escape_html(author)} {f"in r/{_escape_html(sub)}" if sub else ""}</span>
                    <span>{_escape_html(ago)}</span>
                </div>
                {f'<h3 style="margin: 5px 0 10px 0; font-weight: 600; font-size: 1.25rem;">{_escape_html(item_title)}</h3>' if item_title else ''}
                <div style="font-size: 0.95rem; line-height: 1.6; color: var(--text-main); white-space: pre-wrap;">{_escape_html(item_text[:800])}{"..." if len(item_text) > 800 else ""}</div>
                {img_html}
                <div style="display: flex; gap: 20px; font-size: 0.9rem; font-weight: 600; color: var(--primary-accent); margin-top: 10px;">
                    <span>▲ {score} upvotes</span>
                    {f'<span>💬 {r["num_comments"]} comments</span>' if "num_comments" in r else ''}
                    {f'<a href="{_escape_html(r["url"])}" target="_blank" style="color: var(--accent-glow); text-decoration: none;">View Original</a>' if r.get("url") else ''}
                </div>
            </div>
"""
        dashboard += "</div>"
        
    elif source_type == "x":
        rows = _extract_rows(data)
        title = "X (Twitter) Feed"
        subtitle = "Unauthenticated X Scraper Timeline"
        profile_header = ""
        
        if isinstance(data, dict) and data.get("profile"):
            profile = data["profile"]
            stats = profile.get("stats", {})
            profile_header = f"""
            <div class="video-details-container" style="max-width: 800px; margin: 0 auto 30px auto; padding: 25px;">
                <h2 style="font-size: 1.8rem; margin-bottom: 8px;">{_escape_html(profile.get("fullname", ""))}</h2>
                <div style="color: var(--primary-accent); font-weight: 600; margin-bottom: 12px;">@{_escape_html(profile.get("username", ""))}</div>
                <p style="color: var(--text-secondary); line-height: 1.5; margin-bottom: 15px;">{_escape_html(profile.get("bio", ""))}</p>
                <div style="display: flex; gap: 15px; flex-wrap: wrap; font-size: 0.9rem; color: var(--text-secondary);">
                    {f'<span>📍 {profile["location"]}</span>' if profile.get("location") else ''}
                    {f'<span>🔗 <a href="{profile["website"]}" target="_blank" style="color: var(--accent-glow);">{profile["website"]}</a></span>' if profile.get("website") else ''}
                    {f'<span>📅 {profile["joined"]}</span>' if profile.get("joined") else ''}
                </div>
                <div style="display: flex; gap: 25px; margin-top: 20px; border-top: 1px solid rgba(255,255,255,0.05); padding-top: 15px;">
                    {"".join(f'<div style="font-weight: 600;">{_escape_html(val)} <span style="font-weight: 400; color: var(--text-secondary);">{label}</span></div>' for label, val in stats.items())}
                </div>
            </div>
"""
            title = f"@{profile.get('username')}'s timeline"
            
        dashboard += f"""
        <header>
            <h1>{title}</h1>
            <p>{subtitle} — {len(rows)} tweets extracted</p>
        </header>
        {profile_header}
        <div style="max-width: 800px; margin: 0 auto;">
"""
        for r in rows:
            stats = r.get("stats", {})
            avatar = r.get("avatar")
            avatar_html = f'<div class="comment-avatar"><img src="{_escape_html(avatar)}" alt="avatar"></div>' if avatar else ''
            
            dashboard += f"""
            <div class="comment-item" style="background: var(--card-bg);">
                {avatar_html}
                <div class="comment-body" style="width: 100%;">
                    <div style="display: flex; justify-content: space-between; margin-bottom: 8px;">
                        <div>
                            <strong>{_escape_html(r.get("fullname", ""))}</strong>
                            <span style="color: var(--text-secondary); margin-left: 8px;">@{_escape_html(r.get("username", ""))}</span>
                        </div>
                        <span style="color: var(--text-secondary); font-size: 0.85rem;">{_escape_html(r.get("date", ""))}</span>
                    </div>
                    <p style="white-space: pre-wrap; margin-bottom: 12px; line-height: 1.5; color: var(--text-main); font-size: 1.05rem;">{_escape_html(r.get("content", r.get("text", "")))}</p>
                    <div style="display: flex; gap: 25px; font-size: 0.85rem; font-weight: 600; color: var(--text-secondary);">
                        <span>💬 {stats.get("replies", 0)} Replies</span>
                        <span>🔁 {stats.get("retweets", 0)} Retweets</span>
                        <span>❤️ {stats.get("likes", 0)} Likes</span>
                        {f'<span>📊 {r["views"]} Views</span>' if r.get("views") else ''}
                        {f'<a href="{_escape_html(r["link"])}" target="_blank" style="color: var(--primary-accent); text-decoration: none; margin-left: auto;">View Post</a>' if r.get("link") else ''}
                    </div>
                </div>
            </div>
"""
        dashboard += "</div>"
        
    dashboard += """
    </div>
</body>
</html>
"""
    return dashboard

def export_download_page_html(data: Dict[str, Any]) -> str:
    """Render an HTML page for direct downloads of YouTube videos."""
    title = data.get("title", "YouTube Video")
    resolution = data.get("resolution", "360p")
    download_url = data.get("download_url", "")
    
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Download {title}</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;800&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg-dark: #0f172a;
            --card-bg: rgba(30, 41, 59, 0.7);
            --border-glow: rgba(59, 130, 246, 0.3);
            --primary-accent: #3b82f6;
            --accent-glow: #60a5fa;
            --text-main: #f8fafc;
            --text-secondary: #94a3b8;
        }}
        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }}
        body {{
            font-family: 'Outfit', sans-serif;
            background: linear-gradient(135deg, #090d16 0%, var(--bg-dark) 100%);
            color: var(--text-main);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }}
        .container {{
            max-width: 650px;
            width: 100%;
            background: var(--card-bg);
            border: 1px solid rgba(255, 255, 255, 0.05);
            border-radius: 24px;
            backdrop-filter: blur(12px);
            padding: 40px;
            box-shadow: 0 20px 50px rgba(0, 0, 0, 0.3);
            text-align: center;
            animation: fadeInUp 0.6s ease-out;
        }}
        .icon-container {{
            margin-bottom: 25px;
            display: inline-block;
            background: linear-gradient(135deg, var(--primary-accent), #8b5cf6);
            padding: 20px;
            border-radius: 50%;
            box-shadow: 0 8px 24px rgba(59, 130, 246, 0.3);
        }}
        .icon-container svg {{
            width: 40px;
            height: 40px;
            fill: #fff;
            display: block;
        }}
        h1 {{
            font-size: 1.8rem;
            font-weight: 800;
            background: linear-gradient(to right, #3b82f6, #8b5cf6);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 15px;
            line-height: 1.3;
        }}
        .meta-badge {{
            display: inline-block;
            background: rgba(255, 255, 255, 0.05);
            padding: 6px 16px;
            border-radius: 30px;
            font-size: 0.9rem;
            font-weight: 600;
            color: var(--accent-glow);
            margin-bottom: 30px;
            border: 1px solid rgba(255, 255, 255, 0.05);
        }}
        .btn-download {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 10px;
            width: 100%;
            padding: 16px 32px;
            background: linear-gradient(135deg, #3b82f6 0%, #8b5cf6 100%);
            color: #fff;
            text-decoration: none;
            font-size: 1.1rem;
            font-weight: 600;
            border-radius: 12px;
            box-shadow: 0 4px 20px rgba(59, 130, 246, 0.3);
            transition: all 0.3s ease;
            margin-bottom: 25px;
            border: none;
            cursor: pointer;
        }}
        .btn-download:hover {{
            transform: translateY(-3px);
            box-shadow: 0 8px 30px rgba(59, 130, 246, 0.5);
        }}
        .instructions-card {{
            background: rgba(0, 0, 0, 0.2);
            border-radius: 12px;
            padding: 20px;
            text-align: left;
            margin-top: 25px;
            border: 1px solid rgba(255, 255, 255, 0.02);
        }}
        .instructions-card h3 {{
            font-size: 1rem;
            margin-bottom: 12px;
            color: var(--text-main);
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        .instructions-card h3 svg {{
            width: 18px;
            height: 18px;
            fill: var(--accent-glow);
            display: block;
        }}
        .instructions-card ol {{
            padding-left: 20px;
            color: var(--text-secondary);
            font-size: 0.9rem;
            line-height: 1.6;
        }}
        .instructions-card li {{
            margin-bottom: 8px;
        }}
        .footer-text {{
            margin-top: 30px;
            font-size: 0.8rem;
            color: var(--text-secondary);
        }}
        @keyframes fadeInUp {{
            from {{ opacity: 0; transform: translateY(20px); }}
            to {{ opacity: 1; transform: translateY(0); }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="icon-container">
            <svg viewBox="0 0 24 24">
                <path d="M19.35 10.04C18.67 6.59 15.64 4 12 4 9.11 4 6.6 5.64 5.35 8.04 2.34 8.36 0 10.91 0 14c0 3.31 2.69 6 6 6h13c2.76 0 5-2.24 5-5 0-2.64-2.05-4.78-4.65-4.96zM17 13l-5 5-5-5h3V9h4v4h3z"/>
            </svg>
        </div>
        <h1>{_escape_html(title)}</h1>
        <div class="meta-badge">Resolution: {resolution}</div>
        
        <a href="{download_url}" target="_blank" download class="btn-download">
            <svg style="width:20px;height:20px;fill:currentColor" viewBox="0 0 24 24">
                <path d="M19.35 10.04C18.67 6.59 15.64 4 12 4 9.11 4 6.6 5.64 5.35 8.04 2.34 8.36 0 10.91 0 14c0 3.31 2.69 6 6 6h13c2.76 0 5-2.24 5-5 0-2.64-2.05-4.78-4.65-4.96zM17 13l-5 5-5-5h3V9h4v4h3z"/>
            </svg>
            Download Video
        </a>

        <div class="instructions-card">
            <h3>
                <svg viewBox="0 0 24 24">
                    <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/>
                </svg>
                How to Download
            </h3>
            <ol>
                <li>Click the <strong>Download Video</strong> button above to open the video stream in a new tab.</li>
                <li>In the new tab, <strong>right-click</strong> on the video player.</li>
                <li>Select <strong>"Save Video As..."</strong> (or press <code>Ctrl + S</code>) to save the file directly to your local device.</li>
            </ol>
        </div>
        
        <p class="footer-text">Reddit Stealth Scraper — Bypass Server Storage</p>
    </div>
</body>
</html>"""
