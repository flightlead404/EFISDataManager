# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Integration tests for the settings-management read-only dashboard routes.

Exercises GET /api/settings/devices, GET /api/settings/history, and
GET /api/settings/view through Flask's test client against a temp per-device
archive tree (<archive_root>/Settings/link{N}/) and an in-memory config.

These are example/integration tests (not Hypothesis properties).

Requirements: 2.1, 2.2, 2.3, 3.1, 3.2, 3.3, 3.4, 3.5, 9.1, 9.2
"""

import copy

import pytest

from efis_data_manager.dashboard import app as dash_app
from efis_data_manager.efis_settings import snapshot_view, SettingsParseError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_snapshot(archive_root, link_dir, name, sids, update_value=100):
    """Write a KEY=VALUE snapshot under <archive_root>/Settings/<link_dir>/."""
    device_dir = archive_root / "Settings" / link_dir
    device_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"UPDATE={update_value}"]
    lines += [f"{sid}={value}" for sid, value in sids.items()]
    (device_dir / name).write_text("\n".join(lines) + "\n", encoding="ascii")
    return device_dir / name


def _device_config(archive_root):
    """A config with a two-device managed aircraft + a temp archive root."""
    return {
        "archive_path": str(archive_root),
        "aircraft": {
            "A60670": {
                "device_count": 2,
                "devices": {
                    "1": {"link_id": "1", "friendly_name": "Pilot",
                          "chart_capable": True},
                    "2": {"link_id": "2", "friendly_name": "",
                          "chart_capable": False},
                },
                "analysis_data_source": "1",
            },
        },
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Flask test client wired to a temp archive dir + in-memory config."""
    archive_root = tmp_path / "archive"
    archive_root.mkdir()

    state = {"config": _device_config(archive_root)}

    def fake_load_config():
        return copy.deepcopy(state["config"])

    monkeypatch.setattr(dash_app, "load_config", fake_load_config)

    client = dash_app.app.test_client()
    return client, archive_root, state


# ---------------------------------------------------------------------------
# /api/settings/devices
# ---------------------------------------------------------------------------

def test_devices_lists_configured_devices_with_labels(env):
    """Devices endpoint returns each configured device's Link_ID + label (R2.2)."""
    client, _, _ = env
    resp = client.get("/api/settings/devices")
    assert resp.status_code == 200
    devices = resp.get_json()["devices"]
    assert devices == [
        {"link_id": "1", "label": "Pilot (Link 1)"},
        {"link_id": "2", "label": "Link 2"},
    ]


def test_devices_empty_when_no_aircraft_learned(env):
    """An unlearned aircraft yields an empty device list without error."""
    client, _, state = env
    state["config"] = {"archive_path": state["config"]["archive_path"]}
    resp = client.get("/api/settings/devices")
    assert resp.status_code == 200
    assert resp.get_json()["devices"] == []


# ---------------------------------------------------------------------------
# /api/settings/history
# ---------------------------------------------------------------------------

def test_history_orders_newest_to_oldest(env):
    """History is ordered newest->oldest by (Archive_Date, sequence) (R2.1)."""
    client, archive_root, _ = env
    _write_snapshot(archive_root, "link1", "Settings-2026-01-01.dat", {"123": "2700"})
    _write_snapshot(archive_root, "link1", "Settings-2026-02-01.dat", {"123": "2710"})
    _write_snapshot(archive_root, "link1", "Settings-2026-02-01-2.dat", {"123": "2720"})

    resp = client.get("/api/settings/history?link_id=1&family=Settings")
    assert resp.status_code == 200
    data = resp.get_json()
    names = [s["filename"] for s in data["snapshots"]]
    assert names == [
        "Settings-2026-02-01-2.dat",  # same date, higher sequence first
        "Settings-2026-02-01.dat",
        "Settings-2026-01-01.dat",
    ]
    # Metadata per snapshot: date, sequence, family.
    assert data["snapshots"][0]["archive_date"] == "2026-02-01"
    assert data["snapshots"][0]["sequence"] == 2
    assert data["snapshots"][0]["family"] == "Settings"


