# Versioning and Release Policy

When cutting a release for this project, follow this policy:

- Tag the repository with the new release version, e.g. `git tag v0.7.0`.
- Bump `__version__` in `src/efis_data_manager/__init__.py` to match the tag.
- If only the dashboard changed meaningfully, bump `DASHBOARD_VERSION` to reflect
  that in its footer while the menu bar version (`MENUBAR_VERSION`) stays put.
- If only the menu bar tool changed meaningfully, bump `MENUBAR_VERSION` and leave
  `DASHBOARD_VERSION` unchanged.
- Keep `pyproject.toml` `version` in sync with `__version__`.

Principle: one release version for packaging (git tag + `__version__` + pyproject),
with independent display labels (`MENUBAR_VERSION`, `DASHBOARD_VERSION`) for the two
UIs so users can see which component version they are looking at.
