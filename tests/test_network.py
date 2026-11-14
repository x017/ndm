from __future__ import annotations

import os

import httpx

from ndm import network
from ndm.models import Settings


def test_proxy_options_cover_httpx_ytdlp_requests_and_credentials():
    settings = Settings(proxy_enabled=True, proxy_url="socks5://proxy.local:1080",
                        proxy_username="user name", proxy_password="p@ss:word")
    url = "socks5h://user%20name:p%40ss%3Aword@proxy.local:1080"
    assert network.proxy_url(settings) == url
    assert network.httpx_options(settings) == {"proxy": url, "trust_env": False}
    assert network.ytdlp_options(settings) == {"proxy": url}
    assert network.requests_proxies(settings) == {"http": url, "https": url}
    assert network.proxy_label(settings) == "socks5://proxy.local:1080"
    error = network.safe_error(RuntimeError(f"failed via {url} and p@ss:word"), settings)
    assert "p@ss" not in error and "user%20name" not in error and "***" in error
    local = Settings(proxy_enabled=True, proxy_url="socks5://127.0.0.1", proxy_remote_dns=False)
    assert network.proxy_url(local) == "socks5://127.0.0.1:1080"


def test_proxy_environment_overrides_and_restores(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://original:3128")
    monkeypatch.delenv("ALL_PROXY", raising=False)
    network._ORIGINAL_ENV = None
    network.configure_environment(Settings(proxy_enabled=True, proxy_url="http://new:8080"))
    assert os.environ["HTTP_PROXY"] == "http://new:8080"
    assert os.environ["ALL_PROXY"] == "http://new:8080"
    assert os.environ["NO_PROXY"] == ""
    network.configure_environment(Settings())
    assert os.environ["HTTP_PROXY"] == "http://original:3128"
    assert "ALL_PROXY" not in os.environ


def test_httpx_socks_transport_is_installed():
    settings = Settings(proxy_enabled=True, proxy_url="socks5://127.0.0.1:1080")
    with httpx.Client(**network.httpx_options(settings)) as client:
        assert client._mounts