def test_history_empty_for_device_without_snapshots(env):
    """A device/family with no archived snapshots reports an empty list (R2.3)."""
    client, _, _ = env
    resp = client.get("/api/settings/history?link_id=2&family=Settings")
    assert resp.status_code == 200
    assert resp.get_json()["snapshots"] == []


def test_history_is_per_device_isolated(env):
    """Link1's history never includes Link2's snapshots (per-device isolation)."""
    client, archive_root, _ = env
    _write_snapshot(archive_root, "link1", "Settings-2026-01-01.dat", {"123": "2700"})
    _write_snapshot(archive_root, "link2", "Settings-2026-03-01.dat", {"123": "2800"})

    resp = client.get("/api/settings/history?link_id=1&family=Settings")
    names = [s["filename"] for s in resp.get_json()["snapshots"]]
    assert names == ["Settings-2026-01-01.dat"]


def test_history_rejects_unknown_family(env):
    """An unknown family is rejected with a 400 (not a silent empty list)."""
    client, _, _ = env
    resp = client.get("/api/settings/history?link_id=1&family=State")
    assert resp.status_code == 400
    assert "error" in resp.get_json()


# ---------------------------------------------------------------------------
# /api/settings/view
# ---------------------------------------------------------------------------

def test_view_labels_recognized_and_flags_unlabeled(env):
    """View labels recognized SIDs and flags unknown SIDs unlabeled (R3.2/3.3)."""
    client, archive_root, _ = env
    # 123 = Max RPM (recognized); 99999 is not in the mapping (unlabeled).
    _write_snapshot(archive_root, "link1", "Settings-2026-01-01.dat",
                    {"123": "2700", "99999": "xyz"})

    resp = client.get(
        "/api/settings/view?link_id=1&family=Settings&name=Settings-2026-01-01.dat")
    assert resp.status_code == 200
    by_sid = {row["sid"]: row for row in resp.get_json()["sids"]}

    assert by_sid["123"]["label"] == "Max RPM"
    assert by_sid["123"]["unlabeled"] is False
    assert by_sid["123"]["value"] == "2700"

    assert by_sid["99999"]["unlabeled"] is True
    assert by_sid["99999"]["label"] is None
    assert by_sid["99999"]["value"] == "xyz"


def _expected_power_labels():
    from efis_data_manager import power
    exp = {power.SID_RATED_HP: "Rated HP"}
    for cols, fmt in (
        (power.SID_RPM_COLUMN, "Power Map RPM (row {})"),
        (power.SID_MAP55_COLUMN, "Power Map MAP for 55% (row {})"),
        (power.SID_MAP75_COLUMN, "Power Map MAP for 75% (row {})"),
        (power.SID_ALTITUDE_COLUMN, "Power Map Altitude (row {})"),
        (power.SID_DELTA_HP_COLUMN, "Power Map Delta HP (row {})"),
    ):
        for n, sid in enumerate(cols, start=1):
            exp[sid] = fmt.format(n)
    return exp


def test_recognized_sid_labels_include_power_map():
    """167 and all 50 power-map column SIDs are labeled; none collide."""
    from efis_data_manager.efis_settings import recognized_sid_labels
    labels = recognized_sid_labels()
    exp = _expected_power_labels()
    assert len(exp) == 51
    for sid, label in exp.items():
        assert labels[sid] == label
    assert labels["167"] == "Rated HP"
    assert labels["168"] == "Power Map RPM (row 1)"
    assert labels["217"] == "Power Map Delta HP (row 10)"
    # Existing labels are unchanged and nothing collided with them.
    assert labels["123"] == "Max RPM"
    others = {s: l for s, l in labels.items() if s not in exp}
    assert others and all(l not in exp.values() for l in others.values())


def test_snapshot_view_marks_power_map_sids_labeled(tmp_path):
    exp = _expected_power_labels()
    path = _write_snapshot(tmp_path, "link1", "Settings-2026-01-01.dat",
                           {sid: "1" for sid in exp})
    rows = snapshot_view(str(path))
    by_sid = {r.sid: r for r in rows}
    assert set(exp) <= set(by_sid)
    for sid, label in exp.items():
        assert by_sid[sid].label == label
        assert by_sid[sid].unlabeled is False


