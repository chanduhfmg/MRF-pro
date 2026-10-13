"""
Tests for the NPI/TIN search -> pick files -> auto-extract flow.

No network: a fake payer TOC and two fake in-network files are served from
memory by monkeypatching `app.net.open_url`, which is the single choke point
every remote read goes through. That exercises the real index parser, the real
byte-scanning probe, the real worker claim path and the real job creation -
only the socket is fake.
"""
from __future__ import annotations

import gzip
import importlib
import io
import json
import sys

import pytest


# --- fixtures ---------------------------------------------------------------

TARGET_NPI = "1234567893"
OTHER_NPI = "1987654320"
TARGET_TIN = "123456789"

INDEX_URL = "https://payer.test/index.json"
FILE_HIT = "https://payer.test/in-network/hit.json.gz"
FILE_MISS = "https://payer.test/in-network/miss.json.gz"


def _toc() -> bytes:
    return json.dumps(
        {
            "reporting_entity_name": "Test Payer",
            "reporting_entity_type": "health insurance issuer",
            "reporting_structure": [
                {
                    "reporting_plans": [
                        {
                            "plan_name": "Choice Plus PPO",
                            "plan_id": "1234",
                            "plan_id_type": "EIN",
                            "plan_market_type": "group",
                        }
                    ],
                    "in_network_files": [
                        {"description": "in-network rates", "location": FILE_HIT},
                        {"description": "in-network rates", "location": FILE_MISS},
                    ],
                    "allowed_amount_file": {
                        "description": "allowed amounts",
                        "location": "https://payer.test/allowed.json.gz",
                    },
                }
            ],
        }
    ).encode()


