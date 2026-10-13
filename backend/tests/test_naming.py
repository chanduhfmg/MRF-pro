"""Download filenames: short, but every token still says what the file is."""
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

from app.naming import download_basename, short_codeset, short_plan


def _job(**kw):
    base = dict(
        id="abcdef12-0000",
        insurance_company="uhc",
        source_filename="2026-09-01_Oxford-Health-Insurance--Inc-_Insurer_Metro-Network_27_in-network-rates.json.gz",
        plan_name=None,
        target_type=None,
        target_value=None,
        filter_to_target=False,
        target_codes=None,
        code_set_name=None,
        completed_at=datetime(2026, 9, 14, 12, 0, 0),
        created_at=datetime(2026, 9, 14, 11, 0, 0),
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_short_plan_drops_boilerplate_and_keeps_three_meaningful_words():
    assert short_plan("Oxford Health Insurance, Inc. Insurer Metro Network 27") == "Oxford-Metro-27"
    assert short_plan("UMR Inc Third Party Administrator CITY OF SIOUX FALLS DIRECT CONTRACT") == "UMR-CITY-SIOUX"
    assert short_plan("Cigna Health Life Insurance Company PathWell PPO") == "Cigna-Life-PathWell"
    # a leading reporting date is noise, not identity
    assert short_plan("2026-09-01_Acme-Corp_Insurer_PPO") == "Acme-PPO"
    # all-boilerplate names still produce something
    assert short_plan("Health Plan Inc") == "Health-Plan-Inc"
    assert short_plan("") == "file"
    assert len(short_plan("Averyveryveryverylongsinglewordplanname Something Else")) <= 26


def test_short_codeset():
    assert short_codeset(None, 0, is_default=True) == "default"
    assert short_codeset("E&M codes", 57, is_default=False) == "E-M-codes"
    assert short_codeset("Ophthalmology", 4, is_default=False) == "Ophthalmology"
    assert short_codeset(None, 14, is_default=False) == "custom14"


def test_download_basename_searched_provider_with_saved_set():
    j = _job(
        plan_name="Oxford Health Insurance  Inc Insurer Metro Network 27",
        target_type="npi", target_value="1013988054", filter_to_target=True,
        target_codes=json.dumps(["92004", "92012", "92014", "G0439"]),
        code_set_name="Ophthalmology",
    )
    assert download_basename(j, "xlsx") == "UHC_Oxford-Metro-27_NPI1013988054_20260914_Ophthalmology.xlsx"


def test_download_basename_all_providers_custom_codes_and_tin():
    j = _job(
        insurance_company="cigna",
        plan_name="Cigna Health Life Insurance Company PathWell PPO",
        target_type="tin", target_value="47-3943680", filter_to_target=False,
        target_codes=json.dumps(["99213"] * 3),
    )
    # not narrowed -> ALL instead of the id; id digits only when it is used
    assert download_basename(j, "zip") == "CIGNA_Cigna-Life-PathWell_ALL_20260914_custom3.zip"
    j.filter_to_target = True
    assert download_basename(j, "zip").split("_")[2] == "TIN473943680"


def test_download_basename_uploaded_file_uses_filename_and_default_set():
    j = _job()  # no plan, no target, default codes
    assert download_basename(j, "xlsx") == "UHC_Oxford-Metro-27_20260914_default.xlsx"


def test_download_basename_never_emits_unsafe_characters():
    j = _job(plan_name='We/ird "Plan" <Name>: ok?', code_set_name="a/b\\c")
    name = download_basename(j, "xlsx")
    assert all(ch.isalnum() or ch in "_-." for ch in name), name
