import ipaddress
import urllib.request
from unittest import mock
import pytest
import mcp_server as m

def test_public_literal_allowed_and_reserved_literal_denied(monkeypatch):
    monkeypatch.setenv("OPENMATRIX_SSRF_MODE", "off")
    assert m._resolve_public_ips("https://8.8.8.8/")[1] == "8.8.8.8"
    for ip in ("198.18.0.1", "127.0.0.1", "169.254.169.254", "[::ffff:169.254.169.254]"):
        with pytest.raises(ValueError): m._resolve_public_ips(f"http://{ip}/")

def test_cidr_cannot_allow_critical_dns_addresses(monkeypatch):
    monkeypatch.setenv("OPENMATRIX_SSRF_ALLOW_CIDRS", "0.0.0.0/0,::/0")
    monkeypatch.setenv("OPENMATRIX_SSRF_MODE", "off")
    for ip in ("127.0.0.1", "169.254.169.254", "::ffff:169.254.169.254"):
        with mock.patch("mcp_server.socket.getaddrinfo", return_value=[(2,1,6,"",(ip,80))]), pytest.raises(ValueError):
            m._resolve_public_ips("http://evil.example/")

def test_redirect_recalculates_proxy_permission_and_retains_pin(monkeypatch):
    monkeypatch.setenv("OPENMATRIX_USE_SYSTEM_PROXY", "true")
    req=urllib.request.Request("https://www.sec.gov/start")
    req._openmatrix_using_proxy=True
    req._openmatrix_proxy_allowed=True
    handler=m._SsrfsafeRedirectHandler()
    with mock.patch("mcp_server.socket.getaddrinfo", return_value=[(2,1,6,"",("93.184.216.34",443))]):
        evil=handler.redirect_request(req,None,302,"Found",{},"https://evil.example/end")
        assert not getattr(evil,"_openmatrix_proxy_allowed",False)
        assert not getattr(evil,"_openmatrix_using_proxy",False)
        assert evil._openmatrix_pinned_ip=="93.184.216.34"
        official=handler.redirect_request(evil,None,302,"Found",{},"https://data.sec.gov/end")
        assert official._openmatrix_proxy_allowed
