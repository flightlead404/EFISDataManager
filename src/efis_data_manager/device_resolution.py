# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Drive-insert resolution / classification state machine (multi-device-settings).

This module holds the *pure*, flight-critical decision logic that places an
inserted drive against the managed aircraft's configured devices. It is kept
deliberately free of side effects — no rumps prompts, no I/O, no ``save_config``,
no eject, no archiving — so the entire decision table is unit- and
property-testable with plain dicts. ``app.py`` wraps :func:`resolve_drive` with
the actual notify / leave-mounted / proceed side effects.

The guiding rules (see design.md "Resolution algorithm"):

- **Non-destructive on uncertainty.** Any "I can't confidently place this drive"
  branch returns a terminal classification; the caller logs, notifies, and
  leaves the drive mounted (R2.4, R2.6, R3.5, R3.6).
- **Aircraft verification gates device resolution.** A drive whose Mode_S cannot
  be derived, or does not match the managed aircraft, never resolves to a device
  (R3.1, R3.4, R3.5, R3.6).
- **Settings-file Link_ID is authoritative.** A recorded ``device_link_id`` in
  the per-drive identity file is only a fallback for when no readable settings
  file (or no settings Link_ID) is present (R2.2, R4.5, R4.6).

Validates: Requirements 2.1, 2.2, 2.3, 2.5, 2.6, 3.1, 3.2, 3.4, 3.5, 3.6, 4.1,
4.5, 4.6.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional, TYPE_CHECKING

from .config import configured_device, device_label, managed_mode_s
from .efis_settings import extract_source_key

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids any runtime coupling
    from .efis_settings import ParsedBackup


class ResolutionStatus(Enum):
    """The kind of situation an inserted drive presents."""

    RESOLVED = "resolved"                            # device identified; proceed (R2.3, R3.4, R4.1, R4.5)
    LEARN_PROMPT = "learn_prompt"                     # no managed aircraft yet; confirm Mode_S (R3.2)
    AIRCRAFT_UNRECOGNIZED = "aircraft_unrecognized"   # Mode_S present but != managed (R3.5)
    AIRCRAFT_UNDETERMINED = "aircraft_undetermined"   # Mode_S not derivable (R3.6)
    DEVICE_NOT_CONFIGURED = "device_not_configured"   # Link_ID matches no configured device (R2.4)
    DEVICE_UNDETERMINED = "device_undetermined"       # no settings + no usable recorded binding (R2.6)


@dataclass
class ResolutionOutcome:
    """The verdict produced by :func:`resolve_drive`.

    ``device`` and ``display_label`` are populated only for ``RESOLVED``.
    ``binding_mismatch`` / ``recorded_link_id`` are set when the authoritative
    settings-file Link_ID differs from a recorded ``device_link_id`` (R4.6); the
    outcome still resolves to the settings-file device.
    """

    status: ResolutionStatus
    mode_s: Optional[str] = None            # derived Mode_S, when present
    link_id: Optional[str] = None           # authoritative Link_ID used for resolution
    device: Optional[dict] = None           # the resolved Device_Config (RESOLVED only)
    display_label: Optional[str] = None     # device label for notifications (RESOLVED only)
    binding_mismatch: bool = False          # settings Link_ID != recorded device_link_id (R4.6)
    recorded_link_id: Optional[str] = None  # the recorded device_link_id, for the mismatch message
    message: str = ""                       # human-readable rationale / notification body


def _recorded_link_id(recorded_identity: Optional[dict]) -> Optional[str]:
    """Return the recorded ``device_link_id`` binding, or None.

    Tolerates a missing identity file (``None``) and a v1 identity file that
    predates the ``device_link_id`` field (``.get`` returns ``None``), per the
    identity-file backward-read compatibility argument (R6.6).
    """
    if not isinstance(recorded_identity, dict):
        return None
    return recorded_identity.get("device_link_id")


def _resolve_via_recorded_binding(
    recorded_identity: Optional[dict],
    config: dict,
) -> ResolutionOutcome:
    """Fallback branch: place the drive by its recorded ``device_link_id``.

    Reached when there is no readable settings file, or a readable settings file
    carried no Link_ID SID. Does NOT verify Mode_S: a data-less drive carries no
    settings to derive it from, and this is the intended fallback for rotating
    prepared drives (R3.1 derives Mode_S only for populated drives).
    """
    recorded_link = _recorded_link_id(recorded_identity)
    if recorded_link is not None:
        device = configured_device(config, recorded_link)
        if device is not None:
            label = device_label(device)
            return ResolutionOutcome(
                status=ResolutionStatus.RESOLVED,
                link_id=recorded_link,
                device=device,
                display_label=label,
                message=(
                    f"Resolved to {label} from the drive's recorded binding "
                    f"(no settings Link_ID present)."
                ),
            )
    # No settings Link_ID and no configured recorded binding: cannot place it.
    return ResolutionOutcome(
        status=ResolutionStatus.DEVICE_UNDETERMINED,
        recorded_link_id=recorded_link,
        message=(
            "Could not determine which device this drive belongs to: no "
            "settings Link_ID and no recognized recorded device binding. "
            "Leaving the drive mounted and taking no action."
        ),
    )


