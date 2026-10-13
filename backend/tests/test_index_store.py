"""
The local payer-index store: a search must not probe the network to find
its index, and a network outage must not make the feature vanish.
"""
from __future__ import annotations

import importlib
import json
import sys
import time

import pytest


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 's.db').as_posix()}")
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("EXTRACTION_MODE", "stub")
    for name in [n for n in list(sys.modules) if n == "app" or n.startswith("app.")]:
        del sys.modules[name]
    importlib.import_module("app.database").init_db()
    mod = importlib.import_module("app.index_store")
    monkeypatch.setattr(mod, "STORE_FILE", tmp_path / "payer_index.json")
    return mod


def test_signed_url_expiry_is_parsed(store):
    cf = ("https://d25kgz5rikkq4n.cloudfront.net/x/2026-09-01_cigna_index.json"
          "?Expires=1792159264&Signature=abc&Key-Pair-Id=K1")
    assert store.signed_expiry(cf) == 1792159264.0
    amz = "https://b.s3.amazonaws.com/f.json.gz?X-Amz-Date=20260901T000000Z&X-Amz-Expires=3600"
    assert store.signed_expiry(amz) == pytest.approx(1788220800.0 + 3600, abs=90 * 86400)
    assert store.signed_expiry("https://plain.example/index.json.gz") is None


def test_reporting_month_is_read_from_the_url(store):
    assert store.reporting_month_of(
        "https://x/anthem/2026-09-01_anthem_index.json.gz") == "2026-09"
    assert store.reporting_month_of(
        "https://x/reporting_month=2026-07/f.json") == "2026-07"
    assert store.reporting_month_of("https://x/index.json") is None


def test_seed_makes_the_store_usable_before_any_network_call(store):
    data = store.load()
    assert "anthem" in data and "uhc" in data
    e = store.get("anthem")
    assert e.url.endswith("_anthem_index.json.gz")
    assert e.source == "seed"
    # a seed has no resolved_at, so it reads as stale and will be refreshed -
    # but it is still returned when the network is unavailable
    assert store.staleness(e) is not None


def test_fresh_entry_is_returned_without_touching_the_network(store, monkeypatch):
    import app.payers as payers

    def explode(*a, **k):
        raise AssertionError("a fresh store entry must not hit the network")

    monkeypatch.setattr(payers, "resolve_index_url", explode)
    store.put("anthem", "https://x/anthem/2026-09-01_anthem_index.json.gz")

    r = store.get_index_url("anthem")
    assert r["url"].endswith("2026-09-01_anthem_index.json.gz")
    assert r["from_store"] is True and r["stale"] is False


def test_entry_older_than_the_refresh_window_is_refreshed(store, monkeypatch):
    import app.index_store as m

    store.put("anthem", "https://x/old.json.gz")
    raw = json.loads(m.STORE_FILE.read_text())
    raw["anthem"]["resolved_at"] = time.time() - 31 * 86400   # 31 days old
    m.STORE_FILE.write_text(json.dumps(raw))

    e = store.get("anthem")
    assert "older than" in store.staleness(e)

    monkeypatch.setattr(
        m, "resolve_index_url",
        lambda p, force=False: {"url": "https://x/new.json.gz", "size_bytes": 7},
        raising=False,
    )
    import app.payers as payers
    monkeypatch.setattr(
        payers, "resolve_index_url",
        lambda p, force=False: {"url": "https://x/new.json.gz", "size_bytes": 7},
    )
    r = store.get_index_url("anthem")
    assert r["url"] == "https://x/new.json.gz"
    assert r["stale"] is False
    assert store.get("anthem").url == "https://x/new.json.gz"   # persisted


def test_signed_url_near_expiry_is_refreshed_even_if_recent(store):
    soon = time.time() + 60          # inside the default 6h margin
    store.put("cigna", f"https://cf/x.json?Expires={int(soon)}&Signature=a")
    e = store.get("cigna")
    assert e.resolved_at > time.time() - 5      # just written, not old
    assert store.staleness(e) == "signature expiring"


def test_pinned_entries_never_age_out(store):
    import app.index_store as m

    store.put("aetna", "https://mirror.internal/aetna.json.gz", source="pinned")
    raw = json.loads(m.STORE_FILE.read_text())
    raw["aetna"]["resolved_at"] = time.time() - 400 * 86400
    m.STORE_FILE.write_text(json.dumps(raw))
    assert store.staleness(store.get("aetna")) is None


def test_network_failure_falls_back_to_the_stored_url(store, monkeypatch):
    """The outage case: a stale URL that might work beats no search at all."""
    import app.index_store as m
    import app.payers as payers

    store.put("anthem", "https://x/stored.json.gz")
    raw = json.loads(m.STORE_FILE.read_text())
    raw["anthem"]["resolved_at"] = time.time() - 60 * 86400
    m.STORE_FILE.write_text(json.dumps(raw))

    monkeypatch.setattr(
        payers, "resolve_index_url",
        lambda p, force=False: {"url": None, "error": "ConnectionRefusedError"},
    )
    r = store.get_index_url("anthem")
    assert r["url"] == "https://x/stored.json.gz"
    assert r["stale"] is True
    assert "ConnectionRefused" in r["error"]


