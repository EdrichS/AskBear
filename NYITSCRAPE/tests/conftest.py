"""Shared pytest setup for the AskBear scraper tests.

* OFFLINE by default: every socket connection and DNS lookup raises
  ``NetworkBlockedError`` (and fails the test), except in tests marked ``live``, which
  are skipped unless the environment variable ASKBEAR_LIVE=1 is set.
* Never touches the real ``data/`` folder: ``config.PROJECT_ROOT`` (the default data
  folder) points to a temporary folder during every test, and the tests build their own
  data folders in ``tmp_path``.
* ``python -m askbear_scraper ...`` reconfigures logging (``logging.basicConfig(force=True)``);
  the handlers it adds are removed again after each test.

Helpers (fake web server, fake clock, PDF and manifest builders) live in tests/testkit.py.
"""

from __future__ import annotations

import logging
import os
import socket

# progress bars only clutter the captured output (tqdm >= 4.66 reads TQDM_* variables
# when it is imported, so this must happen before the package is imported)
os.environ.setdefault("TQDM_DISABLE", "1")

import pytest  # noqa: E402

from askbear_scraper import config  # noqa: E402
from askbear_scraper.config import Settings  # noqa: E402

import testkit  # noqa: E402

LIVE_ENV = "ASKBEAR_LIVE"


# ---------------------------------------------------------------------------
# offline guard
# ---------------------------------------------------------------------------
class NetworkBlockedError(RuntimeError):
    """Raised when an offline test tries to open a connection or resolve a host name."""


_POLICY = {"allow": False}
_ATTEMPTS = []
_ORIGINALS = {}
_GUARDED = (
    (socket.socket, "connect"),
    (socket.socket, "connect_ex"),
    (socket, "create_connection"),
    (socket, "getaddrinfo"),
    (socket, "gethostbyname"),
    (socket, "gethostbyname_ex"),
)


def _guard(name, real):
    def guarded(*args, **kwargs):
        if _POLICY["allow"]:
            return real(*args, **kwargs)
        target = args[1] if name in ("connect", "connect_ex") and len(args) > 1 else (args[:1] or "")
        _ATTEMPTS.append(f"{name}{target!r}")
        raise NetworkBlockedError(
            f"offline test tried to use the network ({name} {target!r}); use testkit.FakeWeb, "
            f"or mark the test with @pytest.mark.live")
    guarded.__name__ = name
    return guarded


def pytest_configure(config):
    for owner, name in _GUARDED:
        real = getattr(owner, name)
        _ORIGINALS[(owner, name)] = real
        setattr(owner, name, _guard(name, real))


def pytest_unconfigure(config):
    for (owner, name), real in _ORIGINALS.items():
        setattr(owner, name, real)
    _ORIGINALS.clear()


def pytest_collection_modifyitems(config, items):
    if os.environ.get(LIVE_ENV) == "1":
        return
    skip = pytest.mark.skip(reason=f"live test (real NYIT websites): set {LIVE_ENV}=1 to run it")
    for item in items:
        if item.get_closest_marker("live"):
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _network_policy(request):
    """Tests marked ``live`` may use the network; any other test fails if it tries."""
    _POLICY["allow"] = request.node.get_closest_marker("live") is not None
    start = len(_ATTEMPTS)
    yield
    _POLICY["allow"] = False
    attempts = _ATTEMPTS[start:]
    if attempts:
        pytest.fail(f"offline test tried to use the network: {attempts[:3]}")


# ---------------------------------------------------------------------------
# isolation from the real project data and logging setup
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def _fake_project_root(tmp_path_factory):
    return tmp_path_factory.mktemp("project_root")


@pytest.fixture(autouse=True)
def _isolate_project_data(monkeypatch, _fake_project_root):
    """A test that forgets --data-dir / Settings(data_dir=...) writes to a temp folder,
    never to the real data/ folder of the project."""
    monkeypatch.setattr(config, "PROJECT_ROOT", _fake_project_root)


@pytest.fixture(autouse=True)
def _restore_logging():
    root = logging.getLogger()
    level = root.level
    yield
    # cli.main() -> utils.setup_logging() adds a StreamHandler (and maybe a FileHandler)
    for handler in list(root.handlers):
        if type(handler) in (logging.StreamHandler, logging.FileHandler):
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level)


# ---------------------------------------------------------------------------
# common fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def settings(tmp_path) -> Settings:
    """Settings for a fresh data folder in tmp_path (no jitter: delays are predictable)."""
    return Settings(data_dir=tmp_path / "data", delay=1.0, jitter=0.0, max_retries=2, backoff=2.0)


@pytest.fixture
def web() -> testkit.FakeWeb:
    return testkit.FakeWeb()


@pytest.fixture
def clock() -> testkit.FakeClock:
    return testkit.FakeClock()


@pytest.fixture
def fetcher(settings, web, clock):
    return testkit.make_fetcher(settings, web, clock)


@pytest.fixture(scope="session")
def parsed_dataset(tmp_path_factory):
    """A data folder built from every HTML fixture and parsed once with ``run_parse``
    (shared, read-only: used by the stats/validate/CLI tests)."""
    from askbear_scraper import pipeline

    settings = Settings(data_dir=tmp_path_factory.mktemp("parsed") / "data")
    testkit.build_fixture_crawl(settings)
    summary = pipeline.run_parse(settings)
    assert not summary.get("error"), summary
    return settings
