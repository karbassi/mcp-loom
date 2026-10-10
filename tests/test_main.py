import pytest

from loom_mcp import server


@pytest.fixture
def run_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(server.mcp, "run", lambda *a, **kw: calls.append((a, kw)))
    for var in ("LOOM_MCP_TRANSPORT", "LOOM_MCP_HOST", "LOOM_MCP_PORT"):
        monkeypatch.delenv(var, raising=False)
    return calls


def test_defaults_to_stdio(run_calls):
    server.main()
    assert run_calls == [((), {"transport": "stdio"})]


def test_http_defaults_to_loopback_with_host_origin_protection(monkeypatch, run_calls):
    monkeypatch.setenv("LOOM_MCP_TRANSPORT", "http")
    server.main()
    assert run_calls == [
        (
            (),
            {
                "transport": "http",
                "host": "127.0.0.1",
                "port": 8505,
                "show_banner": False,
                "host_origin_protection": True,
            },
        )
    ]


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_http_host_and_port_from_env(monkeypatch, run_calls, host):
    monkeypatch.setenv("LOOM_MCP_TRANSPORT", "http")
    monkeypatch.setenv("LOOM_MCP_HOST", host)
    monkeypatch.setenv("LOOM_MCP_PORT", "9000")
    server.main()
    assert run_calls[0][1]["host"] == host
    assert run_calls[0][1]["port"] == 9000


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "100.72.126.89", "example.com"])
def test_http_refuses_non_loopback_host(monkeypatch, run_calls, host):
    # No auth on the endpoint: anything that can reach it acts as the cookie owner.
    monkeypatch.setenv("LOOM_MCP_TRANSPORT", "http")
    monkeypatch.setenv("LOOM_MCP_HOST", host)
    with pytest.raises(SystemExit):
        server.main()
    assert run_calls == []


def test_rejects_unknown_transport(monkeypatch, run_calls):
    monkeypatch.setenv("LOOM_MCP_TRANSPORT", "sse")
    with pytest.raises(SystemExit):
        server.main()
    assert run_calls == []
