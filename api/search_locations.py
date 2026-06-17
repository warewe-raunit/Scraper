"""
api/search_locations.py — Shared search-location enum for unauthenticated
scraper search endpoints (YouTube, X).

Rendered as a dropdown in the FastAPI Swagger UI. The enum *value* is a
human-readable label ("United States (US)") so the dropdown shows full country
names, while the member *name* is the bare ISO country code ("US") that the
proxy layer expects. Routes forward ``location.name`` to the services, so the
service/proxy code keeps dealing in plain country codes.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional


# Best-effort primary interface language (``hl``) per supported country, so the
# InnerTube ``gl``/``hl`` signals agree with the proxy's exit country instead of
# being hardcoded to US/English. English is used for multilingual markets where
# YouTube's UI default is English (GB/CA/IN/AU/SG).
COUNTRY_TO_HL = {
    "US": "en",
    "DE": "de",
    "FR": "fr",
    "GB": "en",
    "CA": "en",
    "IN": "en",
    "JP": "ja",
    "BR": "pt",
    "AU": "en",
    "UA": "uk",
    "RU": "ru",
    "SG": "en",
    "NL": "nl",
    "ES": "es",
    "IT": "it",
    "PL": "pl",
}


def hl_for_country(code: Optional[str]) -> str:
    """Interface-language code (``hl``) for a country code; defaults to ``en``."""
    if not code:
        return "en"
    return COUNTRY_TO_HL.get(code.strip().upper(), "en")


class SearchLocation(str, Enum):
    US = "United States (US)"
    DE = "Germany (DE)"
    FR = "France (FR)"
    GB = "United Kingdom (GB)"
    CA = "Canada (CA)"
    IN = "India (IN)"
    JP = "Japan (JP)"
    BR = "Brazil (BR)"
    AU = "Australia (AU)"
    UA = "Ukraine (UA)"
    RU = "Russia (RU)"
    SG = "Singapore (SG)"
    NL = "Netherlands (NL)"
    ES = "Spain (ES)"
    IT = "Italy (IT)"
    PL = "Poland (PL)"

    @property
    def code(self) -> str:
        """The bare ISO country code (e.g. ``"US"``) the proxy layer expects."""
        return self.name