def resolve_drive(
    parsed: "Optional[ParsedBackup]",
    recorded_identity: Optional[dict],
    config: dict,
) -> ResolutionOutcome:
    """Classify an inserted drive and resolve it to a configured device.

    Pure and deterministic: for fixed inputs it always returns the same status
    and resolved device, and it performs no side effects.

    Args:
        parsed: the parsed settings backup, or ``None`` when no readable
            settings file is present on the drive.
        recorded_identity: the dict from ``read_identity(mount_point)``, or
            ``None`` when the drive has no (readable) identity file.
        config: the full app config holding the ``aircraft`` map.

    Returns:
        A :class:`ResolutionOutcome` describing what kind of situation this is
        and which device (if any) was resolved.
    """
    # --- Identity branch: a readable settings file is present (R2.1). ---------
    if parsed is not None:
        identity = extract_source_key(parsed)
        mode_s = identity.mode_s
        link_id = identity.link_id

        # (a) Mode_S undetermined dominates: never place a drive whose aircraft
        #     we cannot establish, even if a Link_ID is present (R3.6).
        if mode_s is None:
            return ResolutionOutcome(
                status=ResolutionStatus.AIRCRAFT_UNDETERMINED,
                link_id=link_id,
                message=(
                    "Could not determine the aircraft for this drive: the "
                    "settings file has no Mode_S address. Leaving the drive "
                    "mounted and taking no action."
                ),
            )

        managed = managed_mode_s(config)

        # (b) No managed aircraft established yet: prompt to learn it (R3.2).
        if managed is None:
            return ResolutionOutcome(
                status=ResolutionStatus.LEARN_PROMPT,
                mode_s=mode_s,
                link_id=link_id,
                message=(
                    f"No managed aircraft is set yet. This drive reports "
                    f"Mode_S {mode_s}. Confirm to adopt it as the managed "
                    f"aircraft."
                ),
            )

        # (c) Mode_S matches the managed aircraft: resolve device by Link_ID,
        #     with the settings-file Link_ID authoritative (R3.4, R2.2).
        if mode_s == managed:
            if link_id is not None:
                device = configured_device(config, link_id)
                if device is not None:
                    label = device_label(device)
                    recorded_link = _recorded_link_id(recorded_identity)
                    mismatch = (
                        recorded_link is not None and recorded_link != link_id
                    )
                    if mismatch:
                        message = (
                            f"Resolved to {label} from the settings-file "
                            f"Link_ID {link_id} (authoritative). Note: the "
                            f"drive's recorded binding was Link {recorded_link}."
                        )
                    else:
                        message = (
                            f"Resolved to {label} from the settings-file "
                            f"Link_ID {link_id}."
                        )
                    return ResolutionOutcome(
                        status=ResolutionStatus.RESOLVED,
                        mode_s=mode_s,
                        link_id=link_id,
                        device=device,
                        display_label=label,
                        binding_mismatch=mismatch,
                        recorded_link_id=recorded_link,
                        message=message,
                    )
                # Link_ID present but matches no configured device (R2.4).
                return ResolutionOutcome(
                    status=ResolutionStatus.DEVICE_NOT_CONFIGURED,
                    mode_s=mode_s,
                    link_id=link_id,
                    message=(
                        f"This drive's settings report Link_ID {link_id}, which "
                        f"is not a configured device for the managed aircraft. "
                        f"Leaving the drive mounted and taking no action."
                    ),
                )
            # Settings readable but no Link_ID SID: a readable settings file
            # without a Link_ID cannot place the device itself; fall through to
            # the recorded-binding branch.
            return _resolve_via_recorded_binding(recorded_identity, config)

        # (d) Mode_S present but does not match the managed aircraft (R3.5).
        return ResolutionOutcome(
            status=ResolutionStatus.AIRCRAFT_UNRECOGNIZED,
            mode_s=mode_s,
            link_id=link_id,
            message=(
                f"This drive belongs to aircraft {mode_s}, which is not the "
                f"managed aircraft ({managed}). Leaving the drive mounted and "
                f"taking no action."
            ),
        )

    # --- Recorded-binding branch: no readable settings file (R2.5). -----------
    return _resolve_via_recorded_binding(recorded_identity, config)
