# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Native macOS settings window for EFIS Data Manager.

Uses PyObjC to create a proper multi-field settings panel with browse buttons
and editable frequency fields.
"""

import objc
from AppKit import (
    NSWindow, NSTextField, NSButton, NSOpenPanel, NSApp,
    NSBackingStoreBuffered, NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
    NSMakeRect, NSFont, NSBezelStyleRounded, NSButtonTypeSwitch, NSControlStateValueOn,
    NSControlStateValueOff, NSButtonTypeRadio,
)
from Foundation import NSObject, NSURL

from efis_data_manager.config import (
    PENDING_AIRCRAFT_KEY,
    device_label,
    managed_aircraft,
    managed_mode_s,
    validate_device_config,
)


class SettingsDelegate(NSObject):
    """ObjC delegate that handles button clicks in the settings window."""

    def initWithConfig_onSave_(self, config, on_save):
        self = objc.super(SettingsDelegate, self).init()
        if self is None:
            return None
        self.config = dict(config)
        self.on_save = on_save
        self.window = None
        self.tail_field = None
        self.archive_field = None
        self.usb_image_field = None
        self.charts_freq_field = None
        self.nav_freq_field = None
        self.software_freq_field = None
        # Chart-type checkboxes, keyed by config chart_types key.
        self.chart_type_checkboxes = {}
        # Device-configuration controls (R1, R5). device_count_field is a small
        # editable text field; device_rows is one dict per device slot holding
        # the name field, chart-capable checkbox, and Link_ID field; the
        # analysis-source radio buttons share a single NSButtonCell group.
        self.device_count_field = None
        self.device_rows = []
        self.analysis_source_buttons = []
        return self

    # Display order and labels for the selectable chart types.
    CHART_TYPE_ORDER = [
        ("sectional", "VFR Sectionals"),
        ("ifr_low", "IFR Low Altitude"),
        ("ifr_high", "IFR High Altitude"),
        ("approach_plates", "Approach Plates & Airport Diagrams"),
    ]

    DEFAULT_CHART_TYPES = {
        "sectional": True,
        "ifr_low": True,
        "ifr_high": False,
        "approach_plates": True,
    }

    def buildAndShow(self):
        """Build and display the settings window."""
        # Height grew from 540 to 980 to fit the Device Configuration section
        # (device count, up to 3 per-device rows, and the analysis-source
        # selector). Save/Cancel stay anchored at the bottom (y=20).
        width, height = 600, 980
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(300, 160, width, height),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered,
            False,
        )
        self.window.setTitle_("EFIS Data Manager - Settings")
        self.window.setLevel_(3)
        # Cocoa windows default to being released when closed. That frees the
        # NSWindow out from under the Python delegate (which still holds
        # self.window and its subviews), so a later teardown touches freed
        # memory and crashes with EXC_BAD_ACCESS. Keep the window's lifetime
        # tied to the Python reference instead.
        self.window.setReleasedWhenClosed_(False)

        content = self.window.contentView()
        y = height - 50

        # Tail Number (first item)
        y = self._add_label(content, "Aircraft Tail Number:", 20, y)
        self.tail_field = self._add_text_field(
            content, self.config.get("tail_number", ""), 20, y, 200
        )
        y -= 45

        # Archive Path
        y = self._add_label(content, "Archive Path:", 20, y)
        self.archive_field = self._add_text_field(
            content, self.config["archive_path"], 20, y, 440
        )
        btn = NSButton.alloc().initWithFrame_(NSMakeRect(470, y, 100, 24))
        btn.setTitle_("Browse...")
        btn.setBezelStyle_(NSBezelStyleRounded)
        btn.setTarget_(self)
        btn.setAction_("browseArchive:")
        content.addSubview_(btn)
        y -= 45

        # USB Image Path
        y = self._add_label(content, "USB Image Path (local mirror of EFIS drive):", 20, y)
        self.usb_image_field = self._add_text_field(
            content, self.config["usb_image_path"], 20, y, 440
        )
        btn2 = NSButton.alloc().initWithFrame_(NSMakeRect(470, y, 100, 24))
        btn2.setTitle_("Browse...")
        btn2.setBezelStyle_(NSBezelStyleRounded)
        btn2.setTarget_(self)
        btn2.setAction_("browseUSBImage:")
        content.addSubview_(btn2)
        y -= 55

        # Check Frequencies
        y = self._add_label(content, "Automatic Check Frequencies:", 20, y)
        y -= 5

        lbl1 = self._make_label("Chart data (hours):", 40, y)
        content.addSubview_(lbl1)
        self.charts_freq_field = self._add_text_field(
            content, str(self.config["check_charts_interval_hours"]), 200, y + 2, 50
        )
        y -= 30

        lbl2 = self._make_label("Nav database (hours):", 40, y)
        content.addSubview_(lbl2)
        self.nav_freq_field = self._add_text_field(
            content, str(self.config.get("check_nav_interval_hours", 24)), 200, y + 2, 50
        )
        y -= 30

        lbl3 = self._make_label("EFIS software (hours):", 40, y)
        content.addSubview_(lbl3)
        self.software_freq_field = self._add_text_field(
            content, str(self.config.get("check_software_interval_hours", 24)), 200, y + 2, 50
        )
        y -= 45

        # Chart Types to download (based on your subscription)
        y = self._add_label(content, "Chart Data to Download:", 20, y)
        saved_types = self.config.get("chart_types", self.DEFAULT_CHART_TYPES)
        for key, label in self.CHART_TYPE_ORDER:
            enabled = saved_types.get(key, self.DEFAULT_CHART_TYPES.get(key, False))
            cb = self._add_checkbox(content, label, 40, y, enabled)
            self.chart_type_checkboxes[key] = cb
            y -= 26
        y -= 18

        # Device Configuration (R1, R5) -----------------------------------
        y = self._build_device_section(content, y)

        # Save / Cancel buttons
        cancel_btn = NSButton.alloc().initWithFrame_(NSMakeRect(380, 20, 90, 32))
        cancel_btn.setTitle_("Cancel")
        cancel_btn.setBezelStyle_(NSBezelStyleRounded)
        cancel_btn.setTarget_(self)
        cancel_btn.setAction_("cancelClicked:")
        content.addSubview_(cancel_btn)

        save_btn = NSButton.alloc().initWithFrame_(NSMakeRect(480, 20, 90, 32))
        save_btn.setTitle_("Save")
        save_btn.setBezelStyle_(NSBezelStyleRounded)
        save_btn.setTarget_(self)
        save_btn.setAction_("saveClicked:")
        content.addSubview_(save_btn)

        self.window.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

    def _current_aircraft_record(self):
        """Return the current managed-or-pending aircraft record (or None).

        Prefer the learned managed aircraft; fall back to the pending record
        under PENDING_AIRCRAFT_KEY for an aircraft that has not been learned
        yet. Returns None when nothing is configured (treated as single device).
        """
        record = managed_aircraft(self.config)
        if record is not None:
            return record
        aircraft = self.config.get("aircraft")
        if isinstance(aircraft, dict):
            pending = aircraft.get(PENDING_AIRCRAFT_KEY)
            if isinstance(pending, dict):
                return pending
        return None

    def _build_device_section(self, content, y):
        """Render the Device Configuration section; returns the new y cursor.

        Uses the existing _add_label / _make_label / _add_text_field /
        _add_checkbox idiom and the running y cursor. Pre-fills from the current
        managed-or-pending aircraft record, defaulting to a single device when
        nothing is configured yet. The section is sized for the maximum of 3
        devices so the window height is stable regardless of the current count.
        """
        record = self._current_aircraft_record()
        devices = {}
        if isinstance(record, dict) and isinstance(record.get("devices"), dict):
            devices = record["devices"]
        count = 1
        if isinstance(record, dict) and isinstance(record.get("device_count"), int):
            count = record["device_count"]
        if count < 1:
            count = 1
        if count > 3:
            count = 3
        analysis_source = None
        if isinstance(record, dict):
            analysis_source = record.get("analysis_data_source")

        y = self._add_label(content, "Device Configuration:", 20, y)

        # Device count (1-3), a small editable field matching the frequency
        # fields' idiom (R1.3, R1.4).
        count_lbl = self._make_label("Number of displays (1-3):", 40, y)
        content.addSubview_(count_lbl)
        self.device_count_field = self._add_text_field(
            content, str(count), 240, y + 2, 50
        )
        y -= 34

        # Per-device rows, one per device slot 1..3. Rows beyond the current
        # count are still rendered (so the layout is stable) but pre-filled from
        # whatever is configured; saveClicked_ only reads up to the saved count.
        self.device_rows = []
        for link_id in ("1", "2", "3"):
            device = devices.get(link_id) if isinstance(devices, dict) else None
            device = device if isinstance(device, dict) else {}
            friendly = device.get("friendly_name") or ""
            chart_capable = bool(device.get("chart_capable", True))
            dev_link = device.get("link_id") or link_id

            hdr = self._make_label(f"Device {link_id}:", 40, y)
            hdr.setFont_(NSFont.boldSystemFontOfSize_(12))
            content.addSubview_(hdr)
            y -= 28

            name_lbl = self._make_label(f"Device {link_id} name:", 40, y)
            content.addSubview_(name_lbl)
            name_field = self._add_text_field(content, friendly, 200, y + 2, 240)
            y -= 30

            link_lbl = self._make_label("Link ID:", 40, y)
            content.addSubview_(link_lbl)
            link_field = self._add_text_field(
                content, str(dev_link), 200, y + 2, 50
            )
            chart_cb = self._add_checkbox(
                content, "Chart-capable", 270, y, chart_capable
            )
            y -= 32

            self.device_rows.append(
                {
                    "slot": link_id,
                    "name_field": name_field,
                    "link_field": link_field,
                    "chart_checkbox": chart_cb,
                }
            )

        # Analysis-data-source: single-select over the three device slots. A
        # radio group is clearly single-select; labels come from device_label.
        # No capability gating -- any device is selectable (R1.11, R1.12, R5.3).
        y -= 4
        src_lbl = self._make_label("Analysis data source:", 40, y)
        src_lbl.setFrame_(NSMakeRect(40, y, 300, 20))
        content.addSubview_(src_lbl)
        y -= 26

        self.analysis_source_buttons = []
        for link_id in ("1", "2", "3"):
            device = devices.get(link_id) if isinstance(devices, dict) else None
            if isinstance(device, dict):
                label = device_label(device)
            else:
                label = f"Link {link_id}"
            radio = NSButton.alloc().initWithFrame_(NSMakeRect(60, y, 480, 20))
            radio.setButtonType_(NSButtonTypeRadio)
            radio.setTitle_(label)
            radio.setFont_(NSFont.systemFontOfSize_(12))
            radio.setTarget_(self)
            radio.setAction_("analysisSourceClicked:")
            is_on = (analysis_source == link_id) or (
                analysis_source is None and link_id == "1"
            )
            radio.setState_(
                NSControlStateValueOn if is_on else NSControlStateValueOff
            )
            content.addSubview_(radio)
            self.analysis_source_buttons.append((link_id, radio))
            y -= 24

        y -= 10
        return y

    def _add_label(self, view, text, x, y):
        """Add a bold label, returns new y position."""
        label = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, 560, 20))
        label.setStringValue_(text)
        label.setBezeled_(False)
        label.setEditable_(False)
        label.setDrawsBackground_(False)
        label.setFont_(NSFont.boldSystemFontOfSize_(13))
        view.addSubview_(label)
        return y - 28

    def _make_label(self, text, x, y):
        """Create a non-bold label (doesn't add to view)."""
        label = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, 160, 20))
        label.setStringValue_(text)
        label.setBezeled_(False)
        label.setEditable_(False)
        label.setDrawsBackground_(False)
        label.setFont_(NSFont.systemFontOfSize_(12))
        return label

    def _add_text_field(self, view, value, x, y, width):
        """Add an editable text field."""
        field = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, width, 22))
        field.setStringValue_(value)
        field.setEditable_(True)
        field.setBezeled_(True)
        field.setFont_(NSFont.systemFontOfSize_(12))
        view.addSubview_(field)
        return field

    def _add_checkbox(self, view, title, x, y, checked):
        """Add a checkbox (switch-style button); returns the NSButton."""
        cb = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, 520, 20))
        cb.setButtonType_(NSButtonTypeSwitch)
        cb.setTitle_(title)
        cb.setFont_(NSFont.systemFontOfSize_(12))
        cb.setState_(NSControlStateValueOn if checked else NSControlStateValueOff)
        view.addSubview_(cb)
        return cb

    @objc.IBAction
    def browseArchive_(self, sender):
        path = self._pick_folder(str(self.archive_field.stringValue()))
        if path:
            self.archive_field.setStringValue_(path)

    @objc.IBAction
    def browseUSBImage_(self, sender):
        path = self._pick_folder(str(self.usb_image_field.stringValue()))
        if path:
            self.usb_image_field.setStringValue_(path)

    @objc.IBAction
    def analysisSourceClicked_(self, sender):
        """Enforce single-select across the standalone analysis-source radios.

        These radio buttons are separate NSButtons (not an NSMatrix), so Cocoa
        does not auto-deselect siblings; clear the others here to keep exactly
        one selected (R1.11).
        """
        for _link_id, radio in self.analysis_source_buttons:
            if radio is not sender:
                radio.setState_(NSControlStateValueOff)
        sender.setState_(NSControlStateValueOn)

    def _selected_analysis_source(self):
        """Return the Link_ID of the currently selected analysis-source radio."""
        for link_id, radio in self.analysis_source_buttons:
            if radio.state() == NSControlStateValueOn:
                return link_id
        return None

    @objc.IBAction
    def saveClicked_(self, sender):
        self.config["tail_number"] = str(self.tail_field.stringValue()).strip().upper()
        self.config["archive_path"] = str(self.archive_field.stringValue())
        self.config["usb_image_path"] = str(self.usb_image_field.stringValue())
        chart_types = {}
        for key, cb in self.chart_type_checkboxes.items():
            chart_types[key] = bool(cb.state() == NSControlStateValueOn)
        self.config["chart_types"] = chart_types
        try:
            self.config["check_charts_interval_hours"] = int(self.charts_freq_field.stringValue())
        except (ValueError, TypeError):
            pass
        try:
            self.config["check_nav_interval_hours"] = int(self.nav_freq_field.stringValue())
        except (ValueError, TypeError):
            pass
        try:
            self.config["check_software_interval_hours"] = int(self.software_freq_field.stringValue())
        except (ValueError, TypeError):
            pass
        self._save_device_config()
        self.on_save(self.config)
        self._dismiss()

    def _save_device_config(self):
        """Read the device-config controls, validate, and persist (R1.13, R5.7).

        Builds a ``proposed`` record from the controls, validates it against the
        prior valid record via ``validate_device_config`` (reject-and-retain),
        and stores the validated record under the real Mode_S when one is
        learned, else under PENDING_AIRCRAFT_KEY. On rejection the controls are
        reset to the validated (previous valid) values, mirroring the existing
        "keep previous value on bad input" idiom for the frequency fields, so no
        invalid state is silently persisted.
        """
        # Prior valid record: the managed aircraft, else the pending record,
        # else None (validate_device_config supplies sensible defaults).
        prior = self._current_aircraft_record()

        # Proposed device count from the small text field.
        try:
            proposed_count = int(self.device_count_field.stringValue())
        except (ValueError, TypeError):
            # Non-integer: let validation reject it against the prior count.
            proposed_count = self.device_count_field.stringValue()

        # Proposed per-device records. Only slots up to the proposed count are
        # considered configured; extra rows are ignored so count drives the set.
        effective_count = (
            proposed_count
            if isinstance(proposed_count, int) and 1 <= proposed_count <= 3
            else len(self.device_rows)
        )
        proposed_devices = {}
        for index, row in enumerate(self.device_rows):
            if index >= effective_count:
                break
            link_value = str(row["link_field"].stringValue()).strip()
            name_value = str(row["name_field"].stringValue())
            chart_value = bool(row["chart_checkbox"].state() == NSControlStateValueOn)
            slot = row["slot"]
            proposed_devices[slot] = {
                "link_id": link_value,
                "friendly_name": name_value,
                "chart_capable": chart_value,
            }

        proposed = {
            "device_count": proposed_count,
            "devices": proposed_devices,
            "analysis_data_source": self._selected_analysis_source(),
        }

        record, _rejections = validate_device_config(prior, proposed)

        # Persist under the real Mode_S when learned, else the pending sentinel.
        aircraft = self.config.get("aircraft")
        if not isinstance(aircraft, dict):
            aircraft = {}
            self.config["aircraft"] = aircraft
        mode_s = managed_mode_s(self.config)
        key = mode_s if mode_s is not None else PENDING_AIRCRAFT_KEY
        aircraft[key] = record

        # Reflect the validated (reject-and-retain) values back into the
        # controls so rejected edits do not linger as invalid on-screen state.
        self._reload_device_controls(record)

    def _reload_device_controls(self, record):
        """Reset the device-config controls to a validated record's values."""
        self.device_count_field.setStringValue_(str(record.get("device_count", 1)))
        devices = record.get("devices") if isinstance(record, dict) else None
        devices = devices if isinstance(devices, dict) else {}
        # Map stored devices onto the fixed 1/2/3 row slots by their link_id.
        by_link = {
            str(dev.get("link_id")): dev
            for dev in devices.values()
            if isinstance(dev, dict)
        }
        for row in self.device_rows:
            dev = by_link.get(row["slot"], {})
            row["name_field"].setStringValue_(dev.get("friendly_name") or "")
            row["link_field"].setStringValue_(str(dev.get("link_id") or row["slot"]))
            row["chart_checkbox"].setState_(
                NSControlStateValueOn
                if bool(dev.get("chart_capable", True))
                else NSControlStateValueOff
            )
        selected = record.get("analysis_data_source") if isinstance(record, dict) else None
        for link_id, radio in self.analysis_source_buttons:
            radio.setState_(
                NSControlStateValueOn if link_id == selected else NSControlStateValueOff
            )

    @objc.IBAction
    def cancelClicked_(self, sender):
        self._dismiss()

    def _dismiss(self):
        """Close the window and drop references so the delegate reports closed.

        With releasedWhenClosed=False the NSWindow is not freed by Cocoa on
        close, so it is safe to release our reference here; the window is
        deallocated when the last Python reference (self.window) goes away.
        """
        if self.window is not None:
            self.window.orderOut_(None)
            self.window.close()
            self.window = None

    def is_open(self):
        """True if the settings window is currently on screen."""
        return self.window is not None and bool(self.window.isVisible())

    def focus(self):
        """Bring an already-open settings window to the front."""
        if self.window is not None:
            self.window.makeKeyAndOrderFront_(None)
            NSApp.activateIgnoringOtherApps_(True)

    def _pick_folder(self, current_path):
        """Open a native folder picker."""
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(False)
        panel.setCanChooseDirectories_(True)
        panel.setAllowsMultipleSelection_(False)
        panel.setCanCreateDirectories_(True)
        if current_path:
            panel.setDirectoryURL_(NSURL.fileURLWithPath_(current_path))
        if panel.runModal() == 1:
            return str(panel.URL().path())
        return None


def show_settings(config, on_save):
    """Create and show the settings window.

    Args:
        config: Current config dict.
        on_save: Callback receiving updated config dict on save.
    """
    delegate = SettingsDelegate.alloc().initWithConfig_onSave_(config, on_save)
    delegate.buildAndShow()
    return delegate  # Caller must retain reference to prevent GC
