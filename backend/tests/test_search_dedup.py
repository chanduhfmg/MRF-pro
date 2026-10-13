"""
A payer routinely republishes the exact same network under a second legal
entity (e.g. UHC's "Oxford Health Insurance Inc" and "Oxford Health Plans
LLC" filings of the identical "Metro Network 27", sizes differing by a few
dozen bytes out of ~400MB). Byte-scanning both wastes a download and an
extraction on data that's already been seen. find_duplicate_match() / the
matched_only filter in list_matches() is what stops a second such filing
from being offered as if it were a distinct file.
"""
from __future__ import annotations

import importlib
import sys

import pytest


@pytest.fixture()
def db_ctx(tmp_path, monkeypatch):
    db_file = tmp_path / "dedup.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data"))

    for name in [n for n in list(sys.modules) if n == "app" or n.startswith("app.")]:
        del sys.modules[name]

    database = importlib.import_module("app.database")
    database.init_db()
    crud = importlib.import_module("app.crud")

    with database.session_scope() as db:
        search = crud.create_search(
            db, insurance_company="uhc", search_type="tin",
            search_value="132665168", search_value_norm="132665168",
            files_to_scan=40,
        )
        search_id = search.id

    yield database, crud, search_id


def _record(crud, db, search_id, *, filename, size, tin, dup_of=None):
    return crud.record_match(
        db, search_id,
        location=f"https://payer.test/{filename}",
        filename=filename,
        reference_count=1,
        file_size_bytes=size,
        matched=True,
        confidence="strong",
        matched_tin=tin,
        npi_count=1,
        bytes_scanned=size,
        exhausted=True,
        budget_exceeded=False,
        duplicate_of_match_id=dup_of,
    )


def test_near_identical_size_same_tin_is_flagged_a_duplicate(db_ctx):
    database, crud, search_id = db_ctx
    with database.session_scope() as db:
        first = _record(
            crud, db, search_id,
            filename="Oxford-Health-Insurance-Metro-27.json.gz",
            size=401_553_491, tin="132665168",
        )
        # Real observed case: same network, second legal entity, 24 bytes
        # apart out of ~400MB.
        dup = crud.find_duplicate_match(
            db, search_id, matched_tin="132665168", file_size_bytes=401_553_515,
        )
        assert dup is not None
        assert dup.id == first.id


def test_different_tin_is_never_a_duplicate_regardless_of_size(db_ctx):
    database, crud, search_id = db_ctx
    with database.session_scope() as db:
        _record(
            crud, db, search_id,
            filename="Oxford-Health-Insurance-Metro-27.json.gz",
            size=401_553_491, tin="132665168",
        )
        dup = crud.find_duplicate_match(
            db, search_id, matched_tin="999999999", file_size_bytes=401_553_491,
        )
        assert dup is None


def test_meaningfully_different_size_is_not_a_duplicate(db_ctx):
    """Same TIN can legitimately be in two genuinely different files (a
    small regional plan and a large national one) - only near-identical
    size is evidence of a republished duplicate, not the TIN alone."""
    database, crud, search_id = db_ctx
    with database.session_scope() as db:
        _record(
            crud, db, search_id,
            filename="small-regional-plan.json.gz",
            size=5_000_000, tin="132665168",
        )
        dup = crud.find_duplicate_match(
            db, search_id, matched_tin="132665168", file_size_bytes=401_553_491,
        )
        assert dup is None


def test_matched_only_hides_duplicates_but_keeps_them_queryable(db_ctx):
    database, crud, search_id = db_ctx
    with database.session_scope() as db:
        first = _record(
            crud, db, search_id,
            filename="Oxford-Health-Insurance-Metro-27.json.gz",
            size=401_553_491, tin="132665168",
        )
        _record(
            crud, db, search_id,
            filename="Oxford-Health-Plans-Metro-27.json.gz",
            size=401_553_515, tin="132665168",
            dup_of=first.id,
        )

    with database.session_scope() as db:
        visible, total_visible = crud.list_matches(db, search_id, matched_only=True)
        assert [m.filename for m in visible] == ["Oxford-Health-Insurance-Metro-27.json.gz"]
        assert total_visible == 1

        everything, total_all = crud.list_matches(db, search_id, matched_only=False)
        assert total_all == 2
        assert {m.filename for m in everything} == {
            "Oxford-Health-Insurance-Metro-27.json.gz",
            "Oxford-Health-Plans-Metro-27.json.gz",
        }


def test_a_third_filing_points_at_the_original_not_the_second_copy(db_ctx):
    """A chain of 3+ republications must all point at the same canonical
    match, not form a chain (find_duplicate_match only searches among
    matches that are not themselves already flagged as a duplicate)."""
    database, crud, search_id = db_ctx
    with database.session_scope() as db:
        first = _record(
            crud, db, search_id,
            filename="filing-a.json.gz", size=401_553_491, tin="132665168",
        )
        dup2 = crud.find_duplicate_match(
            db, search_id, matched_tin="132665168", file_size_bytes=401_553_500,
        )
        assert dup2.id == first.id
        _record(
            crud, db, search_id, filename="filing-b.json.gz",
            size=401_553_500, tin="132665168", dup_of=dup2.id,
        )

        dup3 = crud.find_duplicate_match(
            db, search_id, matched_tin="132665168", file_size_bytes=401_553_480,
        )
        assert dup3.id == first.id  # not filing-b, which is already a duplicate
