# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Prompt-flow tests for the divergence confirmation against a REAL archive
(settings-management task 10.5; Requirements 7.2, 7.3, 7.4, 7.5, 7.6).

Unlike ``test_app_settings_wiring`` these tests mock neither
``settings_writer.diverges`` nor the archiver. They use a real temp archive
root with real per-device snapshots and a real temp card directory, and drive
the real ``_run_archive`` / ``_confirm_archive_settings`` bound to a
``SimpleNamespace`` stand-in. Only the alert (recording, returning 1/0), the
notification/status sinks and the follow-on chart-sync thread are stubbed.
"""

import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from efis_data_manager.app import EFISDataManagerApp
from efis_data_manager.settings_archive import device_settings_dir

OLD_DATE = "2026-01-01"
LATEST_DATE = "2026-03-01"
DEMO_NAME = "DEMO-0001.LOG"
DEMO_BYTES = b"demo log content\n"


def _body(vne: int, update: int = 5, checksize: int = 1234, checksum: str = "ABCD") -> str:
    """A well-formed-enough GRT settings body (archiver copies bytes verbatim)."""
    return (
        "CHECKTYPE=STD\n"
        f"22={vne}\n"
        f"UPDATE={update}\n"
        f"CHECKSIZE={checksize}\n"
        f"CHECKSUM={checksum}\n"
    )


def _tree(root: Path) -> dict:
    """Relative path -> bytes for every file under ``root`` (empty if absent)."""
    if not root.exists():
        return {}
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@pytest.fixture
def env():
    archive_root = Path(tempfile.mkdtemp(prefix="efis_prompt_archive_"))
    card = Path(tempfile.mkdtemp(prefix="efis_prompt_card_"))
    try:
        yield SimpleNamespace(archive=archive_root, card=card)
    finally:
        shutil.rmtree(archive_root, ignore_errors=True)
        shutil.rmtree(card, ignore_errors=True)


def _seed_link1(env, old_vne=150, latest_vne=200):
    """Real per-device history for Link 1: an older and a latest snapshot."""
    d = device_settings_dir(env.archive, "1")
    d.mkdir(parents=True, exist_ok=True)
    (d / f"Settings-{OLD_DATE}.dat").write_text(_body(old_vne, update=3))
    (d / f"Settings-{LATEST_DATE}.dat").write_text(_body(latest_vne, update=9))
    return d


def _put_card_settings(env, vne, name="Settings.dat", update=7, **kw):
    path = env.card / name
    path.write_text(_body(vne, update=update, **kw))
    return path


def _put_demo(env):
    (env.card / DEMO_NAME).write_bytes(DEMO_BYTES)


def _standin(env, alert_result):
    s = SimpleNamespace()
    s.config = {
        "archive_path": str(env.archive),
        "aircraft": {
            "A60670": {
                "devices": {"1": {"link_id": "1", "friendly_name": "PFD"}}
            }
        },
    }
    s.notify_calls = []
    s.alert_calls = []
    s._notify = lambda title, msg: s.notify_calls.append((title, msg))
    s._set_status = lambda msg: None
    s._refresh_alerts = lambda: None
    s._run_drive_update = lambda mp: None

    def _alert(title, message, **kw):
        s.alert_calls.append((title, message, kw))
        return alert_result

    s._run_alert_on_main_thread = _alert
    s._device_display_label = lambda lid: EFISDataManagerApp._device_display_label(s, lid)
    s._confirm_archive_settings = (
        lambda mp, lid: EFISDataManagerApp._confirm_archive_settings(s, mp, lid)
    )
    return s


def _run(env, standin, link_id="1"):
    """Run the real _run_archive; only config + chart-sync thread are patched."""
    with mock.patch(
        "efis_data_manager.archiver.load_config",
        return_value={"archive_path": str(env.archive)},
    ), mock.patch(
        "efis_data_manager.app.threading.Thread",
        return_value=SimpleNamespace(start=lambda: None),
    ):
        EFISDataManagerApp._run_archive(standin, str(env.card), link_id=link_id)


def _settings_snaps(env, link="link1"):
    d = env.archive / "Settings" / link
    return sorted(p.name for p in d.glob("Settings-*")) if d.exists() else []


def _demo_archived(env):
    return list((env.archive / "Demo").glob(f"*/{DEMO_NAME}"))


def _card_snapshot(env):
    return _tree(env.card)


# --------------------------------------------------------------------------- #
# 1. Differs -> prompt once; OK archives settings and the demo file
# --------------------------------------------------------------------------- #


def test_differs_from_older_snapshot_prompts_and_ok_archives(env):
    _seed_link1(env, old_vne=150, latest_vne=200)
    _put_card_settings(env, vne=150)  # equals the OLDER snapshot
    _put_demo(env)
    s = _standin(env, alert_result=1)

    _run(env, s)

    assert len(s.alert_calls) == 1
    _title, msg, kw = s.alert_calls[0]
    assert kw == {"ok": "Archive settings", "cancel": "Skip settings"}
    assert "PFD (Link 1)" in msg
    assert LATEST_DATE in msg
    assert f"This card matches the version archived {OLD_DATE}." in msg
    assert "has not been archived before" not in msg

    snaps = _settings_snaps(env)
    assert len(snaps) == 3, snaps  # two seeded + one new dated snapshot
    new = [n for n in snaps if OLD_DATE not in n and LATEST_DATE not in n]
    assert len(new) == 1 and new[0].endswith(".dat")
    assert (env.archive / "Settings" / "link1" / new[0]).read_text() == _body(150, update=7)
    assert len(_demo_archived(env)) == 1


def test_novel_content_prompts_with_not_archived_wording_and_ok_archives(env):
    _seed_link1(env)
    _put_card_settings(env, vne=999)  # matches nothing
    _put_demo(env)
    s = _standin(env, alert_result=1)

    _run(env, s)

    assert len(s.alert_calls) == 1
    _title, msg, kw = s.alert_calls[0]
    assert kw == {"ok": "Archive settings", "cancel": "Skip settings"}
    assert "PFD (Link 1)" in msg
    assert LATEST_DATE in msg
    assert "This content has not been archived before." in msg
    assert "matches the version archived" not in msg

    assert len(_settings_snaps(env)) == 3
    assert len(_demo_archived(env)) == 1


# --------------------------------------------------------------------------- #
# 2. Skip -> settings archive unchanged, card untouched, demo still archived
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("card_vne", [150, 999], ids=["matches-older", "novel"])
def test_skip_leaves_settings_archive_and_card_unchanged_but_archives_demo(env, card_vne):
    _seed_link1(env, old_vne=150, latest_vne=200)
    _put_card_settings(env, vne=card_vne)
    # WP / Plan slots on the card must not be archived either.
    (env.card / "WP.dat").write_text(_body(1))
    (env.card / "Plan.dat").write_text(_body(2))
    _put_demo(env)
    settings_before = _tree(env.archive / "Settings")
    card_settings_before = {
        k: v for k, v in _card_snapshot(env).items() if k != DEMO_NAME
    }
    s = _standin(env, alert_result=0)

    _run(env, s)

    assert len(s.alert_calls) == 1
    assert s.alert_calls[0][2] == {"ok": "Archive settings", "cancel": "Skip settings"}

    # Settings/WP/Plan archive tree is byte-for-byte unchanged.
    assert _tree(env.archive / "Settings") == settings_before
    # Card's settings files are untouched.
    card_after = _card_snapshot(env)
    assert {k: v for k, v in card_after.items() if k != DEMO_NAME} == card_settings_before
    # Non-settings content is still archived (and moved off the card).
    archived = _demo_archived(env)
    assert len(archived) == 1 and archived[0].read_bytes() == DEMO_BYTES
    assert DEMO_NAME not in card_after


# --------------------------------------------------------------------------- #
# 3. Card equals latest -> no prompt; dedup skip
# --------------------------------------------------------------------------- #


def test_card_equal_to_latest_does_not_prompt_and_dedups(env):
    _seed_link1(env, latest_vne=200)
    _put_card_settings(env, vne=200)
    _put_demo(env)
    before = _tree(env.archive / "Settings")
    s = _standin(env, alert_result=0)  # would skip if (wrongly) asked

    _run(env, s)

    assert s.alert_calls == []
    assert _tree(env.archive / "Settings") == before  # deduped, nothing new
    assert len(_demo_archived(env)) == 1


# --------------------------------------------------------------------------- #
# 4. No baseline -> no prompt; first snapshot archived
# --------------------------------------------------------------------------- #


def test_no_baseline_does_not_prompt_and_archives_first_snapshot(env):
    _put_card_settings(env, vne=200)
    _put_demo(env)
    s = _standin(env, alert_result=0)

    _run(env, s)

    assert s.alert_calls == []
    snaps = _settings_snaps(env)
    assert len(snaps) == 1
    assert (env.archive / "Settings" / "link1" / snaps[0]).read_text() == _body(200, update=7)
    assert len(_demo_archived(env)) == 1


# --------------------------------------------------------------------------- #
# 5. No Settings slot on the card -> no prompt
# --------------------------------------------------------------------------- #


def test_card_without_settings_slot_does_not_prompt(env):
    _seed_link1(env)
    _put_demo(env)
    before = _tree(env.archive / "Settings")
    s = _standin(env, alert_result=0)

    _run(env, s)

    assert s.alert_calls == []
    assert _tree(env.archive / "Settings") == before
    assert len(_demo_archived(env)) == 1


# --------------------------------------------------------------------------- #
# 6. link_id None -> no prompt; archives into Settings/unresolved/
# --------------------------------------------------------------------------- #


def test_link_id_none_does_not_prompt_and_archives_to_unresolved(env):
    _seed_link1(env)  # a differing history for link1 must be irrelevant
    _put_card_settings(env, vne=999)
    _put_demo(env)
    link1_before = _tree(env.archive / "Settings" / "link1")
    s = _standin(env, alert_result=0)

    _run(env, s, link_id=None)

    assert s.alert_calls == []
    unresolved = _settings_snaps(env, "unresolved")
    assert len(unresolved) == 1
    assert _tree(env.archive / "Settings" / "link1") == link1_before
    assert len(_demo_archived(env)) == 1


# --------------------------------------------------------------------------- #
# 7. Differs only in UPDATE / CHECKSIZE / CHECKSUM -> no prompt
# --------------------------------------------------------------------------- #


def test_volatile_only_difference_does_not_prompt(env):
    _seed_link1(env, latest_vne=200)  # latest: 22=200, UPDATE=9, CHECKSIZE=1234
    _put_card_settings(env, vne=200, update=42, checksize=9999, checksum="FFFF")
    _put_demo(env)
    before = _tree(env.archive / "Settings")
    s = _standin(env, alert_result=0)

    _run(env, s)

    assert s.alert_calls == []
    assert _tree(env.archive / "Settings") == before  # content-equal -> dedup
    assert len(_demo_archived(env)) == 1
