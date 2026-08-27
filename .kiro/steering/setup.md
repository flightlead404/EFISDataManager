# Development Setup Notes

## Post-install / venv rebuild steps

After creating or recreating the virtualenv and installing dependencies,
the Playwright browser binary must be installed separately. `pip install`
does NOT download the browser.

```bash
cd ~/Projects/EFISDataManager
./venv/bin/pip install -r requirements.txt
./venv/bin/playwright install chromium
```

If the Chromium browser is missing, GRT nav DB and EFIS/AHRS software checks
will fail with a clear message: "Playwright browser not installed. Run this
once to fix: ./venv/bin/playwright install chromium".

The browser is cached at `~/Library/Caches/ms-playwright/` (outside the venv),
so it survives venv rebuilds — but a fresh machine or cleared cache requires
re-running `playwright install chromium`.

## Running the components

- Menu bar tool: `./venv/bin/python -m efis_data_manager.app`
- Web dashboard: `./venv/bin/python -m efis_data_manager.dashboard`
  (then open http://localhost:5050)