def test_view_endpoint_labels_power_map_sids(env):
    client, archive_root, _ = env
    _write_snapshot(archive_root, "link1", "Settings-2026-01-01.dat",
                    {"123": "2700", "168": "1000", "208": "5"})
    resp = client.get(
        "/api/settings/view?link_id=1&family=Settings&name=Settings-2026-01-01.dat")
    assert resp.status_code == 200
    by_sid = {r["sid"]: r for r in resp.get_json()["sids"]}
    assert by_sid["168"]["label"] == "Power Map RPM (row 1)"
    assert by_sid["208"]["label"] == "Power Map Delta HP (row 1)"
    assert by_sid["168"]["unlabeled"] is False
    assert by_sid["123"]["label"] == "Max RPM"


def test_view_excludes_volatile_keys(env):
    """The volatile UPDATE/CHECKSIZE/CHECKSUM keys are not shown in the view."""
    client, archive_root, _ = env
    _write_snapshot(archive_root, "link1", "Settings-2026-01-01.dat",
                    {"123": "2700", "CHECKSIZE": "10", "CHECKSUM": "abcd"},
                    update_value=100)

    resp = client.get(
        "/api/settings/view?link_id=1&family=Settings&name=Settings-2026-01-01.dat")
    sids = {row["sid"] for row in resp.get_json()["sids"]}
    assert "UPDATE" not in sids
    assert "CHECKSIZE" not in sids
    assert "CHECKSUM" not in sids
    assert "123" in sids


def test_view_missing_snapshot_is_404(env):
    """A view request for a non-existent snapshot returns 404, not a crash."""
    client, _, _ = env
    resp = client.get(
        "/api/settings/view?link_id=1&family=Settings&name=Nope-2026-01-01.dat")
    assert resp.status_code == 404
    assert "error" in resp.get_json()


def test_view_rejects_path_traversal_name(env):
    """A non-bare name (path traversal attempt) is rejected with 400."""
    client, _, _ = env
    resp = client.get(
        "/api/settings/view?link_id=1&family=Settings&name=../secret.dat")
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_view_reports_parse_failure_and_leaves_file(env):
    """A malformed snapshot is reported (422) and left unchanged (R3.5, R9.3)."""
    client, archive_root, _ = env
    device_dir = archive_root / "Settings" / "link1"
    device_dir.mkdir(parents=True, exist_ok=True)
    bad = device_dir / "Settings-2026-01-01.dat"
    bad.write_text("no valid pairs here\n", encoding="ascii")
    before = bad.read_bytes()

    resp = client.get(
        "/api/settings/view?link_id=1&family=Settings&name=Settings-2026-01-01.dat")
    assert resp.status_code == 422
    assert "error" in resp.get_json()
    # The snapshot file is unchanged (read-only contract, R3.5/R9.3).
    assert bad.read_bytes() == before


# ---------------------------------------------------------------------------
# snapshot_view unit coverage (shared SID vocabulary, read-only)
# ---------------------------------------------------------------------------

def test_snapshot_view_orders_labeled_before_unlabeled(tmp_path):
    """snapshot_view returns recognized SIDs first, unlabeled SIDs trailing."""
    path = tmp_path / "Settings-2026-01-01.dat"
    path.write_text("UPDATE=1\n123=2700\n88888=raw\n", encoding="ascii")
    rows = snapshot_view(str(path))
    assert rows[0].unlabeled is False
    assert rows[-1].unlabeled is True
    assert rows[-1].sid == "88888"


def test_snapshot_view_raises_on_unparseable(tmp_path):
    """snapshot_view surfaces SettingsParseError for an unparseable file."""
    path = tmp_path / "Settings-2026-01-01.dat"
    path.write_text("garbage with no pairs\n", encoding="ascii")
    with pytest.raises(SettingsParseError):
        snapshot_view(str(path))


# ---------------------------------------------------------------------------
# /api/settings/restore (POST) — dashboard-SELECT / DM-WRITE handoff (R5)
# ---------------------------------------------------------------------------
#
# These exercise the restore POST through the Flask test client. The drive
# write path is the real settings_writer.restore_version (no mocking of the
# write itself), but the "bound drive" lookup is mocked so we never touch a
# real /Volumes mount: _bound_drive_for_link_id is patched to return a temp
# directory standing in for the mounted drive.


