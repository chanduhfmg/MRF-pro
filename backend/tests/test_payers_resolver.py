"""
Index resolution per payer, with the network mocked at head()/sniff_json()/
_fetch_json() - the three functions every resolver goes through.

Covers the three resolver kinds (templates, Cigna's lookup JSON, HealthSparq
fan-out), the TLS-block short-circuit, and the pin endpoint's verify-before-
save behaviour.
"""
from __future__ import annotations

import importlib
import json
import sys

import pytest

BLOCKED = "blocked: host refused the TLS connection (firewall)"


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'p.db').as_posix()}")
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("EXTRACTION_MODE", "stub")
    for name in [n for n in list(sys.modules) if n == "app" or n.startswith("app.")]:
        del sys.modules[name]
    database = importlib.import_module("app.database")
    database.init_db()
    main = importlib.import_module("app.main")
    payers = importlib.import_module("app.payers")
    # never touch the repo's real override file or index store from a test
    monkeypatch.setattr(payers, "OVERRIDE_FILE", tmp_path / "payer_sources.json")
    index_store = importlib.import_module("app.index_store")
    monkeypatch.setattr(index_store, "STORE_FILE", tmp_path / "payer_index.json")

    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        yield client, payers, monkeypatch


def test_healthsparq_candidates_follow_confirmed_pattern(ctx):
    _, payers, _ = ctx
    urls = payers.candidate_urls(payers.get_source("aetna"))
    assert urls, "aetna must produce candidates"
    for u in urls:
        assert u.startswith(
            "https://mrf.healthsparq.com/aetnacvs-egress.nophi.kyruushsq.com/prd/mrf/AETNACVS_I/"
        )
        assert "/tableOfContents/" in u and u.endswith("_index.json.gz")
    # both brands and every entity are represented, newest date first
    assert any("/ALICFI/" in u for u in urls) and any("/ALICSI/" in u for u in urls)
    assert any("Aetna-Life-Insurance-Company_index" in u for u in urls)
    dates = [u.split("/prd/mrf/AETNACVS_I/")[1].split("/")[1] for u in urls]
    assert dates == sorted(dates, reverse=True)

    mvp = payers.candidate_urls(payers.get_source("mvp"))
    assert all("/mvp-egress.nophi.kyruushsq.com/prd/mrf/MVP_I/MVP/" in u for u in mvp)


def test_blocked_host_short_circuits_and_is_reported(ctx):
    client, payers, mp = ctx
    calls = []

    def fake_head(url, timeout=12.0):
        calls.append(url)
        return False, None, BLOCKED

    mp.setattr(payers, "head", fake_head)
    r = payers.resolve_index_url("aetna", force=True)

    assert r["url"] is None
    assert r["blocked"] is True
    assert r["blocked_host"] == "mrf.healthsparq.com"
    assert "refused the connection" in r["error"]
    # dozens of candidates, ONE probe: the host answers them all the same way
    assert len(calls) == 1

    out = client.get("/api/payers/aetna?probe=true").json()
    assert out["blocked"] is True and out["resolved"] is False
    assert out["blocked_host"] == "mrf.healthsparq.com"


def test_cigna_lookup_json_yields_signed_toc(ctx):
    client, payers, mp = ctx
    signed = (
        "https://d25kgz5rikkq4n.cloudfront.net/cost_transparency/mrf/table-of-contents/"
        "reporting_month=2026-09/2026-09-01_cigna-health-life-insurance-company_index.json"
        "?Expires=1792159264&Signature=abc&Key-Pair-Id=K1"
    )
    lookup = {"mrfs": [{"kind": "TOC", "files": [{"url": signed, "file_size": "57.2 MB"}]}]}
    mp.setattr(payers, "_fetch_json", lambda url, timeout=20.0: lookup)
    mp.setattr(payers, "head", lambda url, timeout=12.0: (True, 59978079, None))
    mp.setattr(payers, "sniff_json", lambda url, timeout=20.0: (True, None))

    r = payers.resolve_index_url("cigna", force=True)
    assert r["url"] == signed
    assert r["size_bytes"] == 59978079
    assert r["blocked"] is False

    # probe=true asks for a live re-resolve; without it the endpoint reports
    # what the local store holds, which is what a search actually uses
    out = client.get("/api/payers/cigna?probe=true").json()
    assert out["resolved"] is True and out["index_url"] == signed


