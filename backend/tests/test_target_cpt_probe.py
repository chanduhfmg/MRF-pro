"""
The bug this module fixes: the old whole-file CPT check reported a code as
"matched" the instant it appeared anywhere in the file, for any provider -
so a TIN search would show a code as billed even when only some *other*
practice in the network bills it. scan_target_billing_codes() must resolve
each billing_code to its actual provider sub-group and only count a code
once that sub-group's TIN/NPI matches the search target.
"""
from __future__ import annotations

import gzip
import io
import json

from app.target_cpt_probe import scan_target_billing_codes

TARGET_TIN = "111111111"
TARGET_NPI = "1111111111"
OTHER_TIN = "222222222"
OTHER_NPI = "2222222222"


def _mrf_bytes() -> bytes:
    doc = {
        "reporting_entity_name": "Test Payer",
        "provider_references": [
            {"provider_group_id": 1,
             "provider_groups": [{"npi": [TARGET_NPI], "tin": {"value": TARGET_TIN}}]},
            {"provider_group_id": 2,
             "provider_groups": [{"npi": [OTHER_NPI], "tin": {"value": OTHER_TIN}}]},
        ],
        "in_network": [
            # billed only by the target group
            {"billing_code": "AAAAA", "billing_code_type": "CPT",
             "negotiated_rates": [{"provider_references": [1],
                                    "negotiated_prices": [{"negotiated_rate": 1}]}]},
            # billed by both groups
            {"billing_code": "BBBBB", "billing_code_type": "CPT",
             "negotiated_rates": [{"provider_references": [1, 2],
                                    "negotiated_prices": [{"negotiated_rate": 2}]}]},
            # billed only by the OTHER group - must never show up as "matched"
            # for a search scoped to the target
            {"billing_code": "CCCCC", "billing_code_type": "CPT",
             "negotiated_rates": [{"provider_references": [2],
                                    "negotiated_prices": [{"negotiated_rate": 3}]}]},
        ],
    }
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        gz.write(json.dumps(doc).encode())
    return buf.getvalue()


def test_scan_scopes_matches_to_target_tin_not_whole_file(tmp_path):
    dest = tmp_path / "file.json.gz"
    dest.write_bytes(_mrf_bytes())

    result = scan_target_billing_codes(
        "http://example.invalid/unused",  # dest already exists -> no download
        dest,
        ["AAAAA", "BBBBB", "CCCCC"],
        target_kind="tin",
        target_value=TARGET_TIN,
    )

    assert result.error == ""
    assert not result.cancelled
    # AAAAA and BBBBB are billed by the target TIN; CCCCC is billed only by
    # the other group and must not be reported just because it exists in the
    # file somewhere.
    assert result.matched_codes == ["AAAAA", "BBBBB"]
    # CCCCC never gets found under the target, so the whole in_network array
    # is read to the end looking for it - "exhausted" correctly means "fully
    # scanned", not "everything requested was found".
    assert result.exhausted is True
    # scratch download is always cleaned up, success or not
    assert not dest.exists()


def test_scan_scopes_matches_to_target_npi(tmp_path):
    dest = tmp_path / "file2.json.gz"
    dest.write_bytes(_mrf_bytes())

    result = scan_target_billing_codes(
        "http://example.invalid/unused",
        dest,
        ["AAAAA", "BBBBB", "CCCCC"],
        target_kind="npi",
        target_value=OTHER_NPI,
    )

    assert result.matched_codes == ["BBBBB", "CCCCC"]
    assert "AAAAA" not in result.matched_codes


def test_scan_stops_early_once_every_requested_code_is_found(tmp_path):
    dest = tmp_path / "file3.json.gz"
    dest.write_bytes(_mrf_bytes())

    result = scan_target_billing_codes(
        "http://example.invalid/unused",
        dest,
        ["AAAAA"],
        target_kind="tin",
        target_value=TARGET_TIN,
    )

    assert result.matched_codes == ["AAAAA"]
    assert result.exhausted is True


def test_scan_requires_a_target(tmp_path):
    dest = tmp_path / "file4.json.gz"
    dest.write_bytes(_mrf_bytes())

    result = scan_target_billing_codes(
        "http://example.invalid/unused", dest, ["AAAAA"],
        target_kind="", target_value="",
    )
    assert result.error
    assert result.matched_codes == []


def test_discovery_mode_finds_every_code_the_target_bills(tmp_path):
    """codes=None (Provider Lookup page's "Discover billed codes") - no
    fixed list, so it must find every code the target bills, not just some
    it was told to look for, and must still exclude codes billed only by
    another provider group."""
    dest = tmp_path / "file5.json.gz"
    dest.write_bytes(_mrf_bytes())

    result = scan_target_billing_codes(
        "http://example.invalid/unused", dest, None,
        target_kind="tin", target_value=TARGET_TIN,
    )

    assert result.error == ""
    assert result.discovery is True
    assert result.requested_codes == []  # nothing was requested - full discovery
    assert result.matched_codes == ["AAAAA", "BBBBB"]
    assert "CCCCC" not in result.matched_codes
    assert result.exhausted is True


