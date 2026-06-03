"""
api/routes package initialization.
"""

from __future__ import annotations

import csv
import json
from io import StringIO
from typing import Any

from fastapi import Response

_MOJIBAKE_MARKERS = ("Ã", "Â", "â", "ðŸ", "ð\x9f")


def csv_response(data: Any, filename: str) -> Response:
    rows = _csv_rows(data)
    output = StringIO()

    if rows:
        fieldnames = list(dict.fromkeys(key for row in rows for key in row.keys()))
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in fieldnames})

    return Response(
        content=("\ufeff" + output.getvalue()).encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}.csv"},
    )


def _csv_rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        if isinstance(data.get("posts"), list):
            return data["posts"]
        if isinstance(data.get("comments"), list):
            return data["comments"]
        if isinstance(data.get("tweets"), list):
            return data["tweets"]
        if isinstance(data.get("post"), dict):
            return [data["post"]]
        if isinstance(data.get("details"), dict):
            return _csv_rows(data["details"])
        return [data]
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    return [{"value": data}]


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, str):
        return _repair_mojibake(value)
    return str(value)


def _repair_mojibake(value: str) -> str:
    if not _looks_mojibaked(value):
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

    return repaired if _mojibake_score(repaired) < _mojibake_score(value) else value


def _looks_mojibaked(value: str) -> bool:
    return any(marker in value for marker in _MOJIBAKE_MARKERS) or any(0x80 <= ord(char) <= 0x9F for char in value)


def _mojibake_score(value: str) -> int:
    return (
        sum(value.count(marker) for marker in _MOJIBAKE_MARKERS)
        + sum(1 for char in value if 0x80 <= ord(char) <= 0x9F)
        + (value.count("�") * 3)
    )
