"""Native macOS settings window for EFIS Data Manager.

Uses PyObjC to create a proper multi-field settings panel with browse buttons
and editable frequency fields.
"""

import objc
from AppKit import (
    NSWindow, NSTextField, NSButton, NSOpenPanel, NSApp,
    NSBackingStoreBuffered, NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
    NSMakeRect, NSFont, NSBezelStyleRounded,
)
from Foundation import NSObject, NSURL


class SettingsDelegate(NSObject):
    """ObjC delegate that handles button clicks in the settings window."""

    def initWithConfig_onSave_(self, config, on_save):
        self = objc.super(SettingsDelegate, self).init()
        if self is None:
            return None
        self.config = dict(config)
        self.on_save = on_save
        self.window = None
        self.archive_field = None
        self.usb_image_field = None
        self.charts_freq_field = None
        self.nav_freq_field = None
        self.software_freq_field = None
        return self

    def buildAndShow(self):
        """Build and display the settings window."""
        width, height = 600, 360
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(300, 200, width, height),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered,
            False,
        )
        self.window.setTitle_("EFIS Data Manager - Settings")
        self.window.setLevel_(3)

        content = self.window.contentView()
        y = height - 50

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
        y -= 50

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
    def saveClicked_(self, sender):
        self.config["archive_path"] = str(self.archive_field.stringValue())
        self.config["usb_image_path"] = str(self.usb_image_field.stringValue())
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
        self.on_save(self.config)
        self.window.close()

    @objc.IBAction
    def cancelClicked_(self, sender):
        self.window.close()

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
