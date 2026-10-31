# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Engine-level unit tests for the adopt content-handling matrix (task 12.2).

These exercise ``drive_updater.adopt_drive`` under its new content-action
signature (task 12.1)::

    adopt_drive(mount_point, *, chart_capable, nav_label_prefix,
                content_action, progress_callback=None)

One example per quadrant of the A/B/C/D content matrix, mapped to the five
``content_action`` values (design Section D.3):

  - A1 "manifest_from_card"  (chart-capable, keep existing data)
  - A2 "delete_then_fresh"   (chart-capable, erase existing data)
  - B  "fresh"               (chart-capable, no existing data)
  - C  "nav_only_delete"     (non-chart-capable, existing data, confirmed)
  - D  "nav_only"            (non-chart-capable, no existing data)

After every non-aborted case both provisioning files are present at the volume
root and ``resolve_drive_id`` returns the drive's id (R7.4). A1 additionally
proves the from-card manifest is built+saved and the normal diff sync runs and
converges (current local cycle on card, orphans pruned).

rsync runs against real temp dirs (skipped when rsync is absent, mirroring
``tests/test_card_manifest_write.py``); the mount preflight / watchdog and the
hardware-only diskutil steps are mocked, so no physical USB is needed.

Requirements: 6.1, 6.3, 6.4, 6.6, 6.9, 6.10, 7.4
"""

import os
import shutil

import pytest

from efis_data_manager import drive_updater as du
from efis_data_manager import manifest as m
from efis_data_manager.manifest import Manifest_Builder, Manifest_Store


pytestmark = pytest.mark.skipif(
    shutil.which("rsync") is None, reason="rsync not available on PATH"
)


# The current local cycle the populate brings a chart-capable drive toward.
LOCAL_CHARTS_CYCLE = "2611"
LOCAL_PLATES_CYCLE = "2611"


def _write(path, content=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)
    return path


def _rel_files(root):
    """Family-root-relative POSIX paths of every regular file under ``root``."""
    out = set()
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            abspath = os.path.join(dirpath, name)
            out.add(os.path.relpath(abspath, root).replace(os.sep, "/"))
    return out


# --- shared isolation fixtures (mirror tests/test_card_manifest_write.py) ----


@pytest.fixture(autouse=True)
def _identity_env(monkeypatch):
    """No real diskutil: identity captures no VolumeUUID/name, and the
    ``decide_streams`` label cross-check never shells out for a temp dir.

    ``wait_for_mount_ready`` is forced True and ``_volume_uuid`` to None exactly
    as the other identity tests do, so ``resolve_drive_id`` reaches the
    both-files gate without a readiness race or a UUID mismatch (R7.4).
    """
    monkeypatch.setattr(du, "_volume_uuid", lambda mp: None)
    monkeypatch.setattr(du, "_volume_name", lambda mp: None)
    monkeypatch.setattr(du, "wait_for_mount_ready", lambda *a, **k: True)
    # Temp "drives" aren't real OS mount points; make preflight/watchdog treat
    # them as present, writable mounts so the real rsync-based update runs.
    monkeypatch.setattr(du.os.path, "ismount", lambda p: True)
    # The post-sync relabel is a best-effort diskutil step; keep it a no-op so a
    # temp dir never shells out.
    monkeypatch.setattr(du, "_rename_volume", lambda mount, label: True)


@pytest.fixture
def state_paths(tmp_path, monkeypatch):
    """Isolate the durable sync-state file under a temp dir."""
    state_dir = tmp_path / "DataManagerLogs"
    state_dir.mkdir()
    monkeypatch.setattr(du, "SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setattr(du, "SYNC_STATE_PATH", str(state_dir / ".sync_state.json"))
    monkeypatch.setattr(
        du, "LEGACY_SYNC_MARKER_PATH", str(state_dir / ".sync_in_progress")
    )
    return state_dir


@pytest.fixture
def local_manifest_dir(tmp_path, monkeypatch):
    """Redirect the local manifest dir into tmp_path (never touch app-support)."""
    local = tmp_path / "manifests"
    monkeypatch.setattr(m, "LOCAL_MANIFEST_DIR", local)
    return local


def _make_local_image(tmp_path):
    """Build a minimal local USB image with all three families + payload.

    Returns the image dir. The scanned payload is ``ChartData/`` minus
    ``Plates/`` and the marker; the plates payload is ``ChartData/Plates/``;
    nav is the two root DBs.
    """
    image = tmp_path / "image"
    chart = image / "ChartData"
    plates = chart / "Plates"
    plates.mkdir(parents=True)
    # scanned payload + marker
    _write(str(chart / "SEC" / "tile1.png"), b"sec-tile")
    _write(str(chart / "LO" / "tile2.png"), b"lo-tile")
    _write(str(chart / "ScannedCharts.sqlite"), b"scanned-marker")
    # plates payload + marker
    _write(str(plates / "plate1.pdf"), b"plate")
    _write(str(plates / "Plates.sqlite"), b"plates-marker")
    # nav files
    _write(str(image / "NAV.DB"), b"nav-db")
    _write(str(image / "NAV-proc.DB"), b"nav-proc")
    return image


def _save_local_chart_manifests(image):
    """Save the SSD-side local manifests for the two chart families.

    ``build_jobs`` loads these (``Manifest_Store.load_local``) to diff against
    the card manifest. They must mirror the local image's payload exactly so the
    diff sync converges the card to the current local cycle. The scanned
    manifest is rooted at ``ChartData/`` with ``Plates/`` + the marker pruned
    (matching ``currency._build_family_manifest``); the plates manifest is
    rooted at ``ChartData/Plates/``.
    """
    chart_root = str(image / "ChartData")
    scanned = Manifest_Builder.build("scanned", chart_root, LOCAL_CHARTS_CYCLE)
    scanned.entries = {
        rel: e
        for rel, e in scanned.entries.items()
        if not rel.startswith("Plates/") and rel != "ScannedCharts.sqlite"
    }
    Manifest_Store.save_local(scanned)

    plates_root = str(image / "ChartData" / "Plates")
    plates = Manifest_Builder.build("plates", plates_root, LOCAL_PLATES_CYCLE)
    plates.entries = {
        rel: e for rel, e in plates.entries.items() if rel != "Plates.sqlite"
    }
    Manifest_Store.save_local(plates)


@pytest.fixture
def env(tmp_path, monkeypatch, state_paths, local_manifest_dir):
    """A local image + its SSD manifests, plus a drive dir to adopt.

    ``load_config`` points the populate source at the local image; the drive dir
    is created empty (each test seeds its own pre-adopt card content).
    """
    image = _make_local_image(tmp_path)
    _save_local_chart_manifests(image)
    drive = tmp_path / "drive"
    drive.mkdir()
    monkeypatch.setattr(du, "load_config", lambda: {"usb_image_path": str(image)})
    return {"image": image, "drive": drive, "mount": str(drive)}


def _assert_both_files_and_resolves(mount):
    """Both provisioning files present AND resolve_drive_id returns the id.

    This is the shared R7.4 post-adopt recognition assertion: identity present,
    a valid card manifest present, and ``resolve_drive_id`` returns the written
    id (the both-files recognition gate, R3.1).
    """
    identity = du.read_identity(mount)
    assert identity is not None, "identity file missing after adopt"
    assert identity["id"]
    assert Manifest_Store.load_card(mount) is not None, "card manifest missing"
    assert du.resolve_drive_id(mount) == identity["id"]


# --- Case A1: manifest_from_card (chart-capable, keep existing data) ----------


def test_adopt_a1_manifest_from_card_builds_manifest_then_diff_syncs(env):
    """A1: build the from-card manifest, save it, then run the normal diff sync.

    Seed the drive with an existing (stale) ChartData/ tree plus an orphan file
    absent from the local cycle. Adopt with ``manifest_from_card``: the from-card
    manifest is built+saved, the diff sync converges the card to the current
    local cycle, and the orphan is pruned via the files_from / removed path
    (R6.1, R6.3). Identity records chart_capable=True and both files resolve
    (R7.4).
    """
    drive = env["drive"]
    # Pre-existing stale ChartData/ on the card, including an orphan not present
    # in the local cycle (must be pruned) and a stale-content file (must be
    # rewritten to the local bytes).
    _write(str(drive / "ChartData" / "SEC" / "tile1.png"), b"STALE!!!")  # changed
    _write(str(drive / "ChartData" / "LO" / "orphan.png"), b"orphan")    # removed
    _write(str(drive / "ChartData" / "ScannedCharts.sqlite"), b"old-marker")
    _write(str(drive / "ChartData" / "Plates" / "plate1.pdf"), b"plate")
    _write(str(drive / "ChartData" / "Plates" / "Plates.sqlite"), b"old-pmarker")

    progress = []
    result = du.adopt_drive(
        env["mount"],
        chart_capable=True,
        nav_label_prefix="HXR",
        content_action="manifest_from_card",
        progress_callback=progress.append,
    )
    assert result["success"] is True, result["message"]

    # A from-card manifest was built (the hashing progress was surfaced) and
    # saved: EFIS_MANIFEST.json is present at the volume root.
    assert any("existing chart data" in msg.lower() for msg in progress)
    assert Manifest_Store.load_card(env["mount"]) is not None

    # The diff sync converged: the scanned payload equals the local cycle, the
    # stale file was rewritten, and the orphan was pruned.
    scanned_payload = {
        r
        for r in _rel_files(str(drive / "ChartData"))
        if not r.startswith("Plates/") and r != "ScannedCharts.sqlite"
    }
    assert scanned_payload == {"SEC/tile1.png", "LO/tile2.png"}
    assert (drive / "ChartData" / "SEC" / "tile1.png").read_bytes() == b"sec-tile"
    assert not (drive / "ChartData" / "LO" / "orphan.png").exists()  # pruned

    # The card manifest now records the current local charts cycle (rewritten by
    # _write_card_manifest_for_family on convergence).
    card = Manifest_Store.load_card(env["mount"])
    assert card.charts_cycle == LOCAL_CHARTS_CYCLE

    # Identity labeling snapshot is chart-capable, and both files resolve.
    assert du.read_identity(env["mount"])["chart_capable"] is True
    _assert_both_files_and_resolves(env["mount"])


# --- Case A2: delete_then_fresh (chart-capable, erase existing data) ----------


def test_adopt_a2_delete_then_fresh_deletes_chartdata_keeps_root(env):
    """A2: ChartData_Delete the pre-existing tree, then populate fresh.

    Seed an existing ChartData/ tree plus root-level survivors (NAV.DB,
    Settings.dat). Adopt with ``delete_then_fresh``: the pre-existing ChartData/
    is deleted then fresh-copied from the local image, the root survivors are
    untouched, both files resolve (R6.4, R7.4).
    """
    drive = env["drive"]
    # Pre-existing chart data to be erased, with a file that would be an orphan
    # if the delete did NOT run first.
    _write(str(drive / "ChartData" / "SEC" / "tile1.png"), b"STALE!!!")
    _write(str(drive / "ChartData" / "LO" / "orphan.png"), b"orphan")
    _write(str(drive / "ChartData" / "ScannedCharts.sqlite"), b"old-marker")
    # Root-level survivors outside ChartData/ that the scoped delete must keep.
    _write(str(drive / "NAV.DB"), b"pre-existing-nav")
    _write(str(drive / "Settings.dat"), b"user-settings")

    result = du.adopt_drive(
        env["mount"],
        chart_capable=True,
        nav_label_prefix="HXR",
        content_action="delete_then_fresh",
    )
    assert result["success"] is True, result["message"]

    # Fresh copy from local: the stale content is replaced and the orphan is
    # gone (the delete removed it before the fresh populate, which had no record
    # of it to re-create).
    assert (drive / "ChartData" / "SEC" / "tile1.png").read_bytes() == b"sec-tile"
    assert (drive / "ChartData" / "LO" / "tile2.png").read_bytes() == b"lo-tile"
    assert not (drive / "ChartData" / "LO" / "orphan.png").exists()

    # Root survivors untouched (nav is re-synced to the local content; Settings
    # is outside every family root and never touched).
    assert (drive / "Settings.dat").read_bytes() == b"user-settings"

    _assert_both_files_and_resolves(env["mount"])


# --- Case B: fresh (chart-capable, no existing data) --------------------------


def test_adopt_b_fresh_copies_all_from_local(env):
    """B: empty drive ChartData -> copy-all from local, both files resolve.

    The drive has an empty ChartData/ (no existing payload). Adopt with
    ``fresh``: the empty card manifest is saved, the populate diffs against empty
    so every local file is copied and nothing is pruned (R6.6). Both files
    resolve (R7.4).
    """
    drive = env["drive"]
    (drive / "ChartData").mkdir()  # present but empty: no Existing_Chart_Data

    result = du.adopt_drive(
        env["mount"],
        chart_capable=True,
        nav_label_prefix="HXR",
        content_action="fresh",
    )
    assert result["success"] is True, result["message"]

    # Copy-all: both chart families and nav landed on the drive.
    assert (drive / "ChartData" / "SEC" / "tile1.png").read_bytes() == b"sec-tile"
    assert (drive / "ChartData" / "LO" / "tile2.png").read_bytes() == b"lo-tile"
    assert (drive / "ChartData" / "Plates" / "plate1.pdf").read_bytes() == b"plate"
    assert (drive / "NAV.DB").read_bytes() == b"nav-db"

    assert du.read_identity(env["mount"])["chart_capable"] is True
    _assert_both_files_and_resolves(env["mount"])


# --- Case C: nav_only_delete (non-chart-capable, existing data, confirmed) ----


def test_adopt_c_nav_only_delete_erases_charts_syncs_nav(env):
    """C-confirmed: delete ChartData/, write empty manifest, nav-only populate.

    Seed an existing ChartData/ tree. Adopt with ``nav_only_delete`` and
    ``chart_capable=False``: ChartData/ is deleted, nav is synced, NO chart
    families are written (scanned/plates absent on the drive), identity records
    chart_capable=False, both files present (empty manifest + identity), and
    resolve returns the id (R6.9, R7.4).
    """
    drive = env["drive"]
    _write(str(drive / "ChartData" / "SEC" / "tile1.png"), b"charts")
    _write(str(drive / "ChartData" / "Plates" / "plate1.pdf"), b"plate")
    _write(str(drive / "ChartData" / "ScannedCharts.sqlite"), b"marker")

    result = du.adopt_drive(
        env["mount"],
        chart_capable=False,
        nav_label_prefix="MINIAP",
        content_action="nav_only_delete",
    )
    assert result["success"] is True, result["message"]

    # ChartData/ was deleted and NOT repopulated (nav-only populate withholds
    # the chart families, so a non-chart-capable drive never carries charts).
    assert not (drive / "ChartData").exists()

    # Nav was synced.
    assert (drive / "NAV.DB").read_bytes() == b"nav-db"
    assert (drive / "NAV-proc.DB").read_bytes() == b"nav-proc"

    # Identity labeling snapshot is non-chart-capable, both files resolve.
    assert du.read_identity(env["mount"])["chart_capable"] is False
    _assert_both_files_and_resolves(env["mount"])


# --- Case D: nav_only (non-chart-capable, no existing data) -------------------


def test_adopt_d_nav_only_syncs_nav_no_prompt(env):
    """D: empty manifest, nav-only populate, no delete.

    The drive has no existing chart data. Adopt with ``nav_only`` and
    ``chart_capable=False``: nav is synced only, no chart families are written,
    both files present, resolve returns the id (R6.10, R7.4).
    """
    drive = env["drive"]

    result = du.adopt_drive(
        env["mount"],
        chart_capable=False,
        nav_label_prefix="MINIAP",
        content_action="nav_only",
    )
    assert result["success"] is True, result["message"]

    # Nav synced; no chart families written.
    assert (drive / "NAV.DB").read_bytes() == b"nav-db"
    assert (drive / "NAV-proc.DB").read_bytes() == b"nav-proc"
    assert not (drive / "ChartData" / "SEC" / "tile1.png").exists()
    assert not (drive / "ChartData" / "Plates" / "plate1.pdf").exists()

    assert du.read_identity(env["mount"])["chart_capable"] is False
    _assert_both_files_and_resolves(env["mount"])


# =============================================================================
# Task 13.3 — end-to-end expansion of the adopt coverage.
#
# The five per-quadrant engine tests above (task 12.2) each assert both-files
# recognition (R7.4). The app-level Case-C-decline test (task 12.3) lives in
# tests/test_adopt_case_c_decline.py, and the resolve-then-prompt / cancel-abort
# device-selection tests (task 13.2) live at the app level. The tests below ADD
# complementary end-to-end coverage that ties the engine to the content matrix
# and corroborates the abort contract at the engine boundary, WITHOUT
# duplicating those existing tests:
#
#   - A1 convergence, stronger: the from-card manifest built at adopt time
#     matches a fresh Manifest_Builder over the card tree exactly, and after the
#     diff sync the card manifest records the current local cycle. This proves
#     build-from-card THEN diff-sync end to end (R6.1).
#   - device-selection -> content-action mapping at the engine boundary: the five
#     content_action values collapse to the four distinct on-disk outcomes
#     (chart families present vs nav-only) (R6.1, R7.4).
#   - an invalid content_action raises ValueError (the task 12.1 defensive guard).
#   - R5.4 / R6.8 abort corroboration at the engine level: the engine is only
#     ever entered with a valid content_action (the ValueError guard is the
#     engine's half of the contract); the "app never calls adopt_drive on an
#     abort" half is covered by the app-level decline/cancel tests (12.3 / 13.2).
#
# Requirements: 5.4, 6.1, 6.8, 7.4
# =============================================================================


def _build_card_family_entries(chart_root):
    """A fresh from-card build of the two chart families over ``chart_root``.

    Mirrors ``_card_manifest_from_card`` / ``currency._build_family_manifest``:
    the ``scanned`` family is rooted at ``ChartData/`` with the ``Plates/``
    subtree and the ``ScannedCharts.sqlite`` marker pruned; the ``plates`` family
    is rooted at ``ChartData/Plates/`` with its ``Plates.sqlite`` marker pruned.
    Both cycles are null on a from-card build. Returns ``{family: entries}``.
    """
    scanned = Manifest_Builder.build("scanned", chart_root, None)
    scanned_entries = {
        rel: e
        for rel, e in scanned.entries.items()
        if not rel.startswith("Plates/") and rel != "ScannedCharts.sqlite"
    }
    plates_root = os.path.join(chart_root, "Plates")
    plates = Manifest_Builder.build("plates", plates_root, None)
    plates_entries = {
        rel: e for rel, e in plates.entries.items() if rel != "Plates.sqlite"
    }
    return {"scanned": scanned_entries, "plates": plates_entries}


def _seed_stale_card_chartdata(drive):
    """Seed an existing (stale) ChartData/ tree with an orphan to be pruned."""
    _write(str(drive / "ChartData" / "SEC" / "tile1.png"), b"STALE!!!")  # changed
    _write(str(drive / "ChartData" / "LO" / "orphan.png"), b"orphan")    # removed
    _write(str(drive / "ChartData" / "ScannedCharts.sqlite"), b"old-marker")
    _write(str(drive / "ChartData" / "Plates" / "plate1.pdf"), b"plate")
    _write(str(drive / "ChartData" / "Plates" / "Plates.sqlite"), b"old-pmarker")


# --- A1 end-to-end: build-from-card THEN diff-sync convergence ---------------


def test_adopt_a1_from_card_manifest_matches_fresh_build_then_converges(env):
    """A1 end-to-end: from-card manifest == fresh build of the card BEFORE sync;
    after the sync the card manifest records the current local cycle.

    This is a stronger convergence assertion than the per-quadrant A1 test
    above. We capture what a fresh ``Manifest_Builder`` sees on the card tree
    BEFORE adopt, build the from-card manifest the same way ``adopt_drive`` does
    (via the actual adopt run), and prove two things end to end:

      1. The manifest adopt builds from the card is exactly a truthful snapshot
         of the card's own ``ChartData/`` at adopt time (null cycles, pruned
         markers / Plates subtree), i.e. build-from-card is faithful.
      2. After the normal diff sync runs on top of that from-card baseline, the
         card manifest records the CURRENT local charts/plates cycle — the diff
         sync converged the card forward from the from-card baseline.

    Requirements: 6.1
    """
    drive = env["drive"]
    _seed_stale_card_chartdata(drive)

    # What a fresh from-card build sees on the card tree BEFORE the adopt runs.
    expected_from_card = _build_card_family_entries(str(drive / "ChartData"))
    # Sanity: the stale card really does carry the orphan we expect pruned later,
    # so the "converged forward" assertion below is meaningful.
    assert "LO/orphan.png" in expected_from_card["scanned"]

    # Intercept the from-card manifest adopt builds+saves so we can compare it to
    # the fresh build above. _card_manifest_from_card is the exact helper
    # adopt_drive calls for the manifest_from_card action.
    built = {}
    real_from_card = du._card_manifest_from_card

    def _spy_from_card(mount, progress_callback=None):
        cm = real_from_card(mount, progress_callback=progress_callback)
        built["card"] = cm
        return cm

    import unittest.mock as _mock

    with _mock.patch.object(du, "_card_manifest_from_card", _spy_from_card):
        result = du.adopt_drive(
            env["mount"],
            chart_capable=True,
            nav_label_prefix="HXR",
            content_action="manifest_from_card",
        )
    assert result["success"] is True, result["message"]

    # (1) The from-card manifest adopt built is a faithful snapshot of the card
    # tree as it stood BEFORE the sync: same entries per family, null cycles.
    from_card_cm = built["card"]
    assert from_card_cm.charts_cycle is None
    assert from_card_cm.plates_cycle is None
    assert (
        from_card_cm.families["scanned"].entries.keys()
        == expected_from_card["scanned"].keys()
    )
    assert (
        from_card_cm.families["plates"].entries.keys()
        == expected_from_card["plates"].keys()
    )
    for rel, entry in expected_from_card["scanned"].items():
        assert from_card_cm.families["scanned"].entries[rel].sha256 == entry.sha256
    for rel, entry in expected_from_card["plates"].items():
        assert from_card_cm.families["plates"].entries[rel].sha256 == entry.sha256

    # (2) After the diff sync, the on-disk card manifest records the CURRENT
    # local cycle (converged forward from the null-cycle from-card baseline), and
    # the orphan present in the from-card baseline was pruned on disk.
    card = Manifest_Store.load_card(env["mount"])
    assert card.charts_cycle == LOCAL_CHARTS_CYCLE
    assert card.plates_cycle == LOCAL_PLATES_CYCLE
    assert not (drive / "ChartData" / "LO" / "orphan.png").exists()
    _assert_both_files_and_resolves(env["mount"])


# --- content_action -> on-disk outcome mapping at the engine boundary ---------

# (content_action, chart_capable, expects_chart_families_on_drive). The five
# actions collapse to two on-disk shapes: chart-capable actions leave the chart
# families on the drive; nav-only actions leave NONE. All five leave nav + both
# provisioning files (R7.4).
_ACTION_OUTCOME_CASES = [
    ("manifest_from_card", True, True),
    ("delete_then_fresh", True, True),
    ("fresh", True, True),
    ("nav_only_delete", False, False),
    ("nav_only", False, False),
]

# Chart-family marker files that MUST be present iff the action is chart-capable.
_CHART_FAMILY_MARKERS = [
    ("ChartData", "SEC", "tile1.png"),
    ("ChartData", "LO", "tile2.png"),
    ("ChartData", "Plates", "plate1.pdf"),
]


@pytest.mark.parametrize(
    "content_action,chart_capable,expect_charts",
    _ACTION_OUTCOME_CASES,
    ids=[c[0] for c in _ACTION_OUTCOME_CASES],
)
def test_adopt_content_action_maps_to_ondisk_outcome(
    env, content_action, chart_capable, expect_charts
):
    """The five content_action values produce the four distinct on-disk outcomes.

    Table-driven over every action. Chart-capable actions
    (manifest_from_card / delete_then_fresh / fresh) leave the chart families on
    the drive; nav-only actions (nav_only_delete / nav_only) leave NO chart
    families. Every action syncs nav and leaves both provisioning files so the
    drive resolves (R7.4). This is the device-selection -> content-action mapping
    verified at the engine boundary (R6.1, R7.4).
    """
    drive = env["drive"]
    # Only the actions whose branch consumes pre-existing data get seeded: the
    # two delete-first actions (delete_then_fresh / nav_only_delete) and
    # manifest_from_card (which builds the from-card manifest off it). The
    # "no existing data" actions (fresh = Case B, nav_only = Case D) model an
    # empty-ChartData drive, so seeding them would be an invalid precondition.
    if content_action in ("manifest_from_card", "delete_then_fresh", "nav_only_delete"):
        _seed_stale_card_chartdata(drive)

    result = du.adopt_drive(
        env["mount"],
        chart_capable=chart_capable,
        nav_label_prefix="HXR" if chart_capable else "MINIAP",
        content_action=content_action,
    )
    assert result["success"] is True, result["message"]

    # Nav is always synced regardless of chart capability (R10.1).
    assert (drive / "NAV.DB").read_bytes() == b"nav-db"
    assert (drive / "NAV-proc.DB").read_bytes() == b"nav-proc"

    # Chart families present iff the action is chart-capable.
    for parts in _CHART_FAMILY_MARKERS:
        exists = (drive.joinpath(*parts)).exists()
        assert exists is expect_charts, (
            f"{content_action}: {'/'.join(parts)} present={exists}, "
            f"expected {expect_charts}"
        )
    # A nav-only outcome carries NO ChartData/ payload at all.
    if not expect_charts:
        assert not (drive / "ChartData").exists()

    # Identity labeling snapshot matches the device capability, and the drive
    # resolves via both files (R7.4) for every content action.
    assert du.read_identity(env["mount"])["chart_capable"] is chart_capable
    _assert_both_files_and_resolves(env["mount"])


# --- defensive guard (task 12.1): invalid content_action raises --------------


@pytest.mark.parametrize(
    "bad_action", ["", "bogus", "MANIFEST_FROM_CARD", "delete", None]
)
def test_adopt_invalid_content_action_raises_valueerror(env, bad_action):
    """An unknown content_action raises ValueError and never touches the drive.

    The main-thread handler only ever passes one of the five known values; this
    is the engine's defensive guard (task 12.1). An invalid value fails fast
    BEFORE any identity / manifest write, which is the engine half of the abort
    contract (R5.4, R6.8): the engine is only ever entered with a valid action.
    """
    drive = env["drive"]
    with pytest.raises(ValueError):
        du.adopt_drive(
            env["mount"],
            chart_capable=True,
            nav_label_prefix="HXR",
            content_action=bad_action,
        )
    # Fast-fail: no identity, no manifest, drive untouched.
    assert du.read_identity(env["mount"]) is None
    assert Manifest_Store.load_card(env["mount"]) is None
    assert du.resolve_drive_id(env["mount"]) is None


def test_adopt_rejects_every_action_outside_the_known_set(env):
    """Only the five documented content_action values are accepted by the engine.

    Corroborates R5.4 / R6.8 at the engine boundary: the valid-action set the
    engine will run is exactly the five mapped from device selection; anything
    else is rejected before the drive is modified. The "app never CALLS
    adopt_drive on a cancel/decline" side of the abort contract is covered by the
    app-level tests (task 12.3 Case-C-decline, task 13.2 cancel-aborts); this
    asserts the complementary engine-side guarantee.
    """
    assert du._ADOPT_CONTENT_ACTIONS == {
        "manifest_from_card",
        "delete_then_fresh",
        "fresh",
        "nav_only_delete",
        "nav_only",
    }
    for bad in ["adopt", "nav", "charts", "fresh_then_delete", "none"]:
        with pytest.raises(ValueError):
            du.adopt_drive(
                env["mount"],
                chart_capable=False,
                nav_label_prefix="MINIAP",
                content_action=bad,
            )
    # None of the rejected calls provisioned the drive.
    assert du.resolve_drive_id(env["mount"]) is None
