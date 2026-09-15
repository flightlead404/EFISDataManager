# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for the pure/near-pure mount_swap helpers (task 1.2).

Covers the two side-effect-light helpers the mount swap is built from:

  - resolve_device_node(mount_point): parses ``diskutil info -plist`` with
    plistlib and returns the ``DeviceNode`` string. None-safe: returns None on
    non-zero exit, unparseable/garbage stdout, a missing ``DeviceNode`` key, and
    when subprocess.run raises OSError / SubprocessError.
  - private_mountpoint / _derive_private_mountpoint / _sanitize_label: the label
    -> path derivation is pure, stays under PRIVATE_MOUNT_BASE, is distinct per
    label, and sanitizes path-traversal labels down to a single safe segment so
    a derived path can never escape the base. private_mountpoint additionally
    creates the directory 0700.

No physical drive and NO real diskutil are involved: subprocess.run is mocked so
nothing external runs, and PRIVATE_MOUNT_BASE is monkeypatched to a tmp_path so
the filesystem-touching test never writes under /private/tmp.

Requirements: 3.1, 6.4
"""

import os
import plistlib
import stat
import subprocess

import pytest

from efis_data_manager import mount_swap as ms


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _completed(returncode=0, stdout=b""):
    """Build a CompletedProcess as subprocess.run(capture_output=True) returns.

    resolve_device_node calls subprocess.run WITHOUT text=True, so stdout is
    bytes and plistlib.loads is handed raw bytes — matching that here.
    """
    return subprocess.CompletedProcess(
        args=["diskutil"], returncode=returncode, stdout=stdout, stderr=b""
    )


def _plist_bytes(mapping):
    """Serialize a dict to binary-safe plist bytes via plistlib.dumps."""
    return plistlib.dumps(mapping)


@pytest.fixture
def fake_run(monkeypatch):
    """Install a fake subprocess.run into mount_swap and record its calls.

    Returns a small controller: set ``.result`` to a CompletedProcess to return,
    or set ``.raises`` to an exception instance to raise. Guarantees no real
    diskutil ever executes.
    """

    class _Controller:
        def __init__(self):
            self.result = _completed()
            self.raises = None
            self.calls = []

        def __call__(self, argv, *args, **kwargs):
            self.calls.append(argv)
            if self.raises is not None:
                raise self.raises
            return self.result

    controller = _Controller()
    monkeypatch.setattr(ms.subprocess, "run", controller)
    return controller


# ---------------------------------------------------------------------------
# resolve_device_node
# ---------------------------------------------------------------------------


def test_resolve_device_node_returns_device_node(fake_run):
    fake_run.result = _completed(
        stdout=_plist_bytes({"DeviceNode": "/dev/disk4s1", "VolumeName": "EFIS_3"})
    )
    assert ms.resolve_device_node("/Volumes/EFIS_3") == "/dev/disk4s1"


def test_resolve_device_node_invokes_diskutil_info_plist(fake_run):
    fake_run.result = _completed(stdout=_plist_bytes({"DeviceNode": "/dev/disk4s1"}))
    ms.resolve_device_node("/Volumes/EFIS_3")
    # Exactly one call, argv-array (never a shell string), reading the plist.
    assert len(fake_run.calls) == 1
    argv = fake_run.calls[0]
    assert isinstance(argv, list)
    assert argv == [ms.DISKUTIL_BIN, "info", "-plist", "/Volumes/EFIS_3"]


def test_resolve_device_node_none_on_nonzero_returncode(fake_run):
    # Valid plist body, but a non-zero exit means the query failed -> None.
    fake_run.result = _completed(
        returncode=1, stdout=_plist_bytes({"DeviceNode": "/dev/disk4s1"})
    )
    assert ms.resolve_device_node("/Volumes/EFIS_3") is None


def test_resolve_device_node_none_on_unparseable_stdout(fake_run):
    fake_run.result = _completed(stdout=b"this is not a plist at all \x00\xff")
    assert ms.resolve_device_node("/Volumes/EFIS_3") is None


def test_resolve_device_node_none_on_empty_stdout(fake_run):
    fake_run.result = _completed(stdout=b"")
    assert ms.resolve_device_node("/Volumes/EFIS_3") is None


def test_resolve_device_node_none_on_missing_key(fake_run):
    # Well-formed plist, but no DeviceNode key.
    fake_run.result = _completed(
        stdout=_plist_bytes({"VolumeName": "EFIS_3", "FilesystemType": "msdos"})
    )
    assert ms.resolve_device_node("/Volumes/EFIS_3") is None


def test_resolve_device_node_none_on_empty_device_node(fake_run):
    # Key present but empty string is not a usable node -> None.
    fake_run.result = _completed(stdout=_plist_bytes({"DeviceNode": ""}))
    assert ms.resolve_device_node("/Volumes/EFIS_3") is None


def test_resolve_device_node_none_on_non_string_device_node(fake_run):
    fake_run.result = _completed(stdout=_plist_bytes({"DeviceNode": 1234}))
    assert ms.resolve_device_node("/Volumes/EFIS_3") is None


def test_resolve_device_node_none_on_oserror(fake_run):
    fake_run.raises = OSError("diskutil not found")
    assert ms.resolve_device_node("/Volumes/EFIS_3") is None


def test_resolve_device_node_none_on_subprocess_error(fake_run):
    fake_run.raises = subprocess.SubprocessError("boom")
    assert ms.resolve_device_node("/Volumes/EFIS_3") is None


def test_resolve_device_node_none_on_timeout_expired(fake_run):
    # TimeoutExpired is a SubprocessError subclass -> swallowed to None.
    fake_run.raises = subprocess.TimeoutExpired(cmd="diskutil", timeout=10)
    assert ms.resolve_device_node("/Volumes/EFIS_3") is None


# ---------------------------------------------------------------------------
# _sanitize_label
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label, expected",
    [
        ("EFIS_3", "EFIS_3"),
        ("../../etc/passwd", "passwd"),  # basename drops the traversal prefix
        ("a/b", "b"),
        ("..", "volume"),  # bare parent-dir marker -> safe fallback
        (".", "volume"),
        ("", "volume"),
        ("/", "volume"),  # basename("/") is "" -> fallback
        ("///", "volume"),
        ("a\\b", "ab"),  # residual backslash separators stripped
        ("  EFIS_3  ", "EFIS_3"),  # surrounding whitespace stripped
    ],
)
def test_sanitize_label_reduces_to_single_safe_segment(label, expected):
    assert ms._sanitize_label(label) == expected


def test_sanitize_label_never_contains_separators():
    for label in ["../../etc/passwd", "a/b/c", "x\\y\\z", "/abs/path", ".."]:
        segment = ms._sanitize_label(label)
        assert "/" not in segment
        assert "\\" not in segment
        assert segment not in ("", ".", "..")


def test_sanitize_label_handles_non_string():
    # Defensive: a non-string label must not raise, falls back to "volume".
    assert ms._sanitize_label(None) == "volume"


# ---------------------------------------------------------------------------
# _derive_private_mountpoint (pure)
# ---------------------------------------------------------------------------


def test_derive_private_mountpoint_under_base(monkeypatch, tmp_path):
    base = str(tmp_path / "efis-datamanager")
    monkeypatch.setattr(ms, "PRIVATE_MOUNT_BASE", base)
    path = ms._derive_private_mountpoint("EFIS_3")
    assert path == os.path.join(base, "EFIS_3")


def test_derive_distinct_labels_distinct_paths(monkeypatch, tmp_path):
    base = str(tmp_path / "efis-datamanager")
    monkeypatch.setattr(ms, "PRIVATE_MOUNT_BASE", base)
    p1 = ms._derive_private_mountpoint("EFIS_3")
    p2 = ms._derive_private_mountpoint("EFIS_4")
    assert p1 != p2
    assert os.path.dirname(p1) == os.path.dirname(p2) == os.path.normpath(base)


@pytest.mark.parametrize(
    "hostile", ["../../etc/passwd", "a/b", "..", "", "/", "///", "..\\..\\x"]
)
def test_derive_traversal_labels_stay_under_base(monkeypatch, tmp_path, hostile):
    """A path-traversal label can never produce a path outside the base."""
    base = str(tmp_path / "efis-datamanager")
    monkeypatch.setattr(ms, "PRIVATE_MOUNT_BASE", base)
    derived = os.path.normpath(ms._derive_private_mountpoint(hostile))
    norm_base = os.path.normpath(base)
    # Strict descendant: the derived path is one segment under the base.
    assert os.path.dirname(derived) == norm_base
    assert os.path.commonpath([norm_base, derived]) == norm_base
    assert derived != norm_base


def test_derive_reads_module_global(monkeypatch, tmp_path):
    """_derive_private_mountpoint reads the module-global PRIVATE_MOUNT_BASE.

    Patching the attribute on the module must change the derived path, proving
    the derivation is not bound to a stale import-time constant.
    """
    base_a = str(tmp_path / "base_a")
    monkeypatch.setattr(ms, "PRIVATE_MOUNT_BASE", base_a)
    assert ms._derive_private_mountpoint("EFIS_3") == os.path.join(base_a, "EFIS_3")

    base_b = str(tmp_path / "base_b")
    monkeypatch.setattr(ms, "PRIVATE_MOUNT_BASE", base_b)
    assert ms._derive_private_mountpoint("EFIS_3") == os.path.join(base_b, "EFIS_3")


# ---------------------------------------------------------------------------
# private_mountpoint (creates the dir 0700)
# ---------------------------------------------------------------------------


def test_private_mountpoint_creates_dir_0700(monkeypatch, tmp_path):
    base = str(tmp_path / "efis-datamanager")
    monkeypatch.setattr(ms, "PRIVATE_MOUNT_BASE", base)

    path = ms.private_mountpoint("EFIS_3")

    assert path == os.path.join(base, "EFIS_3")
    assert os.path.isdir(path)
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o700


def test_private_mountpoint_idempotent_and_tightens_permissions(monkeypatch, tmp_path):
    base = str(tmp_path / "efis-datamanager")
    monkeypatch.setattr(ms, "PRIVATE_MOUNT_BASE", base)

    # Pre-create the dir with loose perms; private_mountpoint must tighten it.
    path = os.path.join(base, "EFIS_3")
    os.makedirs(path, mode=0o755)
    os.chmod(path, 0o755)

    returned = ms.private_mountpoint("EFIS_3")
    assert returned == path
    assert os.path.isdir(path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o700


def test_private_mountpoint_stays_under_base_for_hostile_label(monkeypatch, tmp_path):
    base = str(tmp_path / "efis-datamanager")
    monkeypatch.setattr(ms, "PRIVATE_MOUNT_BASE", base)

    path = os.path.normpath(ms.private_mountpoint("../../etc/passwd"))
    norm_base = os.path.normpath(base)
    assert os.path.dirname(path) == norm_base
    assert os.path.commonpath([norm_base, path]) == norm_base
    # Nothing was created outside the base.
    assert os.path.isdir(path)