def test_cigna_lookup_failure_is_explained(ctx):
    _, payers, mp = ctx

    def boom(url, timeout=20.0):
        raise OSError("timed out")

    mp.setattr(payers, "_fetch_json", boom)
    r = payers.resolve_index_url("cigna", force=True)
    assert r["url"] is None
    assert r["error"].startswith("lookup failed")


def test_templates_resolver_rejects_html_shell(ctx):
    _, payers, mp = ctx
    mp.setattr(payers, "head", lambda url, timeout=12.0: (True, 577, None))
    mp.setattr(payers, "sniff_json", lambda url, timeout=20.0: (False, "served HTML, not JSON"))
    r = payers.resolve_index_url("anthem", force=True)
    assert r["url"] is None
    assert r["blocked"] is False
    assert all(t["error"] == "served HTML, not JSON" for t in r["tried"])


def test_pin_verifies_before_saving_and_unpin_restores(ctx):
    client, payers, mp = ctx

    # a URL that serves HTML must be refused, and nothing persisted
    mp.setattr(payers, "head", lambda url, timeout=12.0: (True, 10, None))
    mp.setattr(payers, "sniff_json", lambda url, timeout=20.0: (False, "served HTML, not JSON"))
    bad = client.post("/api/payers/anthem/index", json={"index_url": "https://x.test/shell"})
    assert bad.status_code == 422
    assert "did not serve a JSON index" in bad.json()["detail"]
    assert not payers.OVERRIDE_FILE.exists()

    # a blocked host is refused with the real reason
    mp.setattr(payers, "head", lambda url, timeout=12.0: (False, None, BLOCKED))
    blocked = client.post(
        "/api/payers/aetna/index",
        json={"index_url": "https://mrf.healthsparq.com/x/index.json.gz"},
    )
    assert blocked.status_code == 422
    assert "refused the connection" in blocked.json()["detail"]

    # a good one is saved, resolves, and reports pinned
    mp.setattr(payers, "head", lambda url, timeout=12.0: (True, 4096, None))
    mp.setattr(payers, "sniff_json", lambda url, timeout=20.0: (True, None))
    ok = client.post(
        "/api/payers/aetna/index", json={"index_url": "https://mirror.test/aetna_index.json.gz"}
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["pinned"] is True and body["resolved"] is True
    assert body["index_url"] == "https://mirror.test/aetna_index.json.gz"
    saved = json.loads(payers.OVERRIDE_FILE.read_text())
    assert saved["aetna"]["index_url"] == "https://mirror.test/aetna_index.json.gz"
    # pinned URL is the only candidate now
    assert payers.candidate_urls(payers.get_source("aetna")) == [
        "https://mirror.test/aetna_index.json.gz"
    ]

    gone = client.delete("/api/payers/aetna/index")
    assert gone.status_code == 200
    assert gone.json()["pinned"] is False
    assert "aetna" not in json.loads(payers.OVERRIDE_FILE.read_text())
    assert len(payers.candidate_urls(payers.get_source("aetna"))) > 1


def test_payer_without_probe_does_not_touch_network(ctx):
    client, payers, mp = ctx

    def explode(*a, **k):
        raise AssertionError("network must not be touched with probe=false")

    mp.setattr(payers, "head", explode)
    mp.setattr(payers, "_fetch_json", explode)
    r = client.get("/api/payers/cigna?probe=false")
    assert r.status_code == 200
    assert r.json()["resolved"] is False and r.json()["verified"] == "confirmed"
