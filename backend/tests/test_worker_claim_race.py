"""
The bug this fixes: with two `python -m app.worker` processes running against
the same SQLite file (recommended so a long search doesn't block extraction
jobs - see worker.py's docstring), the old claim was a plain SELECT the
'queued' row then UPDATE it. SQLite has no row locking, so both processes
could SELECT the same row before either COMMITted, and both then ran the
identical search concurrently - independently probing files and writing
conflicting progress to the same row (its "N matched" bouncing between two
different numbers depending on which worker's write landed last), while
every OTHER queued item sat untouched because neither worker was ever free.

claim_next_job/claim_next_search now do a single atomic
`UPDATE ... WHERE status='queued'` gated by rowcount instead, which SQLite
serializes one COMMIT at a time - only one of any number of simultaneous
callers can ever win a given row, and this proves it under real thread
concurrency, not just two sequential calls.
"""
from __future__ import annotations

import importlib
import sys
import threading

import pytest


@pytest.fixture()
def db_ctx(tmp_path, monkeypatch):
    db_file = tmp_path / "claim_race.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data"))

    for name in [n for n in list(sys.modules) if n == "app" or n.startswith("app.")]:
        del sys.modules[name]

    database = importlib.import_module("app.database")
    database.init_db()
    crud = importlib.import_module("app.crud")

    yield database, crud


def test_two_workers_racing_for_one_search_only_one_wins(db_ctx):
    database, crud = db_ctx
    with database.session_scope() as db:
        search = crud.create_search(
            db, insurance_company="uhc", search_type="tin",
            search_value="123456789", search_value_norm="123456789",
            files_to_scan=10,
        )
        search_id = search.id

    barrier = threading.Barrier(8)
    winners: list = []
    lock = threading.Lock()

    def race(worker_id: str) -> None:
        barrier.wait()  # force every thread to hit the claim at the same instant
        with database.session_scope() as db:
            claimed = crud.claim_next_search(db, worker_id)
        if claimed is not None:
            with lock:
                winners.append((worker_id, claimed.id))

    threads = [threading.Thread(target=race, args=(f"worker-{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly one of the 8 simultaneous callers may claim the one queued
    # search - never zero (it must go to someone) and never more than one
    # (the bug this replaces).
    assert len(winners) == 1
    assert winners[0][1] == search_id

    with database.session_scope() as db:
        s = crud.get_search(db, search_id)
        assert s.status.value == "processing"
        assert s.worker_id == winners[0][0]


def test_two_workers_racing_for_one_job_only_one_wins(db_ctx, tmp_path):
    database, crud = db_ctx
    with database.session_scope() as db:
        job = crud.create_job(
            db, insurance_company="uhc",
            source_filename="in.json.gz",
            source_file_path=str(tmp_path / "in.json.gz"),
            total_bytes=1000,
        )
        job_id = job.id

    barrier = threading.Barrier(8)
    winners: list = []
    lock = threading.Lock()

    def race(worker_id: str) -> None:
        barrier.wait()
        with database.session_scope() as db:
            claimed = crud.claim_next_job(db, worker_id)
        if claimed is not None:
            with lock:
                winners.append((worker_id, claimed.id))

    threads = [threading.Thread(target=race, args=(f"worker-{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(winners) == 1
    assert winners[0][1] == job_id

    with database.session_scope() as db:
        j = crud.get_job(db, job_id)
        assert j.status.value == "processing"
        assert j.worker_id == winners[0][0]


def test_claiming_twice_in_a_row_the_second_call_gets_nothing(db_ctx):
    """Not a race - just confirms the guarded UPDATE actually flips the
    status, so a second sequential claim attempt finds nothing left."""
    database, crud = db_ctx
    with database.session_scope() as db:
        search = crud.create_search(
            db, insurance_company="uhc", search_type="npi",
            search_value="1234567893", search_value_norm="1234567893",
            files_to_scan=10,
        )
        search_id = search.id

    with database.session_scope() as db:
        first = crud.claim_next_search(db, "worker-a")
        assert first is not None and first.id == search_id

    with database.session_scope() as db:
        second = crud.claim_next_search(db, "worker-b")
        assert second is None
