"""
Regression: "Stop process" while files are still queued behind the
concurrency limit must end as `cancelled`, not `failed`.

The first live run of the feature hit exactly this: the pending futures were
.cancel()'d, `as_completed` still yielded them, and `.result()` raised
CancelledError - so a clean user stop was recorded as a crash.

Reproduced deterministically here: concurrency 1, two files to scan, and a
fake probe that flips the cancel flag on its first call. The second file's
future is therefore pending when the stop lands.
"""
from __future__ import annotations

from tests.test_search import TARGET_NPI, app_ctx  # noqa: F401  (fixture)


def test_stop_with_pending_files_ends_cancelled_and_keeps_matches(app_ctx, monkeypatch):
    client, _ = app_ctx

    from app import crud
    from app.config import settings
    from app.database import session_scope
    from app.provider_probe import ProbeResult
    import app.pipeline.search as sp

    # one probe at a time -> the second file is a pending future
    monkeypatch.setattr(settings, "search_concurrency", 1)

    r = client.post(
        "/api/searches",
        json={"insurance_company": "anthem", "search_type": "npi",
              "search_value": TARGET_NPI, "max_files": 2, "codes": ["99213"]},
    )
    sid = r.json()["id"]

    calls = {"n": 0}

    def fake_probe(location, search_type, value, **kw):
        calls["n"] += 1
        # first file: a hit - and the user clicks Stop right then
        with session_scope() as db:
            crud.request_search_cancel(db, sid)
        res = ProbeResult(location=location, matched=True, confidence="strong",
                          matched_tin="123456789", bytes_scanned=10, exhausted=False)
        res.provider_group_id = "42"
        return res

    monkeypatch.setattr(sp, "probe_file", fake_probe)

    import app.worker as worker
    assert worker.process_one_search() is True

    s = client.get(f"/api/searches/{sid}").json()
    assert s["status"] == "cancelled", s
    assert s["error_message"] is None, "a user stop must never read as a failure"
    assert s["files_matched"] == 1
    # the second file never ran: it was pending when the stop landed
    assert calls["n"] == 1
    assert s["files_scanned"] == 1
    assert s["files_to_scan"] == 2
    assert s["percent_complete"] < 100.0

    # and the match that was found is still there, extractable
    body = client.get(f"/api/searches/{sid}/matches").json()
    assert body["matched_count"] == 1
    assert body["matches"][0]["matched"] is True

    ex = client.post(f"/api/searches/{sid}/extract", json={"top_n": 1})
    assert ex.status_code == 200, ex.text
    assert ex.json()["created"] == 1
