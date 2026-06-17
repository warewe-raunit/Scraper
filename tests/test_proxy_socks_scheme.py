"""
tests/test_proxy_socks_scheme.py — SOCKS proxies must use REMOTE DNS.

The pool emits socks5h:// / socks4a:// so curl_cffi resolves the target through
the proxy (plain socks5:// = local DNS fails with curl err 97). Chromium/CAPTCHA
only understand the plain schemes, so parse_proxy_url collapses the aliases.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.proxy_provider import GoodProxiesProvider  # noqa: E402
from tools.proxy_config import playwright_proxy_config, parse_proxy_url  # noqa: E402


def test_pool_emits_remote_dns_socks_schemes():
    f = GoodProxiesProvider._to_proxy_url
    assert f({"ip": "1.2.3.4:1080", "type": "socks5"}) == "socks5h://1.2.3.4:1080"
    assert f({"ip": "1.2.3.4:1080", "type": "socks4"}) == "socks4a://1.2.3.4:1080"
    assert f({"ip": "1.2.3.4:8080", "type": "http"}) == "http://1.2.3.4:8080"
    # https proxies are contacted over the http scheme by curl (CONNECT does TLS)
    assert f({"ip": "1.2.3.4:8080", "type": "https"}) == "http://1.2.3.4:8080"


def test_playwright_collapses_remote_dns_aliases():
    # Chromium does remote DNS for socks5 natively and rejects the 'h'/'a' aliases.
    assert playwright_proxy_config("socks5h://1.2.3.4:1080")["server"] == "socks5://1.2.3.4:1080"
    assert playwright_proxy_config("socks4a://1.2.3.4:1080")["server"] == "socks4://1.2.3.4:1080"
    assert playwright_proxy_config("http://1.2.3.4:8080")["server"] == "http://1.2.3.4:8080"
    assert parse_proxy_url("socks5h://1.2.3.4:1080")["scheme"] == "socks5"


if __name__ == "__main__":
    test_pool_emits_remote_dns_socks_schemes()
    test_playwright_collapses_remote_dns_aliases()
    print("OK")
