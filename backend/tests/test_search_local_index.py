"""
When a payer's index host is blocked (Aetna/MVP on mrf.healthsparq.com), a
search must not just fail - if someone has hand-saved that payer's per-group
index.json(.gz) files into data/local_indexes/{payer}/, the search should
fold them into its candidate list and keep going instead.

No network: resolve_index_url is forced to report "blocked", and probe_file
is stubbed out, so this only exercises the local-file fallback path added to
run_search - not the byte-scanning probe itself (covered elsewhere).
"""
from __future__ import annotations

import gzip
import json
import ssl
import urllib.error

import pytest

from tests.test_index_store import store  # noqa: F401  (fixture)

TARGET_NPI = "1234567893"


def _write_local_index(path, *, sponsor: str, locations: list[str]) -> None:
    toc = {
        "last_updated_on": "2026-09-05",
        "reporting_entity_name": "Aetna Life Insurance Company",
        "reporting_entity_type": "Third Party Administrator",
        "reporting_structure": [
            {
                "in_network_files": [
                    {"description": "in-network file", "location": loc} for loc in locations
                ],
                "reporting_plans": [
                    {
                        "plan_name": f"{sponsor} Open Choice",
                        "issuer_name": "Aetna Life Insurance Company",
                        "plan_id_type": "ein",
                        "plan_id": "999999999",
                        "plan_sponsor_name": sponsor,
                        "plan_market_type": "group",
                    }
                ],
            }
        ],
        "version": "2.0.0",
    }
    path.write_bytes(gzip.compress(json.dumps(toc).encode()))


class _Report:
    def __init__(self):
        self.stages: list = []

    def stage(self, name, index_url=None):
        self.stages.append((name, index_url))

    def cancelled(self, force=False):
        return False

    def listing(self, **k):
        pass

    def scanning_started(self, **k):
        pass

    def scanning(self, **k):
        pass

    def match(self, entry, res, **kw):
        pass


class _Search:
    id = "s1"
    insurance_company = "aetna"
    search_type = "npi"
    search_value = TARGET_NPI
    hint = None
    files_to_scan = 5


def test_blocked_payer_falls_back_to_local_index_files(store, monkeypatch):  # noqa: F811
    import app.payers as payers
    import app.pipeline.search as sp

    def blocked(company, force=False):
        return {
            "url": None,
            "blocked": True,
            "blocked_host": "mrf.healthsparq.com",
            "label": "Aetna",
            "publisher_url": "https://health1.aetna.com/",
        }

    monkeypatch.setattr(payers, "resolve_index_url", blocked)
    monkeypatch.setattr(sp, "resolve_index_url", blocked)

    local_dir = payers.local_index_dir("aetna")
    local_dir.mkdir(parents=True, exist_ok=True)
    _write_local_index(
        local_dir / "2026-09-05_1-TRANSPARENCY_index.json.gz",
        sponsor="TRANSPARENCY",
        locations=["https://mrf.healthsparq.com/prd/mrf/a.json.gz"],
    )
    _write_local_index(
        local_dir / "2026-09-05_2-OTHERCO_index.json.gz",
        sponsor="OTHERCO",
        locations=["https://mrf.healthsparq.com/prd/mrf/b.json.gz"],
    )

    probed: list[str] = []

    def fake_probe(location, *a, **k):
        probed.append(location)
        from app.provider_probe import ProbeResult

        return ProbeResult(location=location, matched=False, exhausted=True)

    monkeypatch.setattr(sp, "probe_file", fake_probe)

    summary, cancelled = sp.run_search(_Search(), _Report())

    assert cancelled is False
    assert summary["files_listed"] == 2
    assert summary["files_scanned"] == 2
    assert sorted(probed) == [
        "https://mrf.healthsparq.com/prd/mrf/a.json.gz",
        "https://mrf.healthsparq.com/prd/mrf/b.json.gz",
    ]


