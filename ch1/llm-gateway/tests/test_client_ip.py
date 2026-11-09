"""客户端 IP 解析：X-Forwarded-For 只在可信代理之后才采信（防审计字段伪造）。"""

from __future__ import annotations

from starlette.requests import Request

from app.api.chat import _client_ip
from app.core.config_manager import AppConfig


def _make_request(peer: str, xff: str | None = None) -> Request:
    headers = []
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode()))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/v1/chat/completions",
        "headers": headers,
        "client": (peer, 12345),
        "scheme": "http",
        "server": ("testserver", 80),
        "query_string": b"",
    }
    return Request(scope)


def _cfg(trust: bool, proxies: tuple[str, ...] = ()) -> AppConfig:
    cfg = AppConfig()
    cfg.trust_proxy_headers = trust
    cfg.trusted_proxies = list(proxies)
    return cfg


def test_xff_ignored_by_default():
    """默认不信任 XFF：即使带了伪造头，也取 TCP 对端地址。"""
    req = _make_request("10.0.0.5", "9.9.9.9")
    assert _client_ip(req, _cfg(trust=False)) == "10.0.0.5"


def test_trusted_proxy_uses_rightmost_untrusted_hop():
    """可信代理之后：从右向左跳过代理，返回第一个非可信地址（左侧伪造值被忽略）。"""
    req = _make_request("127.0.0.1", "1.1.1.1, 8.8.8.8, 127.0.0.1")
    assert _client_ip(req, _cfg(trust=True, proxies=("127.0.0.1",))) == "8.8.8.8"


def test_untrusted_peer_ignores_xff():
    """开启信任但直连来源不在白名单：仍忽略 XFF。"""
    req = _make_request("10.0.0.5", "9.9.9.9")
    assert _client_ip(req, _cfg(trust=True, proxies=("127.0.0.1",))) == "10.0.0.5"


def test_cidr_trusted_proxy():
    """trusted_proxies 支持 CIDR 网段。"""
    req = _make_request("10.1.2.3", "203.0.113.9, 10.1.2.3")
    assert _client_ip(req, _cfg(trust=True, proxies=("10.0.0.0/8",))) == "203.0.113.9"


def test_invalid_xff_entry_falls_back_to_peer():
    """XFF 里全是非法地址时回落到对端地址，不把垃圾值写进 trace。"""
    req = _make_request("10.0.0.1", "not-an-ip, also-bad")
    assert _client_ip(req, _cfg(trust=True, proxies=("10.0.0.1",))) == "10.0.0.1"


def test_no_peer_returns_unknown():
    """ASGI 未提供 client 时返回 unknown 而不是崩溃。"""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "scheme": "http",
        "server": ("testserver", 80),
        "query_string": b"",
    }
    assert _client_ip(Request(scope), _cfg(trust=True, proxies=("127.0.0.1",))) == "unknown"