def test_discovery_mode_for_the_other_target(tmp_path):
    dest = tmp_path / "file6.json.gz"
    dest.write_bytes(_mrf_bytes())

    result = scan_target_billing_codes(
        "http://example.invalid/unused", dest, None,
        target_kind="npi", target_value=OTHER_NPI,
    )

    assert result.matched_codes == ["BBBBB", "CCCCC"]
    assert "AAAAA" not in result.matched_codes


def test_discovery_mode_drops_a_corrupt_subgroup_same_as_extraction(tmp_path):
    """An NPI identical to its own sub-group's TIN is bad data - the whole
    sub-group is skipped, not just that one NPI (mirrors extract_tic_all.py's
    iter_rate_rows guard, which the lean discovery pass has to replicate
    itself since it doesn't call iter_rate_rows for this mode)."""
    corrupt_tin = "999999999"
    doc = {
        "reporting_entity_name": "Test Payer",
        "provider_references": [
            {"provider_group_id": 9,
             # the TIN value duplicated into the npi list - corrupt
             "provider_groups": [{"npi": [corrupt_tin], "tin": {"value": corrupt_tin}}]},
        ],
        "in_network": [
            {"billing_code": "ZZZZZ", "billing_code_type": "CPT",
             "negotiated_rates": [{"provider_references": [9],
                                    "negotiated_prices": [{"negotiated_rate": 1}]}]},
        ],
    }
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        gz.write(json.dumps(doc).encode())
    dest = tmp_path / "file7.json.gz"
    dest.write_bytes(buf.getvalue())

    result = scan_target_billing_codes(
        "http://example.invalid/unused", dest, None,
        target_kind="tin", target_value=corrupt_tin,
    )

    assert result.matched_codes == []


# Same TIN under TWO separate contracts (provider_group_id 10 and 20) in one
# file - each billing a different code. This is the case target_group_id
# exists for: "what does this TIN bill anywhere" over-reports what one
# specific contract bills.
_SHARED_TIN = "333333333"


def _two_contracts_same_tin_bytes() -> bytes:
    doc = {
        "reporting_entity_name": "Test Payer",
        "provider_references": [
            {"provider_group_id": 10,
             "provider_groups": [{"npi": ["3333333331"], "tin": {"value": _SHARED_TIN}}]},
            {"provider_group_id": 20,
             "provider_groups": [{"npi": ["3333333332"], "tin": {"value": _SHARED_TIN}}]},
        ],
        "in_network": [
            {"billing_code": "EEEEE", "billing_code_type": "CPT",
             "negotiated_rates": [{"provider_references": [10],
                                    "negotiated_prices": [{"negotiated_rate": 1}]}]},
            {"billing_code": "FFFFF", "billing_code_type": "CPT",
             "negotiated_rates": [{"provider_references": [20],
                                    "negotiated_prices": [{"negotiated_rate": 2}]}]},
        ],
    }
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        gz.write(json.dumps(doc).encode())
    return buf.getvalue()


def test_target_group_id_narrows_discovery_to_one_contract(tmp_path):
    dest = tmp_path / "contracts1.json.gz"
    dest.write_bytes(_two_contracts_same_tin_bytes())

    # Without a group filter: the TIN bills both codes, across its two
    # separate contracts.
    result = scan_target_billing_codes(
        "http://example.invalid/unused", dest, None,
        target_kind="tin", target_value=_SHARED_TIN,
    )
    assert result.matched_codes == ["EEEEE", "FFFFF"]

    dest2 = tmp_path / "contracts2.json.gz"
    dest2.write_bytes(_two_contracts_same_tin_bytes())

    # Narrowed to contract 10: only what THAT contract bills.
    result10 = scan_target_billing_codes(
        "http://example.invalid/unused", dest2, None,
        target_kind="tin", target_value=_SHARED_TIN, target_group_id="10",
    )
    assert result10.matched_codes == ["EEEEE"]

    dest3 = tmp_path / "contracts3.json.gz"
    dest3.write_bytes(_two_contracts_same_tin_bytes())

    # Narrowed to contract 20: only what THAT contract bills.
    result20 = scan_target_billing_codes(
        "http://example.invalid/unused", dest3, None,
        target_kind="tin", target_value=_SHARED_TIN, target_group_id="20",
    )
    assert result20.matched_codes == ["FFFFF"]


def test_target_group_id_narrows_specific_code_check_to_one_contract(tmp_path):
    dest = tmp_path / "contracts4.json.gz"
    dest.write_bytes(_two_contracts_same_tin_bytes())

    # Checking both codes but narrowed to contract 10 - FFFFF belongs to
    # contract 20, so it must not be reported even though the same TIN bills
    # it under the other contract.
    result = scan_target_billing_codes(
        "http://example.invalid/unused", dest, ["EEEEE", "FFFFF"],
        target_kind="tin", target_value=_SHARED_TIN, target_group_id="10",
    )
    assert result.matched_codes == ["EEEEE"]
