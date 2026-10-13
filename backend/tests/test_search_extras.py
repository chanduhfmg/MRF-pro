"""
Pagination, "Stop process", custom CPT codes and saved code sets.

Reuses the stubbed-network fixture from test_search.py.
"""
from __future__ import annotations

import gzip
import json
import sys
import tempfile

from tests.test_search import TARGET_NPI, _run_search, app_ctx  # noqa: F401  (fixture)


def test_matches_are_paginated(app_ctx):
    client, _ = app_ctx
    s = _run_search(client, search_type="npi", value=TARGET_NPI)

    base = f"/api/searches/{s['id']}/matches?matched_only=false"
    p1 = client.get(f"{base}&limit=1&offset=0").json()
    p2 = client.get(f"{base}&limit=1&offset=1").json()

    assert p1["total"] == 2 and p2["total"] == 2
    assert p1["limit"] == 1 and p2["offset"] == 1
    assert len(p1["matches"]) == 1 and len(p2["matches"]) == 1
    assert p1["matches"][0]["id"] != p2["matches"][0]["id"]
    # the match sorts first regardless of page size
    assert p1["matches"][0]["matched"] is True
    # whole-search stats don't change with the page
    assert p1["matched_count"] == p2["matched_count"] == 1
    assert p1["scanned_count"] == p2["scanned_count"] == 2


def test_stop_process_on_a_queued_search_cancels_it_outright(app_ctx):
    client, _ = app_ctx
    r = client.post(
        "/api/searches",
        json={"insurance_company": "anthem", "search_type": "npi",
              "search_value": TARGET_NPI, "codes": ["99213"]},
    )
    sid = r.json()["id"]
    assert r.json()["status"] == "queued"

    c = client.post(f"/api/searches/{sid}/cancel")
    assert c.status_code == 200, c.text
    assert c.json()["status"] == "cancelled"
    assert c.json()["cancel_requested"] is True

    # the worker must not pick it up afterwards
    import app.worker as worker
    assert worker.process_one_search() is False

    # stopping twice is a clean 409, not a crash
    assert client.post(f"/api/searches/{sid}/cancel").status_code == 409


def test_stop_process_on_a_running_search_ends_it_as_cancelled(app_ctx):
    """Simulates the flag landing mid-run: set it before the worker claims the
    row, so run_search sees it on its first check and winds down."""
    client, _ = app_ctx
    r = client.post(
        "/api/searches",
        json={"insurance_company": "anthem", "search_type": "npi",
              "search_value": TARGET_NPI, "codes": ["99213"]},
    )
    sid = r.json()["id"]

    from app import crud
    from app.database import session_scope

    with session_scope() as db:
        crud.get_search(db, sid).cancel_requested = True  # flag only; stays queued

    import app.worker as worker
    assert worker.process_one_search() is True

    s = client.get(f"/api/searches/{sid}").json()
    assert s["status"] == "cancelled", s
    assert s["error_message"] is None, "a user stop is not an error"
    assert s["percent_complete"] < 100.0
    assert client.get(f"/api/searches/{sid}/status").json()["status"] == "cancelled"


def test_extract_with_explicit_codes_stores_cleaned_list(app_ctx):
    client, _ = app_ctx
    s = _run_search(client, search_type="npi", value=TARGET_NPI)
    r = client.post(
        f"/api/searches/{s['id']}/extract",
        json={"top_n": 1, "codes": [" 99213 ", "99213", "g0439, 99214"]},
    )
    assert r.status_code == 200, r.text
    job = r.json()["jobs"][0]
    # trimmed, upper-cased, de-duplicated, split on separators, order kept
    assert job["target_codes"] == ["99213", "G0439", "99214"]
    assert client.get(f"/api/jobs/{job['id']}").json()["target_codes"] == [
        "99213", "G0439", "99214",
    ]


def test_extract_without_codes_uses_default_set(app_ctx):
    client, _ = app_ctx
    s = _run_search(client, search_type="npi", value=TARGET_NPI)
    r = client.post(f"/api/searches/{s['id']}/extract", json={"top_n": 1})
    assert r.json()["jobs"][0]["target_codes"] is None