def test_allow_network_false_never_resolves(store, monkeypatch):
    import app.payers as payers

    monkeypatch.setattr(
        payers, "resolve_index_url",
        lambda p, force=False: (_ for _ in ()).throw(AssertionError("no network allowed")),
    )
    r = store.get_index_url("cigna", allow_network=False)
    assert r["url"] is None or r["from_store"]


def test_refresh_all_keeps_fresh_and_reports_failures(store, monkeypatch):
    import app.payers as payers

    store.put("anthem", "https://x/fresh.json.gz")
    calls: list[str] = []

    def fake(p, force=False):
        calls.append(p)
        return {"url": None, "error": "ConnectionRefusedError"}

    monkeypatch.setattr(payers, "resolve_index_url", fake)
    results = store.refresh_all(only_stale=True)
    by = {r["payer"]: r for r in results}
    assert by["anthem"]["action"] == "kept"
    assert "anthem" not in calls, "a fresh entry must not be re-resolved"
    assert by["cigna"]["action"] == "failed"


def test_search_reads_the_store_and_never_probes_when_fresh(store, monkeypatch, tmp_path):
    """The whole point: a search with a fresh stored URL does no network work
    to find its index, so it is instant and survives an outage."""
    import app.payers as payers
    import app.pipeline.search as sp

    probes: list[str] = []

    def never(p, force=False):
        probes.append(p)
        return {"url": None, "error": "should not have been called"}

    monkeypatch.setattr(payers, "resolve_index_url", never)
    monkeypatch.setattr(sp, "resolve_index_url", never)

    store.put("anthem", "https://stored.example/anthem_index.json.gz")

    class Report:
        stages: list = []
        def stage(self, name, index_url=None): self.stages.append((name, index_url))
        def cancelled(self, force=False): return False
        def listing(self, **k): pass

    class Search:
        id = "s1"; insurance_company = "anthem"; search_type = "npi"
        search_value = "1234567893"; hint = None; files_to_scan = 1

    # the index step resolves from the store; listing then fails on the fake
    # URL, which is fine - we only care that no probe happened
    with pytest.raises(Exception):
        sp.run_search(Search(), Report())

    assert probes == [], f"a fresh stored URL must not trigger a probe, got {probes}"


def test_a_url_that_does_not_verify_is_not_stored(store, monkeypatch):
    """The blip case: during a network fault a captive portal answered with a
    4 KB page, the resolver accepted it, and the store cached a dead URL as
    good for a month. Verification at store time stops that."""
    import app.payers as payers

    monkeypatch.setattr(
        payers, "resolve_index_url",
        lambda p, force=False: {"url": "https://portal/login.html", "size_bytes": 4096},
    )
    monkeypatch.setattr(payers, "sniff_json", lambda u, timeout=20.0: (False, "served HTML, not JSON"))

    r = store.get_index_url("aetna")
    assert r["url"] is None
    assert "did not verify" in r["error"]
    assert store.get("aetna") is None, "junk must never reach the store"


def test_an_implausibly_small_index_is_rejected(store, monkeypatch):
    import app.payers as payers

    monkeypatch.setattr(
        payers, "resolve_index_url",
        lambda p, force=False: {"url": "https://x/tiny.json", "size_bytes": 4096},
    )
    monkeypatch.setattr(payers, "sniff_json", lambda u, timeout=20.0: (True, None))
    r = store.get_index_url("mvp")
    assert r["url"] is None
    assert "4096" in r["error"]


def test_unverified_or_tiny_existing_entries_read_as_stale(store):
    store.put("aetna", "https://x/a.json.gz", source="resolver", verified=False)
    assert store.staleness(store.get("aetna")) == "never verified"

    store.put("mvp", "https://x/m.json.gz", source="resolver",
              size_bytes=4096, verified=True)
    assert "implausibly small" in store.staleness(store.get("mvp"))

    store.put("cigna", "https://x/c.json.gz", source="resolver",
              size_bytes=60_000_000, verified=True)
    assert store.staleness(store.get("cigna")) is None


def test_invalidate_forces_a_re_resolve_next_time(store):
    store.put("anthem", "https://x/bad.json.gz", source="resolver",
              size_bytes=99_000_000, verified=True)
    assert store.staleness(store.get("anthem")) is None

    store.invalidate("anthem", "index could not be read: not gzip")
    e = store.get("anthem")
    assert e.verified is False
    assert store.staleness(e) is not None
    assert "invalidated" in (e.note or "")
