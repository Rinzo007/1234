from types import SimpleNamespace

import pytest

from overture.release import OvertureReleaseError, resolve_overture_release


def test_explicit_release_does_not_touch_overturemaps():
    assert resolve_overture_release("2025-01-15") == "2025-01-15"


def test_latest_release_is_resolved(monkeypatch):
    fake_core = SimpleNamespace(get_latest_release=lambda: "2026-09-01")
    fake_module = SimpleNamespace(core=fake_core)
    monkeypatch.setitem(__import__("sys").modules, "overturemaps", fake_module)
    assert resolve_overture_release("latest") == "2026-09-01"
    assert resolve_overture_release("current") == "2026-09-01"


def test_latest_release_failure_is_explicit(monkeypatch):
    def boom():
        raise RuntimeError("network")

    fake_core = SimpleNamespace(get_latest_release=boom)
    monkeypatch.setitem(
        __import__("sys").modules,
        "overturemaps",
        SimpleNamespace(core=fake_core),
    )
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("network")))
    with pytest.raises(OvertureReleaseError):
        resolve_overture_release("latest")


def test_latest_release_falls_back_to_stac_catalog(monkeypatch):
    import json

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"links": [{"rel": "child", "href": "https://example.test/2026-08-19.0/catalog.json"}]}).encode()

    def boom():
        raise RuntimeError("api changed")

    monkeypatch.setitem(__import__("sys").modules, "overturemaps", SimpleNamespace(core=SimpleNamespace(get_latest_release=boom)))
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: Response())
    assert resolve_overture_release("latest") == "2026-08-19.0"
