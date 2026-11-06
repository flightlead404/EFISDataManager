# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property test for the identity-schema invariants (task 5.2).

Feature: chart-sync-wiring-fix, Property 7: identity schema invariants.

Design Section E — ``drive_updater._new_identity(mount_point, *,
chart_capable=..., nav_label_prefix=...)`` builds a fresh Identity_File dict.
The new schema drops the stale volume ``label`` field (R8.1): the Card_Manifest
is the single authoritative cycle/label record, so no ``label`` is duplicated
into identity. The dict keeps a durable uuid4 string as its ``id`` (R8.3) and
stores NO chart/plate cycle numbers (R8.4) — cycles live only in the
Card_Manifest. ``chart_capable`` and ``nav_label_prefix`` are labeling-only
fields that round-trip the passed values (R8.5 / R8.7 adjacent).

Property 7 asserts, across arbitrary ``chart_capable`` booleans, arbitrary
``nav_label_prefix`` strings, and varied mount-point basenames, that the
returned dict:

  * (R8.1) NEVER contains a ``label`` key;
  * (R8.3) always carries ``id`` as a valid uuid4 string — parseable by
    ``uuid.UUID(value, version=4)`` and exactly 36 characters;
  * (R8.4) NEVER contains any chart/plate cycle key (no ``charts_cycle``,
    ``plates_cycle``, or ``cycle`` key);
  * round-trips the passed ``chart_capable`` and ``nav_label_prefix`` values;
    and
  * carries ``kind == IDENTITY_KIND`` and a present ``schema_version``.

``_new_identity`` calls ``_volume_uuid`` (which shells out to ``diskutil``).
To keep each generated example fast and deterministic — and to avoid issuing
real ``diskutil`` calls against non-device temp paths — the property patches
``drive_updater._volume_uuid`` to a fixed stub via
``unittest.mock.patch.object`` used as a context manager inside the test body
(Hypothesis-safe, unlike a function-scoped pytest fixture).

Validates: Requirements 8.1, 8.3, 8.4
"""

import os
import uuid
from unittest.mock import patch

from hypothesis import given, settings, strategies as st

from efis_data_manager import drive_updater


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

# Arbitrary nav_label_prefix strings: the raw device-friendly-name input,
# which _new_identity stores verbatim (sanitization happens later at relabel
# time, not here). Includes empty, whitespace, punctuation, and unicode.
_NAV_LABEL_PREFIX = st.text(min_size=0, max_size=40)

# A filesystem-safe mount-point basename so the path varies per example.
_BASENAME = st.text(
    alphabet=st.characters(
        whitelist_categories=(),
        whitelist_characters="abcdefghijklmnopqrstuvwxyz0123456789" + "".join(("_", "-", " ")),
    ),
    min_size=1,
    max_size=20,
).map(lambda s: s.strip() or "VOL")

# The cycle-key names that MUST NEVER appear in the identity dict (R8.4).
_CYCLE_KEYS = ("charts_cycle", "plates_cycle", "cycle")


# ---------------------------------------------------------------------------
# Property
# ---------------------------------------------------------------------------

@settings(max_examples=200, deadline=None)
@given(
    chart_capable=st.booleans(),
    nav_label_prefix=_NAV_LABEL_PREFIX,
    basename=_BASENAME,
)
def test_identity_schema_invariants(chart_capable, nav_label_prefix, basename):
    """``_new_identity`` holds the schema invariants for any inputs.

    For arbitrary ``chart_capable`` booleans, ``nav_label_prefix`` strings, and
    mount-point basenames, the returned identity dict:

      * (R8.1) has no ``label`` key;
      * (R8.3) has a uuid4 ``id`` (36 chars, parseable with version=4);
      * (R8.4) has no chart/plate cycle key;
      * round-trips ``chart_capable`` and ``nav_label_prefix``; and
      * has ``kind == IDENTITY_KIND`` and a present ``schema_version``.

    Validates: Requirements 8.1, 8.3, 8.4
    """
    mount_point = os.path.join("/Volumes", basename)

    # Avoid a real (slow, flaky) diskutil call on a non-device path by stubbing
    # _volume_uuid for the duration of this example. A module-attribute swap via
    # patch.object as a context manager is Hypothesis-safe (re-applied per call).
    with patch.object(drive_updater, "_volume_uuid", return_value="STUB-UUID"):
        identity = drive_updater._new_identity(
            mount_point,
            chart_capable=chart_capable,
            nav_label_prefix=nav_label_prefix,
        )

    # (R8.1) NEVER a label key.
    assert "label" not in identity

    # (R8.3) id present and a valid uuid4 string, 36 chars.
    assert "id" in identity
    id_value = identity["id"]
    assert isinstance(id_value, str)
    assert len(id_value) == 36
    # Parseable as a version-4 UUID; the parsed canonical form matches the
    # stored string (no surprise reformatting / casing).
    parsed = uuid.UUID(id_value, version=4)
    assert str(parsed) == id_value
    assert parsed.version == 4

    # (R8.4) NEVER a chart/plate cycle key.
    for key in _CYCLE_KEYS:
        assert key not in identity

    # chart_capable round-trips the passed value exactly (R8.5 adjacent).
    assert identity["chart_capable"] is chart_capable

    # nav_label_prefix round-trips the passed value verbatim (R8.7 adjacent).
    assert identity["nav_label_prefix"] == nav_label_prefix

    # kind == IDENTITY_KIND and schema_version present.
    assert identity["kind"] == drive_updater.IDENTITY_KIND
    assert "schema_version" in identity
    assert identity["schema_version"] == drive_updater.IDENTITY_SCHEMA_VERSION
