# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property test for privileged dd allow-list refusal (image-based-drive-write 5.2).

Feature: image-based-drive-write, Property: privileged allow-list refusal.

Covers task 5.2 (Property family G): the single privilege choke point
``mount_swap.run_privileged`` must

  * REFUSE (raise PrivilegeError, execute NOTHING) when the argv names a binary
    that is not in ``_ALLOWED_BINS``, OR when a ``/bin/dd`` call's device operand
    is NOT a ``/dev/rdiskNsN`` raw partition slice — i.e. a whole-disk raw node
    (``/dev/rdiskN``), a non-raw node (``/dev/diskNsN``), a non-``/dev`` path, or
    zero / two device operands; and

  * PERMIT (reach ``subprocess.run``) a ``/bin/dd`` call carrying exactly one
    ``of=``/``if=`` operand that IS a ``/dev/rdiskNsN`` raw slice, with the other
    side a local file plus arbitrary benign non-device tokens.

This GENERALIZES the example-based dd allow-list cases in
tests/test_mount_swap_privileged.py over Hypothesis-chosen binaries, slice
indices, and benign token sets at >=100 examples.

``mount_swap.subprocess.run`` is mocked (mirroring that file's ``run_mock``) so
accept cases reach it without executing a real dd; it is asserted CALLED for the
accept case and NOT CALLED for every refuse case. The dd path validates the raw
operand syntactically and does NOT call ``_device_info``, so no diskutil mock is
needed here.

Validates: Requirements 8.2, 8.4
"""

from contextlib import contextmanager
from unittest import mock

import pytest
from hypothesis import given, settings, strategies as st

from efis_data_manager import mount_swap as ms


# A small-but-varied space of raw partition slices /dev/rdisk{N}s{M}, built from
# generated integers so the property covers many N/M rather than one fixture.
disk_indices = st.integers(min_value=0, max_value=99)
slice_indices = st.integers(min_value=1, max_value=99)

# Benign non-device dd tokens (block size, counts, flags). None of these name a
# /dev target, so they never affect which operand is the device.
benign_tokens = st.lists(
    st.sampled_from(["bs=1m", "bs=512", "count=16", "count=1", "conv=sync", "status=none"]),
    min_size=0,
    max_size=4,
)

# Local (non-/dev) file paths for the non-device side of a dd pair.
local_paths = st.sampled_from(
    ["/tmp/EFIS_USB.img", "/var/tmp/readback.bin", "/tmp/a.img", "/private/tmp/x.img"]
)

# Disallowed absolute binaries: a fixed sample plus generated /bin/<word> and
# /usr/bin/<word> paths, none of which are in _ALLOWED_BINS.
_word = st.text(
    alphabet=st.characters(min_codepoint=97, max_codepoint=122),  # a-z
    min_size=1,
    max_size=10,
)
disallowed_bins = st.one_of(
    st.sampled_from(["/bin/rm", "/bin/cat", "/usr/bin/tee", "/bin/cp", "/sbin/fsck"]),
    _word.map(lambda w: f"/bin/{w}"),
    _word.map(lambda w: f"/usr/bin/{w}"),
).filter(lambda p: p not in ms._ALLOWED_BINS)


@contextmanager
def _patched_run():
    """Patch mount_swap.subprocess.run for the duration of one generated input.

    A context manager (NOT a function-scoped fixture) so Hypothesis gets a fresh
    mock per example — fixtures are not reset between @given inputs. Mirrors the
    CompletedProcess-shaped return used in tests/test_mount_swap_privileged.py so
    run_privileged can complete normally on an accept path.
    """
    with mock.patch.object(ms.subprocess, "run") as m:
        m.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        yield m


# --- ACCEPT: /bin/dd with exactly one /dev/rdiskNsN operand passes -----------


@settings(max_examples=100)
@given(
    n=disk_indices,
    m=slice_indices,
    local=local_paths,
    extra=benign_tokens,
    direction=st.sampled_from(["write", "read"]),
)
def test_dd_single_raw_slice_operand_reaches_subprocess(n, m, local, extra, direction):
    """/bin/dd with one of=/if= == /dev/rdisk{N}s{M} passes validation and runs."""
    raw = f"/dev/rdisk{n}s{m}"
    if direction == "write":
        # Writing the composed image TO the raw slice: of= is the device.
        argv = [ms.DD_BIN, f"if={local}", f"of={raw}", *extra]
    else:
        # Reading the raw slice back: if= is the device; of= is a local sink.
        argv = [ms.DD_BIN, f"if={raw}", f"of={local}", *extra]

    with _patched_run() as run:
        ms.run_privileged(argv, timeout=60)
        run.assert_called_once()
        passed = run.call_args[0][0]
        assert passed == ["/usr/bin/sudo", "-n", *argv]
        assert isinstance(passed, list)
        assert run.call_args[1].get("shell") is not True


# --- REFUSE: disallowed binary -----------------------------------------------


@settings(max_examples=100)
@given(
    binary=disallowed_bins,
    n=disk_indices,
    m=slice_indices,
    extra=benign_tokens,
)
def test_disallowed_binary_refused(binary, n, m, extra):
    """Any non-whitelisted binary is refused before execution, even with dd-ish args."""
    raw = f"/dev/rdisk{n}s{m}"
    argv = [binary, "if=/tmp/x.img", f"of={raw}", *extra]
    with _patched_run() as run:
        with pytest.raises(ms.PrivilegeError):
            ms.run_privileged(argv, timeout=60)
        run.assert_not_called()


# --- REFUSE: /bin/dd with a device operand that is not a raw slice -----------

# Forms of a BAD dd device target. Each builder takes (n, m) and returns the
# bad token to place in the device operand slot.
_BAD_DEVICE_KINDS = ["whole_disk_raw", "non_raw_slice", "non_dev_path", "whole_disk_nonraw"]


@settings(max_examples=100)
@given(
    n=disk_indices,
    m=slice_indices,
    kind=st.sampled_from(_BAD_DEVICE_KINDS),
    local=local_paths,
    extra=benign_tokens,
)
def test_dd_bad_device_operand_refused(n, m, kind, local, extra):
    """/bin/dd whose of= names something other than /dev/rdiskNsN is refused."""
    if kind == "whole_disk_raw":
        bad = f"/dev/rdisk{n}"  # raw, but no slice -> would clobber partitions
    elif kind == "non_raw_slice":
        bad = f"/dev/disk{n}s{m}"  # buffered node, not the raw character device
    elif kind == "non_dev_path":
        bad = f"{local}.out"  # not under /dev at all -> no /dev target
    else:  # whole_disk_nonraw
        bad = f"/dev/disk{n}"  # non-raw whole disk

    argv = [ms.DD_BIN, f"if={local}", f"of={bad}", *extra]
    with _patched_run() as run:
        with pytest.raises(ms.PrivilegeError):
            ms.run_privileged(argv, timeout=60)
        run.assert_not_called()


# --- REFUSE: /bin/dd with the wrong NUMBER of device operands ----------------


@settings(max_examples=100)
@given(
    n=disk_indices,
    m=slice_indices,
    count=st.sampled_from(["zero", "two"]),
    local=local_paths,
    extra=benign_tokens,
)
def test_dd_wrong_device_operand_count_refused(n, m, count, local, extra):
    """dd must carry EXACTLY one /dev operand; zero or two are refused."""
    if count == "zero":
        # Both sides are local files -> no /dev operand.
        argv = [ms.DD_BIN, f"if={local}", f"of={local}.copy", *extra]
    else:  # two
        # Both sides name raw slices -> two /dev operands.
        argv = [
            ms.DD_BIN,
            f"if=/dev/rdisk{n}s{m}",
            f"of=/dev/rdisk{n}s{m + 1}",
            *extra,
        ]
    with _patched_run() as run:
        with pytest.raises(ms.PrivilegeError):
            ms.run_privileged(argv, timeout=60)
        run.assert_not_called()
