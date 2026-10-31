# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for adopt device selection — resolve-then-prompt and cancel-aborts (R5).

Covers ``EFISDataManagerApp._resolve_adopt_device_link_id`` (task 13.1), which
runs on the MAIN thread as part of the adopt handler and decides the Adopt_Device
BEFORE the background ``_do_adopt_drive`` worker starts (R5.6). Its resolution
order is:

  1. Resolve the drive's own ``Settings.bak`` via
     ``device_resolution.resolve_drive`` (Link_ID SID 387 -> configured device).
     A positively-RESOLVED device WITH a Link_ID is used WITH NO prompt
     (R5.1, R5.2).
  2. Otherwise (no/foreign settings, unresolved, or ambiguous) delegate to the
     Prepare prompt ``_choose_prepare_device_link_id`` (R5.3), which returns
     ``None`` on cancel — the caller then aborts the adopt without touching the
     drive (R5.4).

As with test_chart_capability_gate.py / test_app_write_routing.py, constructing
the full rumps app is impractical headless, so the REAL unbound method is bound
to a minimal ``SimpleNamespace`` stand-in. The logic under test is genuine
production code; only the collaborators (``_find_drive_settings``,
``read_identity``, ``resolve_drive``, and the Prepare prompt) are recorders /
stubs. ``_choose_prepare_device_link_id`` is replaced with a RECORDER that flags
whether it was called, so "no prompt" vs "prompt" is observed directly.
"""

from types import SimpleNamespace
from unittest import mock

from efis_data_manager import app as app_module
from efis_data_manager.app import EFISDataManagerApp
from efis_data_manager.device_resolution import ResolutionStatus, ResolutionOutcome
from efis_data_manager.efis_settings import ParsedBackup, SID_MODE_S, SID_LINK_ID


MODE_S = "A60670"


def _config_two_devices():
    """Managed aircraft with two configured devices (HXr Link 1 + Mini Link 2)."""
    return {
        "aircraft": {
            MODE_S: {
                "device_count": 2,
                "devices": {
                    "1": {"link_id": "1", "friendly_name": "HXr", "chart_capable": True},
                    "2": {"link_id": "2", "friendly_name": "Mini A/P", "chart_capable": False},
                },
                "analysis_data_source": "1",
            }
        }
    }


def _parsed(link_id):
    return ParsedBackup(
        path="/fake/Settings.bak",
        sids={SID_MODE_S: MODE_S, SID_LINK_ID: link_id},
        update_value=1,
        line_count=2,
        valid_pairs=2,
    )


def _make_standin(prompt_returns=None):
    """Build a stand-in with the real method bound and a prompt RECORDER.

    ``prompt_returns`` is the value the Prepare prompt recorder returns when
    invoked. ``standin.prompt_called`` flags whether the fallback prompt ran, so
    tests can assert "no prompt" (RESOLVED path) vs "prompted" (fallback path).
    """
    standin = SimpleNamespace()
    standin.prompt_called = False
    standin._parsed = None

    def _recorder():
        standin.prompt_called = True
        return prompt_returns

    standin._choose_prepare_device_link_id = _recorder
    standin._find_drive_settings = lambda mp: standin._parsed
    # Bind the REAL method under test.
    standin._resolve_adopt_device_link_id = (
        lambda vp: EFISDataManagerApp._resolve_adopt_device_link_id(standin, vp)
    )
    return standin


def _resolved_outcome(link_id):
    """A positively-RESOLVED outcome carrying a Link_ID."""
    return ResolutionOutcome(
        status=ResolutionStatus.RESOLVED,
        mode_s=MODE_S,
        link_id=str(link_id),
        device={"link_id": str(link_id), "friendly_name": "HXr", "chart_capable": True},
        display_label="HXr",
        message="resolved",
    )


def _call_resolve(standin, outcome):
    """Invoke the method with resolve_drive stubbed to return ``outcome``.

    Patches the module-level collaborators the method reaches:
      - ``app_module.load_config`` (config fetch)
      - ``efis_data_manager.drive_updater.read_identity`` (identity read)
      - ``app_module.device_resolution.resolve_drive`` (the resolution verdict)
    ``_find_drive_settings`` and ``_choose_prepare_device_link_id`` are the
    stand-in's own stubs/recorder.
    """
    with mock.patch.object(app_module, "load_config", return_value=_config_two_devices()), \
         mock.patch("efis_data_manager.drive_updater.read_identity", return_value=None), \
         mock.patch.object(app_module.device_resolution, "resolve_drive",
                           return_value=outcome) as resolve_mock:
        result = standin._resolve_adopt_device_link_id("/Volumes/EFIS_X")
    return result, resolve_mock


# ---------------------------------------------------------------------------
# R5.1, R5.2 — a single RESOLVED device is used WITHOUT prompting.
# ---------------------------------------------------------------------------

def test_resolved_single_device_returns_link_id_without_prompt():
    """RESOLVED device with a Link_ID -> that Link_ID, and NO prompt (R5.1, R5.2)."""
    standin = _make_standin(prompt_returns="999")  # would be returned IF prompted
    standin._parsed = _parsed("1")

    result, resolve_mock = _call_resolve(standin, _resolved_outcome("1"))

    assert result == "1"
    assert resolve_mock.called  # resolution was consulted (R5.1)
    assert standin.prompt_called is False, "prompt must not run when resolution succeeds"


def test_resolved_link_id_is_returned_as_string():
    """A RESOLVED Link_ID is normalized to a str (R5.2)."""
    standin = _make_standin(prompt_returns=None)
    standin._parsed = _parsed("2")

    # resolve_drive hands back an int link_id; the method coerces to str.
    outcome = _resolved_outcome(2)
    outcome.link_id = 2  # type: ignore[assignment]

    result, _ = _call_resolve(standin, outcome)

    assert result == "2"
    assert isinstance(result, str)
    assert standin.prompt_called is False


# ---------------------------------------------------------------------------
# R5.3 — unresolved / ambiguous -> delegate to the Prepare prompt.
# ---------------------------------------------------------------------------

def test_unresolved_device_delegates_to_prompt():
    """A non-RESOLVED outcome -> prompt is called and its value returned (R5.3)."""
    standin = _make_standin(prompt_returns="2")
    standin._parsed = None  # foreign / no usable settings

    undetermined = ResolutionOutcome(
        status=ResolutionStatus.DEVICE_UNDETERMINED,
        message="cannot place this drive",
    )
    result, _ = _call_resolve(standin, undetermined)

    assert standin.prompt_called is True, "ambiguous resolution must prompt"
    assert result == "2", "the prompt's chosen Link_ID is returned"


def test_resolved_without_link_id_delegates_to_prompt():
    """RESOLVED but with no Link_ID is not usable -> fall back to the prompt (R5.3)."""
    standin = _make_standin(prompt_returns="1")
    standin._parsed = _parsed("1")

    outcome = _resolved_outcome("1")
    outcome.link_id = None  # resolved shape but missing the Link_ID

    result, _ = _call_resolve(standin, outcome)

    assert standin.prompt_called is True
    assert result == "1"


def test_device_not_configured_delegates_to_prompt():
    """DEVICE_NOT_CONFIGURED (Link_ID matches no device) -> prompt (R5.3)."""
    standin = _make_standin(prompt_returns="2")
    standin._parsed = _parsed("7")  # a Link_ID with no configured device

    outcome = ResolutionOutcome(
        status=ResolutionStatus.DEVICE_NOT_CONFIGURED,
        link_id="7",
        message="no configured device for Link 7",
    )
    result, _ = _call_resolve(standin, outcome)

    assert standin.prompt_called is True
    assert result == "2"


def test_resolution_error_falls_back_to_prompt():
    """If resolution raises, the method swallows it and prompts (R5.3)."""
    standin = _make_standin(prompt_returns="1")
    standin._parsed = _parsed("1")

    with mock.patch.object(app_module, "load_config", return_value=_config_two_devices()), \
         mock.patch("efis_data_manager.drive_updater.read_identity", return_value=None), \
         mock.patch.object(app_module.device_resolution, "resolve_drive",
                           side_effect=RuntimeError("boom")):
        result = standin._resolve_adopt_device_link_id("/Volumes/EFIS_X")

    assert standin.prompt_called is True
    assert result == "1"


# ---------------------------------------------------------------------------
# R5.4 — cancelling the prompt aborts the adopt (None propagates; drive
# untouched). The handler's ``if link_id is None: return`` guard (task 13.1)
# enforces the abort; here we assert the unit-level contract that a cancelled
# prompt yields None and that resolution never fabricates a Link_ID.
# ---------------------------------------------------------------------------

def test_prompt_cancel_returns_none():
    """User cancels the fallback prompt -> None (adopt aborts, drive unmodified) (R5.4)."""
    standin = _make_standin(prompt_returns=None)  # None == user cancelled
    standin._parsed = None

    undetermined = ResolutionOutcome(
        status=ResolutionStatus.DEVICE_UNDETERMINED,
        message="cannot place this drive",
    )
    result, _ = _call_resolve(standin, undetermined)

    assert standin.prompt_called is True
    assert result is None, "a cancelled prompt must return None so the caller aborts"


def test_resolved_path_never_returns_none_without_prompting():
    """The RESOLVED fast path never yields None (only a cancelled prompt does) (R5.4)."""
    standin = _make_standin(prompt_returns=None)
    standin._parsed = _parsed("2")

    result, _ = _call_resolve(standin, _resolved_outcome("2"))

    # Resolution succeeded, so we never reach the (cancellable) prompt.
    assert standin.prompt_called is False
    assert result is not None
    assert result == "2"