def test_extract_rejects_garbage_codes(app_ctx):
    client, _ = app_ctx
    s = _run_search(client, search_type="npi", value=TARGET_NPI)
    r = client.post(
        f"/api/searches/{s['id']}/extract",
        json={"top_n": 1, "codes": ["99213", "<script>"]},
    )
    assert r.status_code == 422


def test_code_set_crud_and_use_in_extraction(app_ctx):
    client, _ = app_ctx
    assert client.get("/api/code-sets").json() == []

    made = client.post(
        "/api/code-sets",
        json={"name": "Ophthalmology", "codes": ["92004", "92012", "92014", "92004"]},
    )
    assert made.status_code == 200, made.text
    cs = made.json()
    assert cs["codes"] == ["92004", "92012", "92014"]
    assert cs["code_count"] == 3

    # names are unique case-insensitively
    dup = client.post("/api/code-sets", json={"name": "ophthalmology", "codes": ["92004"]})
    assert dup.status_code == 409

    assert [c["name"] for c in client.get("/api/code-sets").json()] == ["Ophthalmology"]

    upd = client.put(
        f"/api/code-sets/{cs['id']}",
        json={"name": "Eye exams", "codes": ["92004", "92014"]},
    )
    assert upd.status_code == 200, upd.text
    assert upd.json()["name"] == "Eye exams"
    assert upd.json()["codes"] == ["92004", "92014"]

    # a saved set drives an extraction by id
    s = _run_search(client, search_type="npi", value=TARGET_NPI)
    r = client.post(
        f"/api/searches/{s['id']}/extract", json={"top_n": 1, "code_set_id": cs["id"]}
    )
    assert r.status_code == 200, r.text
    assert r.json()["jobs"][0]["target_codes"] == ["92004", "92014"]

    unknown = client.post(
        f"/api/searches/{s['id']}/extract", json={"top_n": 1, "code_set_id": "nope"}
    )
    assert unknown.status_code == 404

    assert client.delete(f"/api/code-sets/{cs['id']}").status_code == 200
    assert client.get("/api/code-sets").json() == []
    assert client.delete(f"/api/code-sets/{cs['id']}").status_code == 404


def test_code_set_rejects_invalid_codes(app_ctx):
    client, _ = app_ctx
    r = client.post("/api/code-sets", json={"name": "Bad", "codes": ["99213", "not a code!"]})
    assert r.status_code == 422


def test_custom_codes_replace_default_set_in_extraction():
    """The in_network pass matches only the codes the user asked for."""
    from app.config import REPO_ROOT

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    import extract_tic_all as ex

    assert ex.normalize_codes(None) is None
    assert ex.normalize_codes([]) is None
    assert ex.normalize_codes([" 99213", "g0439", "", "99213"]) == {"99213", "G0439"}

    # a tiny MRF: one item in the default set (99213), one outside it (G0439)
    doc = {
        "provider_references": [
            {
                "provider_group_id": 1,
                "provider_groups": [
                    {"npi": [1234567893], "tin": {"type": "ein", "value": "123456789"}}
                ],
            }
        ],
        "in_network": [
            {
                "billing_code": "99213", "billing_code_type": "CPT", "name": "Office visit",
                "negotiated_rates": [
                    {"provider_references": [1], "negotiated_prices": [{"negotiated_rate": 100}]}
                ],
            },
            {
                "billing_code": "G0439", "billing_code_type": "HCPCS", "name": "Wellness",
                "negotiated_rates": [
                    {"provider_references": [1], "negotiated_prices": [{"negotiated_rate": 50}]}
                ],
            },
        ],
    }
    with tempfile.TemporaryDirectory() as d:
        path = f"{d}/mini.json.gz"
        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump(doc, f)

        # default set: G0439 is not in it, so only 99213 comes out
        assert [r["billing_code"] for r in ex.iter_rate_rows(path)] == ["99213"]

        # a custom set REPLACES the default (and is case-insensitive)
        stats: dict = {}
        rows = list(ex.iter_rate_rows(path, target_codes=["g0439"], stats=stats))
        assert [r["billing_code"] for r in rows] == ["G0439"]
        assert stats["custom_codes"] is True
        assert stats["codes_requested"] == ["G0439"]

        rows = list(ex.iter_rate_rows(path, target_codes=["99213", "G0439"]))
        assert sorted(r["billing_code"] for r in rows) == ["99213", "G0439"]
