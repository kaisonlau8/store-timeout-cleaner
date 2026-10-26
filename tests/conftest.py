"""测试不允许真实出站；具体客户端测试可以在用例内替换为 mock。"""

import socket
import urllib.request

import pytest


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("测试禁止真实网络")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(urllib.request, "urlopen", blocked)
    try:
        import requests
    except ImportError:
        pass
    else:
        monkeypatch.setattr(requests.sessions.Session, "request", blocked)
    yield
    # 现有评分页会启动后台复核。保持出站阻断直到线程结束，避免重试跨用例
    # 占用相同日期键，或在 monkeypatch 撤销后意外访问真实接口。
    import score_llm
    score_llm.wait_review_jobs()
