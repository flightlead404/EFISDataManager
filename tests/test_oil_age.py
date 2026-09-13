# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the oil-age warning feature.

Covers:
  - The config default (`oil_age_warning_hours`).
  - The pure `compute_oil_age` core: Properties 1-6 (Hypothesis) plus concrete
    example states.
  - The `get_max_operation_hourmeter` DB helper (NULL handling).
  - The `get_oil_age_status` wrapper: negative-config sanitization + cutoff
    passthrough.

`compute_oil_age` is the property-tested core (no DB, no config). The DB helper,
config sanitization, and cutoff SQL filter are validated by example/DB tests
because they exercise SQLite behavior that does not vary with generated input.
"""

import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from efis_data_manager import analysis, config, database
from efis_data_manager.analysis import compute_oil_age, get_oil_age_status


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    dbp = tmp_path / "efis_data.sqlite"
    monkeypatch.setattr(database, "DB_PATH", dbp)
    monkeypatch.setattr(database, "DB_DIR", tmp_path)
    conn = database.get_db_connection()
    conn.executescript(database.SCHEMA_SQL)
    conn.commit()
    conn.close()
    return dbp


def _insert_operation(op_id, hourmeter_end):
    conn = database.get_db_connection()
    conn.execute(
        "INSERT INTO operations (id, source_filename, start_time, end_time, "
        "duration_seconds, record_count, date, imported_at, hourmeter_end) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (op_id, f"t{op_id}.csv", f"2026-01-0{op_id}T00:00:00",
         f"2026-01-0{op_id}T00:02:00",
         120, 120, "2026-01-01", "2026-01-01T00:03:00", hourmeter_end))
    conn.commit()
    conn.close()


def _change(hourmeter, date="2026-01-01"):
    return {"event_type": "change", "date": date, "hourmeter": hourmeter}


def _addition(hourmeter, date="2026-01-01"):
    return {"event_type": "addition", "date": date, "hourmeter": hourmeter}


# ---------------------------------------------------------------------------
# 1.2 — Config default
# ---------------------------------------------------------------------------

def test_default_config_oil_age_is_zero():
    assert config.DEFAULT_CONFIG["oil_age_warning_hours"] == 0


def test_load_config_supplies_default_when_key_absent(tmp_path, monkeypatch):
    """A saved config lacking the key merges in the 0 default on load."""
    import json
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"tail_number": "N123AB"}))
    monkeypatch.setattr(config, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(config, "APP_SUPPORT_DIR", tmp_path)
    loaded = config.load_config()
    assert loaded["oil_age_warning_hours"] == 0


# ---------------------------------------------------------------------------
# 2.2 — get_max_operation_hourmeter NULL handling
# ---------------------------------------------------------------------------

def test_max_hourmeter_returns_max_ignoring_nulls(temp_db):
    _insert_operation(1, 100.0)
    _insert_operation(2, None)
    _insert_operation(3, 250.5)
    _insert_operation(4, None)
    assert database.get_max_operation_hourmeter() == 250.5


def test_max_hourmeter_none_when_no_rows(temp_db):
    assert database.get_max_operation_hourmeter() is None


def test_max_hourmeter_none_when_all_null(temp_db):
    _insert_operation(1, None)
    _insert_operation(2, None)
    assert database.get_max_operation_hourmeter() is None


# ---------------------------------------------------------------------------
# 3.2 — Property 1
# ---------------------------------------------------------------------------

# Feature: oil-age-warning, Property 1: Age is current hours minus the
# highest-hourmeter change.
@settings(max_examples=200)
@given(
    max_hm=st.floats(min_value=-1e6, max_value=1e6, allow_nan=False,
                     allow_infinity=False),
    hourmeters=st.lists(
        st.floats(min_value=-1e6, max_value=1e6, allow_nan=False,
                  allow_infinity=False),
        min_size=1, max_size=10),
    threshold=st.floats(min_value=0.001, max_value=1e6, allow_nan=False,
                        allow_infinity=False),
    extra_below=st.lists(
        st.floats(min_value=0.0, max_value=1e6, allow_nan=False,
                  allow_infinity=False),
        min_size=0, max_size=5),
)
def test_property_age_is_current_minus_highest_change(
        max_hm, hourmeters, threshold, extra_below):
    H = max(hourmeters)
    # Only exercise the positive-age branch this property is about.
    if not (max_hm - H > 0):
        return
    events = [_change(h) for h in hourmeters]
    result = compute_oil_age(max_hm, events, threshold)
    assert result["oil_hours_since_change"] is not None
    assert math.isclose(result["oil_hours_since_change"], max_hm - H,
                        rel_tol=1e-9, abs_tol=1e-6)

    # Adding further change events strictly below H does not change the result.
    more = events + [_change(H - abs(d) - 1.0) for d in extra_below]
    result2 = compute_oil_age(max_hm, more, threshold)
    assert result2["status"] == result["status"]
    assert result2["limit_exceeded"] == result["limit_exceeded"]
    assert math.isclose(result2["oil_hours_since_change"],
                        result["oil_hours_since_change"],
                        rel_tol=1e-9, abs_tol=1e-6)


# ---------------------------------------------------------------------------
# 3.3 — Property 2
# ---------------------------------------------------------------------------

# Feature: oil-age-warning, Property 2: Oil additions are ignored.
@settings(max_examples=200)
@given(
    max_hm=st.one_of(
        st.none(),
        st.floats(min_value=-1e6, max_value=1e6, allow_nan=False,
                  allow_infinity=False)),
    change_hms=st.lists(
        st.floats(min_value=-1e6, max_value=1e6, allow_nan=False,
                  allow_infinity=False),
        min_size=0, max_size=8),
    addition_hms=st.lists(
        st.floats(min_value=-1e6, max_value=1e6, allow_nan=False,
                  allow_infinity=False),
        min_size=0, max_size=8),
    other_type=st.sampled_from(["addition", "topup", "inspection", "misc"]),
    threshold=st.floats(min_value=0.0, max_value=1e6, allow_nan=False,
                        allow_infinity=False),
)
def test_property_additions_are_ignored(
        max_hm, change_hms, addition_hms, other_type, threshold):
    changes = [_change(h) for h in change_hms]
    additions = [{"event_type": other_type, "date": "2026-01-01",
                  "hourmeter": h} for h in addition_hms]
    # Interleave the non-change events among the change events.
    mixed = []
    for i in range(max(len(changes), len(additions))):
        if i < len(changes):
            mixed.append(changes[i])
        if i < len(additions):
            mixed.append(additions[i])

    with_additions = compute_oil_age(max_hm, mixed, threshold)
    changes_only = compute_oil_age(max_hm, changes, threshold)

    assert with_additions["status"] == changes_only["status"]
    assert with_additions["limit_exceeded"] == changes_only["limit_exceeded"]
    a = with_additions["oil_hours_since_change"]
    b = changes_only["oil_hours_since_change"]
    if a is None or b is None:
        assert a is b
    else:
        assert math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-6)


# ---------------------------------------------------------------------------
# 3.4 — Property 3
# ---------------------------------------------------------------------------

# Feature: oil-age-warning, Property 3: Missing-data states produce no number
# and no warning.
@settings(max_examples=200)
@given(
    threshold=st.floats(min_value=0.001, max_value=1e6, allow_nan=False,
                        allow_infinity=False),
    max_hm=st.floats(min_value=-1e6, max_value=1e6, allow_nan=False,
                     allow_infinity=False),
    addition_hms=st.lists(
        st.floats(min_value=-1e6, max_value=1e6, allow_nan=False,
                  allow_infinity=False),
        min_size=0, max_size=6),
    change_hms=st.lists(
        st.floats(min_value=-1e6, max_value=1e6, allow_nan=False,
                  allow_infinity=False),
        min_size=1, max_size=6),
)
def test_property_missing_data_states(threshold, max_hm, addition_hms,
                                      change_hms):
    # (a) No change events -> no_change_on_record.
    additions = [_addition(h) for h in addition_hms]
    res_no_change = compute_oil_age(max_hm, additions, threshold)
    assert res_no_change["status"] == "no_change_on_record"
    assert res_no_change["oil_hours_since_change"] is None
    assert res_no_change["limit_exceeded"] is False

    # (b) A change exists but no current engine hours -> not_computable.
    changes = [_change(h) for h in change_hms]
    res_not_comp = compute_oil_age(None, changes, threshold)
    assert res_not_comp["status"] == "not_computable"
    assert res_not_comp["oil_hours_since_change"] is None
    assert res_not_comp["limit_exceeded"] is False


# ---------------------------------------------------------------------------
# 3.5 — Property 4
# ---------------------------------------------------------------------------

# Feature: oil-age-warning, Property 4: A non-positive threshold disables the
# warning.
@settings(max_examples=200)
@given(
    max_hm=st.one_of(
        st.none(),
        st.floats(min_value=-1e6, max_value=1e6, allow_nan=False,
                  allow_infinity=False)),
    change_hms=st.lists(
        st.floats(min_value=-1e6, max_value=1e6, allow_nan=False,
                  allow_infinity=False),
        min_size=0, max_size=8),
    threshold=st.floats(min_value=-1e6, max_value=0.0, allow_nan=False,
                        allow_infinity=False),
)
def test_property_nonpositive_threshold_disables(max_hm, change_hms, threshold):
    events = [_change(h) for h in change_hms]
    result = compute_oil_age(max_hm, events, threshold)
    assert result["limit_exceeded"] is False
    assert result["status"] != "exceeded"


# ---------------------------------------------------------------------------
# 3.6 — Property 5
# ---------------------------------------------------------------------------

# Feature: oil-age-warning, Property 5: Exceeded iff hours strictly greater than
# threshold.
@settings(max_examples=200)
@given(
    H=st.floats(min_value=-1e5, max_value=1e5, allow_nan=False,
                allow_infinity=False),
    max_hm=st.floats(min_value=-1e5, max_value=1e5, allow_nan=False,
                     allow_infinity=False),
    eps=st.floats(min_value=0.01, max_value=100.0, allow_nan=False,
                  allow_infinity=False),
    position=st.sampled_from(["at", "below", "above"]),
    lower_hms=st.lists(
        st.floats(min_value=0.0, max_value=1e5, allow_nan=False,
                  allow_infinity=False),
        min_size=0, max_size=4),
)
def test_property_exceeded_boundary(H, max_hm, eps, position, lower_hms):
    # Compute the exact age from the chosen inputs, then place the threshold
    # relative to it so the boundary cases are exact (no float re-derivation).
    hours = max_hm - H
    if not (hours > 0):
        return  # positive-age branch only; negatives are Property 6

    if position == "at":
        threshold = hours          # exactly at the boundary
    elif position == "below":
        threshold = hours - eps    # hours strictly above threshold -> exceeded
    else:  # above
        threshold = hours + eps    # hours strictly below threshold -> ok
    if threshold <= 0:
        return  # keep threshold positive (disabled path is Property 4)

    events = [_change(H)] + [_change(H - abs(x) - 1.0) for x in lower_hms]
    result = compute_oil_age(max_hm, events, threshold)

    expected_exceeded = hours > threshold
    assert result["limit_exceeded"] is expected_exceeded
    assert result["status"] == ("exceeded" if expected_exceeded else "ok")
    if position == "at":
        # Exactly at the boundary is NOT exceeded.
        assert result["limit_exceeded"] is False
        assert result["status"] == "ok"


# ---------------------------------------------------------------------------
# 3.7 — Property 6
# ---------------------------------------------------------------------------

# Feature: oil-age-warning, Property 6: Negative age never raises a warning.
@settings(max_examples=200)
@given(
    H=st.floats(min_value=-1e5, max_value=1e5, allow_nan=False,
                allow_infinity=False),
    threshold=st.floats(min_value=1.0, max_value=1e5, allow_nan=False,
                        allow_infinity=False),
    delta=st.floats(min_value=0.0, max_value=1e5, allow_nan=False,
                    allow_infinity=False),
    lower_hms=st.lists(
        st.floats(min_value=0.0, max_value=1e5, allow_nan=False,
                  allow_infinity=False),
        min_size=0, max_size=4),
)
def test_property_negative_age_never_warns(H, threshold, delta, lower_hms):
    # max_hm <= H, so age = max_hm - H <= 0 (rolled-back / out-of-order).
    max_hm = H - delta
    events = [_change(H)] + [_change(H - abs(x) - 1.0) for x in lower_hms]
    result = compute_oil_age(max_hm, events, threshold)
    assert result["status"] == "ok"
    assert result["limit_exceeded"] is False


# ---------------------------------------------------------------------------
# 3.8 — Concrete example states
# ---------------------------------------------------------------------------

def test_example_disabled():
    r = compute_oil_age(1000.0, [_change(50.0)], 0)
    assert r["status"] == "ok"
    assert r["oil_hours_since_change"] is None
    assert r["limit_exceeded"] is False
    assert r["threshold"] == 0


def test_example_exceeded():
    r = compute_oil_age(100.0, [_change(50.0)], 45)
    assert r["status"] == "exceeded"
    assert math.isclose(r["oil_hours_since_change"], 50.0)
    assert r["limit_exceeded"] is True
    assert r["threshold"] == 45


def test_example_at_threshold():
    r = compute_oil_age(100.0, [_change(50.0)], 50)
    assert r["status"] == "ok"
    assert math.isclose(r["oil_hours_since_change"], 50.0)
    assert r["limit_exceeded"] is False


def test_example_no_change_on_record():
    r = compute_oil_age(100.0, [_addition(50.0)], 45)
    assert r["status"] == "no_change_on_record"
    assert r["oil_hours_since_change"] is None
    assert r["limit_exceeded"] is False


def test_example_not_computable():
    r = compute_oil_age(None, [_change(50.0)], 45)
    assert r["status"] == "not_computable"
    assert r["oil_hours_since_change"] is None
    assert r["limit_exceeded"] is False


def test_example_negative_age():
    r = compute_oil_age(40.0, [_change(50.0)], 5)
    assert r["status"] == "ok"
    assert math.isclose(r["oil_hours_since_change"], -10.0)
    assert r["limit_exceeded"] is False


def test_example_ignores_earlier_change():
    # Highest-hourmeter change wins even when listed out of order.
    r = compute_oil_age(100.0, [_change(50.0), _change(10.0), _change(30.0)], 40)
    assert math.isclose(r["oil_hours_since_change"], 50.0)
    assert r["status"] == "exceeded"


# ---------------------------------------------------------------------------
# 4.2 — get_oil_age_status: sanitization + cutoff passthrough
# ---------------------------------------------------------------------------

def _seed_oil_event(hourmeter, date, event_type="change"):
    conn = database.get_db_connection()
    conn.execute(
        "INSERT INTO oil_events (date, hourmeter, event_type, quarts_added, "
        "quarts_low, note, source, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (date, hourmeter, event_type, 0, 0, "", "manual",
         "2026-01-01T00:00:00"))
    conn.commit()
    conn.close()


def _patch_config(monkeypatch, **over):
    cfg = dict(config.DEFAULT_CONFIG)
    cfg.update(over)
    from efis_data_manager import config as config_mod
    monkeypatch.setattr(config_mod, "load_config", lambda: dict(cfg))


def test_status_negative_config_is_disabled(temp_db, monkeypatch):
    _insert_operation(1, 200.0)
    _seed_oil_event(50.0, "2026-01-01")
    _patch_config(monkeypatch, oil_age_warning_hours=-10)
    r = get_oil_age_status()
    assert r["status"] == "ok"
    assert r["limit_exceeded"] is False
    assert r["threshold"] == 0


def test_status_non_numeric_config_is_disabled(temp_db, monkeypatch):
    _insert_operation(1, 200.0)
    _seed_oil_event(50.0, "2026-01-01")
    _patch_config(monkeypatch, oil_age_warning_hours="banana")
    r = get_oil_age_status()
    assert r["status"] == "ok"
    assert r["threshold"] == 0


def test_status_cutoff_filters_earlier_changes(temp_db, monkeypatch):
    # Two changes: one before the cutoff (hm 50), one on/after (hm 120).
    _insert_operation(1, 200.0)
    _seed_oil_event(50.0, "2025-01-01")   # before cutoff
    _seed_oil_event(120.0, "2026-06-01")  # on/after cutoff
    _patch_config(monkeypatch, oil_age_warning_hours=45,
                  oil_cutoff_date="2026-01-01")
    r = get_oil_age_status()
    # Most-recent change among on/after-cutoff events is hm 120 -> age 80.
    assert math.isclose(r["oil_hours_since_change"], 80.0)
    assert r["status"] == "exceeded"


def test_status_empty_cutoff_includes_all(temp_db, monkeypatch):
    _insert_operation(1, 200.0)
    _seed_oil_event(50.0, "2025-01-01")
    _seed_oil_event(120.0, "2026-06-01")
    _patch_config(monkeypatch, oil_age_warning_hours=45, oil_cutoff_date="")
    r = get_oil_age_status()
    # All events included; highest hourmeter change is 120 -> age 80.
    assert math.isclose(r["oil_hours_since_change"], 80.0)