def _mock_bound_drive(monkeypatch, mount_point):
    """Patch _bound_drive_for_link_id to resolve to ``mount_point`` (R5.2)."""
    def fake_bound(config, link_id):
        return str(mount_point), None
    monkeypatch.setattr(dash_app, "_bound_drive_for_link_id", fake_bound)


def test_restore_writes_sole_slot_and_returns_write_result(env, monkeypatch):
    """A valid restore writes the archived bytes as the sole slot (R5.1-R5.4)."""
    client, archive_root, _ = env
    archived = _write_snapshot(
        archive_root, "link1", "Settings-2026-01-01.dat", {"123": "2700"})
    original_bytes = archived.read_bytes()

    # Temp dir standing in for the mounted bound drive, pre-seeded with the OTHER
    # slot so we can assert it gets removed (sole slot, R5.4).
    drive = archive_root.parent / "drive"
    drive.mkdir()
    (drive / "Settings.bak").write_bytes(b"old other slot\n")
    _mock_bound_drive(monkeypatch, drive)

    resp = client.post("/api/settings/restore", json={
        "link_id": "1", "family": "Settings",
        "name": "Settings-2026-01-01.dat",
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "written"
    # Written slot keeps the archived file's own extension (.dat here).
    assert (drive / "Settings.dat").read_bytes() == original_bytes  # R5.3 bytes verbatim
    assert not (drive / "Settings.bak").exists()                    # R5.4 sole slot
    assert str(drive / "Settings.bak") in data["removed_slots"]
    # The archived source is untouched (read-only archive).
    assert archived.read_bytes() == original_bytes


def test_restore_rejects_non_settings_family(env, monkeypatch):
    """WP/Plan restore is rejected with 400 and writes nothing (R5.9)."""
    client, archive_root, _ = env
    # A WP snapshot exists, but restore must refuse it regardless.
    _write_snapshot(archive_root, "link1", "WP-2026-01-01.bak", {"123": "1"})
    drive = archive_root.parent / "drive"
    drive.mkdir()

    called = {"bound": False}

    def fake_bound(config, link_id):
        called["bound"] = True
        return str(drive), None
    monkeypatch.setattr(dash_app, "_bound_drive_for_link_id", fake_bound)

    resp = client.post("/api/settings/restore", json={
        "link_id": "1", "family": "WP", "name": "WP-2026-01-01.bak",
    })
    assert resp.status_code == 400
    assert "error" in resp.get_json()
    # Rejected before any drive resolution or write.
    assert called["bound"] is False
    assert not (drive / "WP.bak").exists()


def test_restore_rejects_plan_family(env, monkeypatch):
    """Plan restore is rejected with 400 and writes nothing (R5.9)."""
    client, archive_root, _ = env
    # A Plan snapshot exists, but restore must refuse it regardless.
    _write_snapshot(archive_root, "link1", "Plan-2026-01-01.bak", {"123": "1"})
    drive = archive_root.parent / "drive"
    drive.mkdir()

    called = {"bound": False}

    def fake_bound(config, link_id):
        called["bound"] = True
        return str(drive), None
    monkeypatch.setattr(dash_app, "_bound_drive_for_link_id", fake_bound)

    resp = client.post("/api/settings/restore", json={
        "link_id": "1", "family": "Plan", "name": "Plan-2026-01-01.bak",
    })
    assert resp.status_code == 400
    assert resp.get_json()["status"] == "rejected"
    # Rejected before any drive resolution or write (R5.9).
    assert called["bound"] is False
    assert not (drive / "Plan.bak").exists()


def test_restore_rejects_path_traversal_name(env, monkeypatch):
    """A non-bare name (path traversal attempt) is rejected with 400."""
    client, _, _ = env
    # Guard must fire before any drive lookup.
    monkeypatch.setattr(dash_app, "_bound_drive_for_link_id",
                        lambda c, l: (_ for _ in ()).throw(
                            AssertionError("drive lookup must not run")))
    resp = client.post("/api/settings/restore", json={
        "link_id": "1", "family": "Settings", "name": "../secret.dat",
    })
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_restore_missing_snapshot_is_404(env, monkeypatch):
    """Restoring a non-existent snapshot returns 404 and writes nothing."""
    client, _, _ = env
    monkeypatch.setattr(dash_app, "_bound_drive_for_link_id",
                        lambda c, l: (_ for _ in ()).throw(
                            AssertionError("drive lookup must not run")))
    resp = client.post("/api/settings/restore", json={
        "link_id": "1", "family": "Settings", "name": "Nope-2026-01-01.dat",
    })
    assert resp.status_code == 404
    assert "error" in resp.get_json()


def test_restore_no_bound_drive_is_409(env, monkeypatch):
    """When no drive bound to the device is mounted, restore is refused (R5.2)."""
    client, archive_root, _ = env
    _write_snapshot(archive_root, "link1", "Settings-2026-01-01.dat", {"123": "2700"})

    def fake_bound(config, link_id):
        return None, "no managed EFIS drive is currently mounted"
    monkeypatch.setattr(dash_app, "_bound_drive_for_link_id", fake_bound)

    resp = client.post("/api/settings/restore", json={
        "link_id": "1", "family": "Settings",
        "name": "Settings-2026-01-01.dat",
    })
    assert resp.status_code == 409
    data = resp.get_json()
    assert data["status"] == "no-drive"
    assert "error" in data


def test_restore_is_explicit_post_only(env):
    """Restore is a POST handoff; it is never a GET (never automatic, R5.7)."""
    client, _, _ = env
    resp = client.get("/api/settings/restore")
    assert resp.status_code == 405  # method not allowed


def test_restore_bound_drive_lookup_matches_requested_device(env, monkeypatch):
    """_bound_drive_for_link_id resolves the mounted drive and matches Link_ID (R5.2)."""
    client, archive_root, state = env

    # Fake a mounted managed drive that resolves to Link_ID 1.
    from efis_data_manager import device_resolution
    from efis_data_manager.device_resolution import ResolutionStatus, ResolutionOutcome

    monkeypatch.setattr(
        "efis_data_manager.usb_monitor.find_mounted_managed_drive",
        lambda volumes_dir="/Volumes": "/Volumes/EFIS")
    monkeypatch.setattr(
        "efis_data_manager.drive_updater.read_identity", lambda mp: None)
    monkeypatch.setattr(dash_app, "_parse_drive_settings", lambda mp: object())

    def fake_resolve(parsed, recorded, config):
        return ResolutionOutcome(
            status=ResolutionStatus.RESOLVED, link_id="1",
            device={"link_id": "1"}, display_label="Pilot (Link 1)")
    monkeypatch.setattr(device_resolution, "resolve_drive", fake_resolve)

    mount, reason = dash_app._bound_drive_for_link_id(state["config"], "1")
    assert mount == "/Volumes/EFIS"
    assert reason is None

    # A request for a different device finds no bound drive.
    mount2, reason2 = dash_app._bound_drive_for_link_id(state["config"], "2")
    assert mount2 is None
    assert "bound to device Link_ID 1" in reason2


# ---------------------------------------------------------------------------
# /api/settings/compare — labeled per-SID diff (R4)
# ---------------------------------------------------------------------------
#
# Covers archived-vs-archived, archived-vs-drive (b=drive, mocked bound drive),
# equivalent (equal Content_Hash), and added/removed SIDs. All read-only: the
# archived snapshots and drive slot are parsed, never modified.


def test_compare_archived_vs_archived_reports_changed_sid(env):
    """Two archived snapshots diff per-SID with label + value-in-a/value-in-b (R4.1)."""
    client, archive_root, _ = env
    _write_snapshot(archive_root, "link1", "Settings-2026-01-01.dat", {"123": "2700"})
    _write_snapshot(archive_root, "link1", "Settings-2026-02-01.dat", {"123": "2710"})

    resp = client.get(
        "/api/settings/compare?link_id=1&family=Settings"
        "&a=Settings-2026-01-01.dat&b=Settings-2026-02-01.dat")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["equivalent"] is False
    diffs = {d["sid"]: d for d in data["differences"]}
    assert diffs["123"]["label"] == "Max RPM"
    assert diffs["123"]["unlabeled"] is False
    assert diffs["123"]["a"] == "2700"
    assert diffs["123"]["b"] == "2710"
    assert diffs["123"]["change"] == "changed"


def test_compare_equivalent_when_content_hash_equal(env):
    """Equal content (ignoring UPDATE/volatile keys) reports equivalent (R4.4)."""
    client, archive_root, _ = env
    # Same SID content, different UPDATE= => equal Content_Hash => equivalent.
    _write_snapshot(archive_root, "link1", "Settings-2026-01-01.dat",
                    {"123": "2700"}, update_value=100)
    _write_snapshot(archive_root, "link1", "Settings-2026-02-01.dat",
                    {"123": "2700"}, update_value=250)

    resp = client.get(
        "/api/settings/compare?link_id=1&family=Settings"
        "&a=Settings-2026-01-01.dat&b=Settings-2026-02-01.dat")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["equivalent"] is True
    # No content SID differs (UPDATE is excluded from the diff, R4.2).
    assert data["differences"] == []


def test_compare_reports_added_and_removed_sids(env):
    """A SID present in one version but not the other is added/removed (R4.3)."""
    client, archive_root, _ = env
    # a has SID 456; b has SID 789. Each is absent on the other side.
    _write_snapshot(archive_root, "link1", "Settings-2026-01-01.dat",
                    {"123": "2700", "456": "foo"})
    _write_snapshot(archive_root, "link1", "Settings-2026-02-01.dat",
                    {"123": "2700", "789": "bar"})

    resp = client.get(
        "/api/settings/compare?link_id=1&family=Settings"
        "&a=Settings-2026-01-01.dat&b=Settings-2026-02-01.dat")
    assert resp.status_code == 200
    data = resp.get_json()
    diffs = {d["sid"]: d for d in data["differences"]}
    # 123 is equal => not reported; only the added/removed SIDs appear.
    assert "123" not in diffs
    assert diffs["456"]["change"] == "removed"
    assert diffs["456"]["a"] == "foo"
    assert diffs["456"]["b"] is None
    assert diffs["789"]["change"] == "added"
    assert diffs["789"]["a"] is None
    assert diffs["789"]["b"] == "bar"


def test_compare_excludes_volatile_keys(env):
    """UPDATE/CHECKSIZE/CHECKSUM never appear in the reported differences (R4.2)."""
    client, archive_root, _ = env
    _write_snapshot(archive_root, "link1", "Settings-2026-01-01.dat",
                    {"123": "2700", "CHECKSIZE": "10", "CHECKSUM": "aaaa"},
                    update_value=100)
    _write_snapshot(archive_root, "link1", "Settings-2026-02-01.dat",
                    {"123": "2710", "CHECKSIZE": "20", "CHECKSUM": "bbbb"},
                    update_value=200)

    resp = client.get(
        "/api/settings/compare?link_id=1&family=Settings"
        "&a=Settings-2026-01-01.dat&b=Settings-2026-02-01.dat")
    assert resp.status_code == 200
    sids = {d["sid"] for d in resp.get_json()["differences"]}
    assert "UPDATE" not in sids
    assert "CHECKSIZE" not in sids
    assert "CHECKSUM" not in sids
    assert sids == {"123"}


def test_compare_archived_vs_drive(env, monkeypatch):
    """b='drive' diffs the archived snapshot against the bound drive's slot (R4.5)."""
    client, archive_root, _ = env
    _write_snapshot(archive_root, "link1", "Settings-2026-01-01.dat", {"123": "2700"})

    # Temp dir standing in for the mounted bound drive, carrying a current slot.
    drive = archive_root.parent / "drive"
    drive.mkdir()
    (drive / "Settings.dat").write_text(
        "UPDATE=500\n123=2900\n", encoding="ascii")
    _mock_bound_drive(monkeypatch, drive)

    resp = client.get(
        "/api/settings/compare?link_id=1&family=Settings"
        "&a=Settings-2026-01-01.dat&b=drive")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["equivalent"] is False
    diffs = {d["sid"]: d for d in data["differences"]}
    assert diffs["123"]["a"] == "2700"   # archived snapshot
    assert diffs["123"]["b"] == "2900"   # current drive slot
    assert diffs["123"]["change"] == "changed"


def test_compare_archived_vs_drive_equivalent(env, monkeypatch):
    """b='drive' reports equivalent when the drive's slot matches the archive (R4.4)."""
    client, archive_root, _ = env
    _write_snapshot(archive_root, "link1", "Settings-2026-01-01.dat",
                    {"123": "2700"}, update_value=100)

    drive = archive_root.parent / "drive"
    drive.mkdir()
    # Same content SID, different UPDATE => equal Content_Hash.
    (drive / "Settings.dat").write_text(
        "UPDATE=999\n123=2700\n", encoding="ascii")
    _mock_bound_drive(monkeypatch, drive)

    resp = client.get(
        "/api/settings/compare?link_id=1&family=Settings"
        "&a=Settings-2026-01-01.dat&b=drive")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["equivalent"] is True
    assert data["differences"] == []


def test_compare_drive_no_bound_drive_is_409(env, monkeypatch):
    """b='drive' with no bound drive mounted is refused (409), nothing compared."""
    client, archive_root, _ = env
    _write_snapshot(archive_root, "link1", "Settings-2026-01-01.dat", {"123": "2700"})

    def fake_bound(config, link_id):
        return None, "no managed EFIS drive is currently mounted"
    monkeypatch.setattr(dash_app, "_bound_drive_for_link_id", fake_bound)

    resp = client.get(
        "/api/settings/compare?link_id=1&family=Settings"
        "&a=Settings-2026-01-01.dat&b=drive")
    assert resp.status_code == 409
    assert resp.get_json()["status"] == "no-drive"


def test_compare_missing_a_snapshot_is_404(env):
    """A compare against a non-existent 'a' snapshot returns 404, not a crash."""
    client, archive_root, _ = env
    _write_snapshot(archive_root, "link1", "Settings-2026-02-01.dat", {"123": "2700"})
    resp = client.get(
        "/api/settings/compare?link_id=1&family=Settings"
        "&a=Nope-2026-01-01.dat&b=Settings-2026-02-01.dat")
    assert resp.status_code == 404
    assert "error" in resp.get_json()


def test_compare_missing_b_snapshot_is_404(env):
    """A compare against a non-existent 'b' snapshot returns 404, not a crash."""
    client, archive_root, _ = env
    _write_snapshot(archive_root, "link1", "Settings-2026-01-01.dat", {"123": "2700"})
    resp = client.get(
        "/api/settings/compare?link_id=1&family=Settings"
        "&a=Settings-2026-01-01.dat&b=Nope-2026-02-01.dat")
    assert resp.status_code == 404
    assert "error" in resp.get_json()


def test_compare_rejects_path_traversal_names(env):
    """A non-bare 'a' or 'b' name (path traversal attempt) is rejected with 400."""
    client, archive_root, _ = env
    _write_snapshot(archive_root, "link1", "Settings-2026-01-01.dat", {"123": "2700"})
    resp_a = client.get(
        "/api/settings/compare?link_id=1&family=Settings"
        "&a=../secret.dat&b=Settings-2026-01-01.dat")
    assert resp_a.status_code == 400
    resp_b = client.get(
        "/api/settings/compare?link_id=1&family=Settings"
        "&a=Settings-2026-01-01.dat&b=../secret.dat")
    assert resp_b.status_code == 400


def test_compare_rejects_unknown_family(env):
    """An unknown family is rejected with a 400."""
    client, _, _ = env
    resp = client.get(
        "/api/settings/compare?link_id=1&family=State"
        "&a=Settings-2026-01-01.dat&b=drive")
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_compare_reports_parse_failure_and_leaves_file(env):
    """A malformed 'a' snapshot is reported (422) and left unchanged (R9.3)."""
    client, archive_root, _ = env
    device_dir = archive_root / "Settings" / "link1"
    device_dir.mkdir(parents=True, exist_ok=True)
    bad = device_dir / "Settings-2026-01-01.dat"
    bad.write_text("no valid pairs here\n", encoding="ascii")
    before = bad.read_bytes()
    _write_snapshot(archive_root, "link1", "Settings-2026-02-01.dat", {"123": "2700"})

    resp = client.get(
        "/api/settings/compare?link_id=1&family=Settings"
        "&a=Settings-2026-01-01.dat&b=Settings-2026-02-01.dat")
    assert resp.status_code == 422
    assert "error" in resp.get_json()
    assert bad.read_bytes() == before


# ---------------------------------------------------------------------------
# GRT SID dictionary wiring (task 17.2)
# ---------------------------------------------------------------------------

def test_view_rows_carry_grt_name_and_value_meaning(env):
    client, archive_root, _ = env
    _write_snapshot(archive_root, "link1", "Settings-2026-01-01.dat",
                    {"64": "3", "345": "0", "123": "2700", "99999": "xyz"})
    resp = client.get(
        "/api/settings/view?link_id=1&family=Settings&name=Settings-2026-01-01.dat")
    assert resp.status_code == 200
    by_sid = {r["sid"]: r for r in resp.get_json()["sids"]}
    assert by_sid["64"]["value_meaning"] == "Amp"
    assert by_sid["64"]["grt_name"]
    assert by_sid["345"]["value_meaning"] == "Degrees Fahrenheit"
    assert by_sid["123"]["grt_name"] == "SID_LIM_MAX_RPM"
    assert by_sid["123"]["value_meaning"] is None
    assert by_sid["99999"]["unlabeled"] is True
    assert by_sid["99999"]["label"] is None
    assert by_sid["99999"]["grt_name"] is None
    assert by_sid["99999"]["value_meaning"] is None


def test_compare_rows_carry_meanings(env):
    client, archive_root, _ = env
    _write_snapshot(archive_root, "link1", "Settings-2026-01-01.dat", {"345": "0"})
    _write_snapshot(archive_root, "link1", "Settings-2026-02-01.dat", {"345": "1"})
    resp = client.get(
        "/api/settings/compare?link_id=1&family=Settings"
        "&a=Settings-2026-01-01.dat&b=Settings-2026-02-01.dat")
    assert resp.status_code == 200
    d = {x["sid"]: x for x in resp.get_json()["differences"]}["345"]
    assert d["a_meaning"] == "Degrees Fahrenheit"
    assert d["b_meaning"]
    assert d["b_meaning"] != d["a_meaning"]
    assert d["grt_name"]


def test_snapshot_view_labeled_rows_sorted_by_numeric_sid(tmp_path):
    path = tmp_path / "Settings-2026-01-01.dat"
    path.write_text("UPDATE=1\n123=1\n64=3\n345=0\n99999=x\n88888=y\n9=1\n",
                    encoding="ascii")
    rows = snapshot_view(str(path))
    labeled = [int(r.sid) for r in rows if not r.unlabeled]
    assert labeled == sorted(labeled)
    assert labeled.index(64) < labeled.index(123) < labeled.index(345)
    assert [r.sid for r in rows if r.unlabeled] == ["88888", "99999"]
    assert rows[-1].unlabeled and not rows[0].unlabeled


def test_compare_differences_ordered_by_numeric_sid(env):
    client, archive_root, _ = env
    _write_snapshot(archive_root, "link1", "Settings-2026-01-01.dat",
                    {"123": "1", "70": "1", "9": "1"})
    _write_snapshot(archive_root, "link1", "Settings-2026-02-01.dat",
                    {"123": "2", "70": "2", "9": "2"})
    resp = client.get(
        "/api/settings/compare?link_id=1&family=Settings"
        "&a=Settings-2026-01-01.dat&b=Settings-2026-02-01.dat")
    assert resp.status_code == 200
    sids = [d["sid"] for d in resp.get_json()["differences"]]
    assert sids == ["9", "70", "123"]


def test_snapshot_view_excludes_checktype(tmp_path):
    path = tmp_path / "Settings-2026-01-01.dat"
    path.write_text("CHECKTYPE=STD\nUPDATE=1\n123=2700\n", encoding="ascii")
    sids = [r.sid for r in snapshot_view(str(path))]
    assert "CHECKTYPE" not in sids
    assert "123" in sids


def test_settings_page_renders_snapshot_meaning_and_tooltip(env):
    client, _, _ = env
    resp = client.get("/settings")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "withMeaning(row.value, row.value_meaning)" in html
    assert "labelTd.title = row.grt_name" in html
