"""
api/routes package initialization.
"""

from __future__ import annotations

import csv
import json
from io import StringIO
from typing import Any

from fastapi import Response

from api.utils.exporters import export_to_csv as _export_to_csv

def csv_response(data: Any, filename: str) -> Response:
    return _export_to_csv(data, filename)
