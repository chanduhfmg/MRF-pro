"""
"Stop extraction": cancelling a queued job, a running job mid-pipeline, and
the additive column migration that made shipping this possible without
wiping the database.
"""
from __future__ import annotations

import pytest

from tests.test_search import TARGET_NPI, _run_search, app_ctx  # noqa: F401  (fixture)


def _queue_job(client) -> dict:
    s = _run_search(client, search_type="npi", value=TARGET_NPI)
    return client.post(f"/api/searches/{s['id']}/extract", json={"top_n": 1}).json()["jobs"][0]


def test_cancel_queued_job_removes_it_from_the_queue(app_ctx):
    client, _ = app_ctx
    job = _queue_job(client)
    assert job["status"] == "queued" and job["cancel_requested"] is False

    r = client.post(f"/api/jobs/{job['id']}/cancel")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "cancelled"
    assert r.json()["cancel_requested"] is True
    assert r.json()["has_output"] is False

    # the worker must not pick it up
    import app.worker as worker
    assert worker.process_one() is False

    # and stopping it again is a clean 409
    assert client.post(f"/api/jobs/{job['id']}/cancel").status_code == 409
    assert client.post("/api/jobs/nope/cancel").status_code == 404


def test_cancel_running_job_aborts_pipeline_and_discards_output(app_ctx, monkeypatch):
    """The flag lands mid-run: the reporter's next write raises JobCancelled,
    the worker records `cancelled` (not failed) and removes partial output."""
    client, _ = app_ctx
    job = _queue_job(client)

    from app import crud
    from app.database import session_scope
    import app.pipeline.stub as stub

    real_run_stub = stub.run_stub

    def run_stub_then_cancel(j, reporter, out_dir):
        # simulate the user clicking Stop just after the job started: set the
        # flag, then let the stub's next progress write discover it
        with session_scope() as db:
            crud.request_job_cancel(db, j.id)
        return real_run_stub(j, reporter, out_dir)

    monkeypatch.setattr(stub, "run_stub", run_stub_then_cancel)

    import app.worker as worker
    assert worker.process_one() is True

    d = client.get(f"/api/jobs/{job['id']}").json()
    assert d["status"] == "cancelled", d
    assert d["error_message"] is None, "a user stop is not a failure"
    assert d["has_output"] is False
    assert d["current_stage"] == "Stopped by user"

    # partial CSVs are gone (output_dir_for() mkdirs on call, so check the
    # path directly rather than through the helper)
    from app.config import settings
    out = settings.output_dir / job["id"]
    assert not out.exists() or not any(out.glob("*.csv")), list(out.glob("*"))

    st = client.get(f"/api/jobs/{job['id']}/status").json()
    assert st["status"] == "cancelled" and st["cancel_requested"] is True


def test_reporter_raises_job_cancelled_when_flag_is_set(app_ctx):
    client, _ = app_ctx
    job = _queue_job(client)

    from app import crud
    from app.database import session_scope
    from app.pipeline.context import JobCancelled, ProgressReporter

    with session_scope() as db:
        crud.get_job(db, job["id"]).status = crud.JobStatus.processing
    with session_scope() as db:
        crud.request_job_cancel(db, job["id"])

    rep = ProgressReporter(job_id=job["id"], total_bytes=100)
    with pytest.raises(JobCancelled):
        rep.set_stage("Extracting rates")  # forced write -> cancel check


def test_additive_migration_adds_missing_columns(tmp_path, monkeypatch):
    """A table created without a column gets it added, with its default, and
    existing rows survive - no more wiping the dev database on schema changes."""
    import importlib
    import sqlite3
    import sys

    db_file = tmp_path / "old.db"
    con = sqlite3.connect(db_file)
    # an 'old' extraction_jobs table lacking cancel_requested and target_codes
    con.execute(
        "create table extraction_jobs (id varchar(36) primary key, insurance_company varchar(100) not null, "
        "source_filename varchar(500) not null, source_file_path varchar(1000) not null, status varchar(20) not null, "
        "created_at datetime not null default current_timestamp, updated_at datetime not null default current_timestamp, "
        "bytes_processed bigint not null default 0, records_processed bigint not null default 0, retry_count int not null default 0, "
        "filter_to_target boolean not null default 0, downloaded_bytes bigint not null default 0)"
    )
    con.execute(
        "insert into extraction_jobs (id, insurance_company, source_filename, source_file_path, status) "
        "values ('old-1','uhc','f.json.gz','/x/f.json.gz','completed')"
    )
    con.commit()
    con.close()

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data"))
    for name in [n for n in list(sys.modules) if n == "app" or n.startswith("app.")]:
        del sys.modules[name]
    database = importlib.import_module("app.database")
    database.init_db()

    con = sqlite3.connect(db_file)
    cols = {r[1] for r in con.execute("pragma table_info(extraction_jobs)")}
    assert {"cancel_requested", "target_codes", "source_url", "eta_seconds_est"} <= cols
    row = con.execute("select id, cancel_requested, target_codes from extraction_jobs").fetchone()
    assert row == ("old-1", 0, None), "existing rows kept, new columns defaulted"
    con.close()