def _in_network(npi: str, tin: str) -> bytes:
    body = json.dumps(
        {
            "reporting_entity_name": "Test Payer",
            "provider_references": [
                {
                    "provider_group_id": 42,
                    "provider_groups": [
                        {"npi": [int(npi), 1111111112],
                         "tin": {"type": "ein", "value": tin}}
                    ],
                }
            ],
            "in_network": [
                {
                    "billing_code": "99213",
                    "billing_code_type": "CPT",
                    "name": "Office visit",
                    "negotiation_arrangement": "ffs",
                    "negotiated_rates": [
                        {
                            "provider_references": [42],
                            "negotiated_prices": [
                                {
                                    "negotiated_type": "negotiated",
                                    "negotiated_rate": 123.45,
                                    "billing_class": "professional",
                                    "expiration_date": "9999-12-31",
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    ).encode()
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        gz.write(body)
    return buf.getvalue()


_BODIES = {
    INDEX_URL: _toc(),
    FILE_HIT: _in_network(TARGET_NPI, TARGET_TIN),
    FILE_MISS: _in_network(OTHER_NPI, "987654321"),
}


@pytest.fixture()
def app_ctx(tmp_path, monkeypatch):
    db_file = tmp_path / "search.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("EXTRACTION_MODE", "stub")
    monkeypatch.setenv("SEARCH_CONCURRENCY", "2")
    monkeypatch.setenv("SEARCH_MAX_FILES", "10")

    for name in [n for n in list(sys.modules) if n == "app" or n.startswith("app.")]:
        del sys.modules[name]

    database = importlib.import_module("app.database")
    database.init_db()
    main = importlib.import_module("app.main")

    # --- fake the network ---
    import app.net as net

    def fake_open_url(url, *, timeout=60.0, on_bytes=None):
        body = _BODIES.get(url)
        if body is None:
            raise OSError(f"unmapped test URL: {url}")
        raw = io.BytesIO(body)
        counter = net.CountingReader(raw, on_bytes=on_bytes)
        head = counter.read(2)
        counter.unread(head)
        stream = gzip.GzipFile(fileobj=counter) if head == net.GZIP_MAGIC else counter
        return stream, counter, len(body)

    monkeypatch.setattr(net, "open_url", fake_open_url)

    import app.mrf_index as mrf_index
    import app.provider_probe as probe
    import app.target_cpt_probe as target_cpt_probe
    monkeypatch.setattr(mrf_index, "open_url", fake_open_url)
    monkeypatch.setattr(probe, "open_url", fake_open_url)
    monkeypatch.setattr(target_cpt_probe, "open_url", fake_open_url)

    # --- fake index resolution ---
    import app.payers as payers
    import app.pipeline.search as search_pipeline

    def fake_resolve(key, *, force=False, probe_limit=None):
        return {
            "payer": key, "label": key, "url": INDEX_URL, "size_bytes": None,
            "tried": [], "error": None, "publisher_url": "", "verified": "test",
            "notes": "", "from_cache": False,
        }

    monkeypatch.setattr(payers, "resolve_index_url", fake_resolve)
    monkeypatch.setattr(search_pipeline, "resolve_index_url", fake_resolve)

    # Searches read the index URL from the local store, so point the store at
    # a temp file and seed every payer with the fake index. Writing it marks
    # the entry fresh, which is also what we want: no network during tests.
    import app.index_store as index_store

    monkeypatch.setattr(index_store, "STORE_FILE", tmp_path / "payer_index.json")
    for key in payers.PAYER_SOURCES:
        index_store.put(key, INDEX_URL, source="manual")

    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        yield client, tmp_path


def _run_search(client, *, search_type: str, value: str, codes: list[str] | None = None) -> dict:
    r = client.post(
        "/api/searches",
        json={
            "insurance_company": "anthem",
            "search_type": search_type,
            "search_value": value,
            "max_files": 10,
            # matches the billing_code baked into _in_network()'s fixture, so
            # a real TIN/NPI byte-scan hit also verifies as billing it.
            "codes": codes or ["99213"],
        },
    )
    assert r.status_code == 200, r.text
    search_id = r.json()["id"]

    import app.worker as worker

    assert worker.process_one_search() is True
    return client.get(f"/api/searches/{search_id}").json()


# --- tests ------------------------------------------------------------------

def test_npi_search_finds_only_the_file_containing_it(app_ctx):
    client, _ = app_ctx
    s = _run_search(client, search_type="npi", value=TARGET_NPI)

    assert s["status"] == "completed", s
    assert s["files_listed"] == 2, "the allowed-amount file must not be listed"
    assert s["files_scanned"] == 2
    assert s["files_matched"] == 1
    assert s["percent_complete"] == 100.0

    body = client.get(f"/api/searches/{s['id']}/matches").json()
    assert body["matched_count"] == 1
    assert body["scanned_count"] == 2
    hit = body["matches"][0]
    assert hit["location"] == FILE_HIT
    assert hit["matched"] is True
    # the probe's tier-2 confirm should have pulled provider context out
    assert hit["confidence"] == "strong"
    assert hit["provider_group_id"] == "42"
    assert hit["matched_tin"] == TARGET_TIN


def test_tin_search_matches_on_tax_id(app_ctx):
    client, _ = app_ctx
    s = _run_search(client, search_type="tin", value=TARGET_TIN)
    assert s["files_matched"] == 1

    body = client.get(f"/api/searches/{s['id']}/matches").json()
    assert body["matches"][0]["location"] == FILE_HIT


def test_absent_npi_matches_nothing(app_ctx):
    client, _ = app_ctx
    s = _run_search(client, search_type="npi", value="1999999998")
    assert s["status"] == "completed"
    assert s["files_scanned"] == 2
    assert s["files_matched"] == 0


def test_search_rejects_wrong_length_ids(app_ctx):
    client, _ = app_ctx
    bad_npi = client.post(
        "/api/searches",
        json={"insurance_company": "anthem", "search_type": "npi", "search_value": "12345"},
    )
    assert bad_npi.status_code == 422
    assert "10 digits" in bad_npi.json()["detail"]

    bad_tin = client.post(
        "/api/searches",
        json={"insurance_company": "anthem", "search_type": "tin",
              "search_value": "1234567890123"},
    )
    assert bad_tin.status_code == 422


def test_select_files_creates_queued_extraction_jobs(app_ctx):
    client, _ = app_ctx
    s = _run_search(client, search_type="npi", value=TARGET_NPI)

    r = client.post(
        f"/api/searches/{s['id']}/extract",
        json={"top_n": 1, "filter_to_target": True},
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["created"] == 1

    job = out["jobs"][0]
    assert job["status"] == "queued"
    assert job["source_url"] == FILE_HIT
    assert job["search_id"] == s["id"]
    assert job["target_type"] == "npi"
    assert job["target_value"] == TARGET_NPI
    assert job["filter_to_target"] is True
    # the estimate is what the UI shows before anything starts
    assert out["estimated_total_seconds"] is not None

    # the match now points back at its job, so re-selecting reuses it
    again = client.post(
        f"/api/searches/{s['id']}/extract", json={"top_n": 1}
    ).json()
    assert again["jobs"][0]["id"] == job["id"]

    listed = client.get(f"/api/searches/{s['id']}/jobs").json()
    assert [j["id"] for j in listed] == [job["id"]]


def test_extract_without_selection_is_rejected(app_ctx):
    client, _ = app_ctx
    s = _run_search(client, search_type="npi", value=TARGET_NPI)
    r = client.post(f"/api/searches/{s['id']}/extract", json={})
    assert r.status_code == 422


def test_search_status_poll_carries_eta_fields(app_ctx):
    client, _ = app_ctx
    s = _run_search(client, search_type="npi", value=TARGET_NPI)
    st = client.get(f"/api/searches/{s['id']}/status").json()
    assert st["status"] == "completed"
    assert st["percent"] == 100.0
    assert st["files_scanned"] == 2
    assert "eta_seconds" in st


def test_target_filter_narrows_extracted_rows():
    """The extraction-side filter keeps only the searched provider's groups."""
    from app.config import REPO_ROOT

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    import extract_tic_all as ex

    keep = ex.make_target_filter("npi", TARGET_NPI)
    assert keep("123456789", [TARGET_NPI, "1111111112"]) is True
    assert keep("123456789", ["1111111112"]) is False

    keep_tin = ex.make_target_filter("tin", TARGET_TIN)
    assert keep_tin(TARGET_TIN, ["1111111112"]) is True
    assert keep_tin("987654321", ["1111111112"]) is False

    # leading zeros must not change identity
    assert ex.make_target_filter("npi", "0001234567")("9", ["1234567"]) is True
    # no target => no filtering at all (the CLI's original behaviour)
    assert ex.make_target_filter(None, None) is None


def test_payers_endpoint_lists_hardcoded_sources(app_ctx):
    client, _ = app_ctx
    rows = client.get("/api/payers").json()
    keys = {r["key"] for r in rows}
    assert {"uhc", "aetna", "anthem", "cigna", "mvp"} == keys
    anthem = next(r for r in rows if r["key"] == "anthem")
    assert anthem["verified"] == "confirmed"
    assert anthem["publisher_url"].startswith("https://")


# --- UHC's blob-listing shape (not a TiC table of contents) ----------------

UHC_BLOBS_URL = "https://uhc.test/api/v1/uhc/blobs/"
UHC_HIT = "https://uhc.test/download/2026-09-01_Acme_PPO_in-network-rates.json.gz"
UHC_ALLOWED = "https://uhc.test/download/2026-09-01_Acme_allowed-amounts.json.gz"


def _uhc_blob_listing() -> bytes:
    return json.dumps(
        {
            "blobs": [
                {
                    "name": "2026-09-01_Acme-Corp_Insurer_PPO_in-network-rates.json.gz",
                    "downloadUrl": UHC_HIT,
                    "size": 38209124,
                },
                {
                    "name": "2026-09-01_Acme-Corp_allowed-amounts.json.gz",
                    "downloadUrl": UHC_ALLOWED,
                    "size": 172,
                },
                {
                    "name": "2026-09-01_Acme-Corp_index.json",
                    "downloadUrl": "https://uhc.test/download/idx.json",
                    "size": 2013,
                },
            ]
        }
    ).encode()


@pytest.fixture()
def uhc_ctx(tmp_path, monkeypatch):
    db_file = tmp_path / "uhc.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("EXTRACTION_MODE", "stub")

    for name in [n for n in list(sys.modules) if n == "app" or n.startswith("app.")]:
        del sys.modules[name]

    database = importlib.import_module("app.database")
    database.init_db()
    main = importlib.import_module("app.main")

    bodies = {
        UHC_BLOBS_URL: _uhc_blob_listing(),
        UHC_HIT: _in_network(TARGET_NPI, TARGET_TIN),
    }

    import app.net as net

    def fake_open_url(url, *, timeout=60.0, on_bytes=None):
        body = bodies.get(url)
        if body is None:
            raise OSError(f"unmapped test URL: {url}")
        raw = io.BytesIO(body)
        counter = net.CountingReader(raw, on_bytes=on_bytes)
        head = counter.read(2)
        counter.unread(head)
        stream = gzip.GzipFile(fileobj=counter) if head == net.GZIP_MAGIC else counter
        return stream, counter, len(body)

    import app.mrf_index as mrf_index
    import app.provider_probe as probe
    import app.target_cpt_probe as target_cpt_probe
    import app.payers as payers
    import app.pipeline.search as search_pipeline

    monkeypatch.setattr(net, "open_url", fake_open_url)
    monkeypatch.setattr(mrf_index, "open_url", fake_open_url)
    monkeypatch.setattr(probe, "open_url", fake_open_url)
    monkeypatch.setattr(target_cpt_probe, "open_url", fake_open_url)

    def fake_resolve(key, *, force=False, probe_limit=None):
        return {
            "payer": key, "label": key, "url": UHC_BLOBS_URL, "size_bytes": None,
            "tried": [], "error": None, "publisher_url": "", "verified": "test",
            "notes": "", "from_cache": False,
        }

    monkeypatch.setattr(payers, "resolve_index_url", fake_resolve)
    monkeypatch.setattr(search_pipeline, "resolve_index_url", fake_resolve)

    import app.index_store as index_store

    monkeypatch.setattr(index_store, "STORE_FILE", tmp_path / "payer_index.json")
    for key in payers.PAYER_SOURCES:
        index_store.put(key, UHC_BLOBS_URL, source="manual")

    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        yield client


def test_uhc_blob_listing_keeps_only_in_network_files(uhc_ctx):
    client = uhc_ctx
    r = client.post(
        "/api/searches",
        json={"insurance_company": "uhc", "search_type": "npi",
              "search_value": TARGET_NPI, "max_files": 10, "codes": ["99213"]},
    )
    assert r.status_code == 200, r.text
    sid = r.json()["id"]

    import app.worker as worker
    assert worker.process_one_search() is True

    s = client.get(f"/api/searches/{sid}").json()
    assert s["status"] == "completed", s
    # the allowed-amounts and per-employer index blobs must be filtered out
    assert s["files_listed"] == 1
    assert s["files_scanned"] == 1
    assert s["files_matched"] == 1

    body = client.get(f"/api/searches/{sid}/matches").json()
    hit = body["matches"][0]
    assert hit["location"] == UHC_HIT
    # UHC reports sizes inline, so the estimate is based on a real byte count
    assert hit["file_size_bytes"] == 38209124
    assert hit["estimated_seconds"] is not None
    assert "Acme Corp" in (hit["plan_name"] or "")
