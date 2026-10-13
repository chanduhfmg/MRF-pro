"""GET /api/jobs/{id}/manifest - the numbers behind 'why zero rows'."""
from __future__ import annotations

from tests.test_search import TARGET_NPI, _run_search, app_ctx  # noqa: F401  (fixture)


def test_manifest_is_409_until_output_exists_then_returns_stats(app_ctx):
    client, _ = app_ctx
    s = _run_search(client, search_type="npi", value=TARGET_NPI)
    job = client.post(f"/api/searches/{s['id']}/extract", json={"top_n": 1}).json()["jobs"][0]

    # queued: nothing to read yet
    assert client.get(f"/api/jobs/{job['id']}/manifest").status_code == 409

    import app.worker as worker
    assert worker.process_one() is True

    r = client.get(f"/api/jobs/{job['id']}/manifest")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["job_id"] == job["id"]
    assert body["row_count"] == client.get(f"/api/jobs/{job['id']}").json()["record_count"]
    assert "extract_stats" in body and "enrich_stats" in body
    assert body["provider_count"] > 0

    assert client.get("/api/jobs/does-not-exist/manifest").status_code == 404
