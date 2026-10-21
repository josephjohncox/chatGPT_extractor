import pytest
import requests


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Every test must supply its own transport; credentials from mise cannot be used."""

    def reject(*args, **kwargs):
        raise AssertionError("Network access is forbidden in the offline test suite")

    monkeypatch.setattr(requests.Session, "request", reject)
    for name in tuple(__import__("os").environ):
        if name.startswith("CHATGPT_"):
            monkeypatch.delenv(name)
