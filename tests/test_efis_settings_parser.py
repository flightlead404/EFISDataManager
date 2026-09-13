# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for Settings_Parser (efis_settings.py).

Property-based tests (Hypothesis, Properties 1-5) plus supporting fixtures.

Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 10.1, 10.2
"""

import hashlib
import os

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from efis_data_manager.efis_settings import (
    ParsedBackup,
    Settings_Parser,
    SettingsParseError,
)


# SID keys are numeric tokens; values are arbitrary ASCII without newlines or
# the special-key names. Avoid "=" collisions by testing split-on-first "="
# separately.
sid_keys = st.integers(min_value=1, max_value=99999).map(str)
sid_values = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    min_size=0,
    max_size=20,
).filter(lambda s: "\n" not in s and "\r" not in s)


def _write(tmp_path, text: str) -> str:
    p = tmp_path / "Settings.bak"
    p.write_text(text, encoding="ascii", errors="replace")
    return str(p)


def _sha256(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


# ---------------------------------------------------------------------------
# Property 1: Parse round-trip preserves SID map
# Feature: efis-settings-import, Property 1: Parse round-trip preserves SID map
# Validates: Requirements 1.1, 1.3
# ---------------------------------------------------------------------------
@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(pairs=st.dictionaries(sid_keys, sid_values, min_size=1, max_size=30))
def test_property1_parse_round_trip(tmp_path, pairs):
    # Exclude the special key names so the round-trip compares plain SIDs.
    for special in ("UPDATE", "CHECKSIZE", "CHECKSUM"):
        pairs.pop(special, None)
    if not pairs:
        pairs = {"151": "400"}
    text = "".join(f"{k}={v}\n" for k, v in pairs.items())
    path = _write(tmp_path, text)

    parsed = Settings_Parser.parse(path)

    for k, v in pairs.items():
        assert parsed.sids[k] == v


# ---------------------------------------------------------------------------
# Property 2: Parser tolerates garbage and never crashes
# Feature: efis-settings-import, Property 2: Parser tolerates garbage and never crashes
# Validates: Requirements 1.2, 10.2
# ---------------------------------------------------------------------------
garbage_lines = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    min_size=0,
    max_size=20,
).filter(lambda s: "=" not in s and "\n" not in s and "\r" not in s)


@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    pairs=st.dictionaries(sid_keys, sid_values, min_size=1, max_size=15),
    garbage=st.lists(garbage_lines, max_size=15),
)
def test_property2_garbage_tolerance(tmp_path, pairs, garbage):
    for special in ("UPDATE", "CHECKSIZE", "CHECKSUM"):
        pairs.pop(special, None)
    if not pairs:
        pairs = {"151": "400"}

    lines = [f"{k}={v}" for k, v in pairs.items()]
    lines += garbage
    lines += ["=orphanvalue", "   =spaces"]  # empty-key malformed lines
    # Deterministic interleave without importing random.
    text = "\n".join(lines) + "\n"
    path = _write(tmp_path, text)

    parsed = Settings_Parser.parse(path)  # must not raise

    for k, v in pairs.items():
        assert parsed.sids[k] == v
    # Missing special lines default to None.
    assert parsed.update_value is None
    assert parsed.checksize is None
    assert parsed.checksum is None


# ---------------------------------------------------------------------------
# Property 3: Parser is read-only on the source
# Feature: efis-settings-import, Property 3: Parser is read-only on the source
# Validates: Requirements 1.6, 10.1
# ---------------------------------------------------------------------------
@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(body=st.text(min_size=0, max_size=200))
def test_property3_read_only_source(tmp_path, body):
    path = _write(tmp_path, body)
    before = _sha256(path)

    try:
        Settings_Parser.parse(path)
    except SettingsParseError:
        pass  # failure path must also leave source untouched

    after = _sha256(path)
    assert before == after
    assert os.path.exists(path)


# ---------------------------------------------------------------------------
# Property 4: Empty-of-valid-pairs input fails cleanly
# Feature: efis-settings-import, Property 4: Empty-of-valid-pairs input fails cleanly
# Validates: Requirements 1.6
# ---------------------------------------------------------------------------
@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(garbage=st.lists(garbage_lines, min_size=0, max_size=20))
def test_property4_empty_of_valid_pairs_fails(tmp_path, garbage):
    # No line contains a valid KEY=VALUE (garbage_lines exclude "=", and we add
    # only empty-key lines which are also invalid).
    lines = list(garbage) + ["=x", "   =y"]
    text = "\n".join(lines) + "\n"
    path = _write(tmp_path, text)
    before = _sha256(path)

    with pytest.raises(SettingsParseError):
        Settings_Parser.parse(path)

    assert _sha256(path) == before


# ---------------------------------------------------------------------------
# Property 5: UPDATE / checksum extraction
# Feature: efis-settings-import, Property 5: UPDATE / checksum extraction
# Validates: Requirements 1.4, 1.5
# ---------------------------------------------------------------------------
@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    n=st.integers(min_value=-(10**9), max_value=10**9),
    s1=sid_values,
    s2=sid_values,
)
def test_property5_update_checksum_extraction(tmp_path, n, s1, s2):
    text = f"151=400\nUPDATE={n}\nCHECKSIZE={s1}\nCHECKSUM={s2}\n"
    path = _write(tmp_path, text)

    parsed = Settings_Parser.parse(path)

    assert parsed.update_value == n
    assert parsed.checksize == s1
    assert parsed.checksum == s2


# ---------------------------------------------------------------------------
# Supporting example tests
# ---------------------------------------------------------------------------
def test_split_on_first_equals(tmp_path):
    path = _write(tmp_path, "151=400=extra\n")
    parsed = Settings_Parser.parse(path)
    assert parsed.sids["151"] == "400=extra"


def test_oserror_raises_parse_error():
    with pytest.raises(SettingsParseError):
        Settings_Parser.parse("/nonexistent/path/Settings.bak")


def test_non_ascii_bytes_do_not_raise(tmp_path):
    p = tmp_path / "Settings.bak"
    p.write_bytes(b"151=400\n\xff\xfe=junk\n1063=A60670\n")
    parsed = Settings_Parser.parse(str(p))
    assert parsed.sids["151"] == "400"
    assert parsed.sids["1063"] == "A60670"
