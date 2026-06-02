"""
api/routes package initialization.
"""

from __future__ import annotations

import csv
import json
from io import StringIO
from typing import Any

from fastapi import Response


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
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}.csv"},
    )


def _csv_rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        if isinstance(data.get("posts"), list):
            return data["posts"]
        if isinstance(data.get("comments"), list):
            return data["comments"]
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
    return str(value)
