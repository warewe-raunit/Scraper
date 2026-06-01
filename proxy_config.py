"""
proxy_config.py - shared proxy parsing for browser and CAPTCHA flows.

The browser and CAPTCHA providers expect slightly different shapes. Keeping the
parse in one place prevents the proxy from working in one path and silently
breaking in another.
"""

from __future__ import annotations

from urllib.parse import unquote, urlparse


DEFAULT_PROXY_PORTS = {
    "http": 80,
    "https": 443,
    "socks4": 1080,
    "socks5": 1080,
}


def parse_proxy_url(proxy_url: str | None) -> dict | None:
    if not proxy_url:
        return None

    value = proxy_url.strip().strip("\"'")
    if not value:
        return None

    parsed = urlparse(value)
    scheme = (parsed.scheme or "").lower()
    host = parsed.hostname
    port = parsed.port or DEFAULT_PROXY_PORTS.get(scheme)
    if not scheme or not host or not port:
        raise ValueError("Proxy URL must include scheme, host, and port")

    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    config = {
        "scheme": scheme,
        "host": host,
        "port": port,
        "server": f"{scheme}://{host}:{port}",
    }
    if username:
        config["username"] = username
        config["auth_user"] = username
    if password:
        config["password"] = password
        config["auth_pass"] = password
    return config


def playwright_proxy_config(proxy_url: str | None) -> dict | None:
    parsed = parse_proxy_url(proxy_url)
    if not parsed:
        return None

    config = {"server": parsed["server"]}
    if parsed.get("username"):
        config["username"] = parsed["username"]
    if parsed.get("password"):
        config["password"] = parsed["password"]
    return config


def captcha_proxy_config(proxy_url: str | None) -> dict | None:
    return parse_proxy_url(proxy_url)


def capsolver_proxy_url(proxy_config: dict | None) -> str:
    if not proxy_config:
        return ""

    server = str(proxy_config.get("server") or "").strip()
    parsed = None
    if server:
        parsed = parse_proxy_url(server)
        scheme = parsed["scheme"] if parsed else str(proxy_config.get("scheme") or "http")
        host = parsed["host"] if parsed else str(proxy_config.get("host") or "")
        port = parsed["port"] if parsed else proxy_config.get("port") or DEFAULT_PROXY_PORTS.get(scheme, 80)
    else:
        scheme = str(proxy_config.get("scheme") or "http").lower()
        host = str(proxy_config.get("host") or "")
        port = proxy_config.get("port") or DEFAULT_PROXY_PORTS.get(scheme, 80)

    username = str(proxy_config.get("auth_user") or proxy_config.get("username") or (parsed or {}).get("username") or "")
    password = str(proxy_config.get("auth_pass") or proxy_config.get("password") or (parsed or {}).get("password") or "")
    auth = f"{username}:{password}@" if username or password else ""
    return f"{scheme}://{auth}{host}:{port}"