def test_stored_url_that_fails_falls_back_to_local_files(store, monkeypatch):  # noqa: F811
    """The bug this was written for: a previous resolution already cached a
    (now-blocked) index URL, so run_search never even reaches the
    "nothing stored" branch - it tries the stored URL, that read fails, and
    it must fall back to local files instead of just discarding the URL and
    raising."""
    import app.mrf_index as mrf_index
    import app.net as net
    import app.payers as payers
    import app.pipeline.search as sp

    store.put("aetna", "https://mrf.healthsparq.com/prd/mrf/stale_index.json.gz")

    real_open_url = net.open_url

    def blocked_open(url, **k):
        if url.startswith("file://"):
            return real_open_url(url, **k)
        raise urllib.error.URLError(
            ssl.SSLError(1, "[SSL: TLSV1_ALERT_ACCESS_DENIED] tlsv1 alert access denied")
        )

    monkeypatch.setattr(net, "open_url", blocked_open)
    monkeypatch.setattr(mrf_index, "open_url", blocked_open)

    local_dir = payers.local_index_dir("aetna")
    local_dir.mkdir(parents=True, exist_ok=True)
    _write_local_index(
        local_dir / "2026-09-05_1-TRANSPARENCY_index.json.gz",
        sponsor="TRANSPARENCY",
        locations=["https://mrf.healthsparq.com/prd/mrf/a.json.gz"],
    )

    def fake_probe(location, *a, **k):
        from app.provider_probe import ProbeResult

        return ProbeResult(location=location, matched=False, exhausted=True)

    monkeypatch.setattr(sp, "probe_file", fake_probe)

    summary, cancelled = sp.run_search(_Search(), _Report())

    assert cancelled is False
    assert summary["files_listed"] == 1
    assert summary["files_scanned"] == 1
    assert "local" in summary["index_url"]

    # the poisoned stored URL must not be trusted again as-is: invalidate()
    # zeroes resolved_at so the next search re-resolves instead of reusing it
    assert store.get("aetna").resolved_at == 0.0


def test_blocked_payer_with_no_local_files_still_fails(store, monkeypatch):  # noqa: F811
    import app.payers as payers
    import app.pipeline.search as sp

    def blocked(company, force=False):
        return {
            "url": None,
            "blocked": True,
            "blocked_host": "mrf.healthsparq.com",
            "label": "Aetna",
            "publisher_url": "https://health1.aetna.com/",
        }

    monkeypatch.setattr(payers, "resolve_index_url", blocked)
    monkeypatch.setattr(sp, "resolve_index_url", blocked)

    with pytest.raises(sp.SearchFailed, match="firewall"):
        sp.run_search(_Search(), _Report())


def test_local_flat_json_array_is_also_understood(tmp_path):
    """Not every hand-saved local file is the CMS reporting_structure shape -
    someone may already have a flattened list of {location, description,
    plans, ...} entries (the shape this app itself would produce). Both
    shapes should merge together, deduplicated by location."""
    from app.mrf_index import iter_local_index_entries

    toc_path = tmp_path / "2026-10-01_MVP_index.json.gz"
    _write_local_index(
        toc_path, sponsor="MVP",
        locations=["https://mrf.healthsparq.com/prd/mrf/shared.json.gz"],
    )

    flat_path = tmp_path / "mvp_all_files.json"
    flat_path.write_text(json.dumps([
        {
            "description": "in-network file",
            # already present in the TOC file - must be deduplicated, not doubled
            "location": "https://mrf.healthsparq.com/prd/mrf/shared.json.gz",
            "plans": ["MVP EPO / PPO"],
            "source_toc": "MVP",
        },
        {
            "description": "in-network file",
            "location": "https://mrf.healthsparq.com/prd/mrf/only-in-flat-file.json.gz",
            "plans": ["MVP VT / VT Plus"],
            "source_toc": "MVP",
        },
        {
            # no "location" - must be skipped, not crash the whole file
            "description": "allowed amount file",
            "plans": ["MVP VT / VT Plus"],
        },
    ]), encoding="utf-8")

    entries = list(iter_local_index_entries([toc_path, flat_path]))
    locations = {e.location for e in entries}

    assert locations == {
        "https://mrf.healthsparq.com/prd/mrf/shared.json.gz",
        "https://mrf.healthsparq.com/prd/mrf/only-in-flat-file.json.gz",
    }
    flat_entry = next(e for e in entries if e.location.endswith("only-in-flat-file.json.gz"))
    assert flat_entry.plan_name == "MVP VT / VT Plus"
