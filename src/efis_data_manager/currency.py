"""Currency management for Seattle Avionics charts and GRT software/nav DB.

Handles:
- Seattle Avionics login (ASP.NET WebForms with ViewState)
- Download table parsing (Installation.aspx)
- Chart zip download and extraction
- GRT software/nav database version checking
- Cycle comparison to detect new updates
"""

import json
import logging
import os
import re
import subprocess
import zipfile
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

from efis_data_manager.config import load_config, APP_SUPPORT_DIR

logger = logging.getLogger(__name__)

# Seattle Avionics URLs
SA_LOGIN_URL = "https://seattleavionics.com/ChartData/default.aspx?TargetDevice=GRT"
SA_INSTALL_URL = "https://seattleavionics.com/ChartData/Installation.aspx"

# GRT Avionics URLs
GRT_HXR_SOFTWARE_URL = "https://grtavionics.com/horizon-hxr-software/"
GRT_MINIAP_SOFTWARE_URL = "https://grtavionics.com/mini-x-ap-software/"
GRT_NAV_DB_URL = "https://grtavionics.com/navigation-database-updates/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

# Excluded chart types (not needed for this aircraft)
EXCLUDED_SUBSTRINGS = ["high altitude"]

# Local metadata file tracking what cycles we have
CYCLE_METADATA_FILE = APP_SUPPORT_DIR / "chart_cycles.json"

# Target directory mapping for chart extraction (relative to USB image root)
CHART_TARGET_DIRS = {
    "ifr low": "ChartData",
    "sectional": "ChartData",
    "scanned charts": "ChartData",
    "approach plates": "ChartData/Plates",
    "airport diagrams": "ChartData/Plates",
}


class CurrencyError(Exception):
    """Base exception for currency-related errors."""
    pass


class LoginError(CurrencyError):
    """Login to Seattle Avionics failed."""
    pass


class PageLayoutChangedError(CurrencyError):
    """The scrape target page has changed layout — needs manual intervention."""
    pass


# ---------------------------------------------------------------------------
# Keychain helpers
# ---------------------------------------------------------------------------

def get_sa_credentials() -> tuple[str, str]:
    """Retrieve Seattle Avionics credentials from macOS Keychain.

    Returns:
        Tuple of (email, password).

    Raises:
        CurrencyError if credentials not found in Keychain.
    """
    try:
        # Get account name (email)
        result = subprocess.run(
            ["security", "find-generic-password",
             "-s", "EFISDataManager-SeattleAvionics", "-g"],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            raise CurrencyError(
                "Seattle Avionics credentials not found in Keychain. "
                "Use the menu bar app to set them via 'Seattle Avionics Login...'."
            )

        email = ""
        password = ""
        for line in result.stdout.splitlines():
            if '"acct"' in line:
                # Extract value between last pair of quotes
                match = re.search(r'"acct"<blob>="([^"]*)"', line)
                if match:
                    email = match.group(1)

        # Password is in stderr with security -g
        for line in result.stderr.splitlines():
            if line.startswith("password:"):
                match = re.search(r'"([^"]*)"', line)
                if match:
                    password = match.group(1)

        if not email or not password:
            raise CurrencyError("Could not parse credentials from Keychain.")

        return email, password

    except FileNotFoundError:
        raise CurrencyError("'security' command not found — not running on macOS?")


# ---------------------------------------------------------------------------
# Seattle Avionics login
# ---------------------------------------------------------------------------

def _get_form_fields(html: str) -> tuple[dict, str]:
    """Extract all input fields from the first <form> on the page."""
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form")
    if not form:
        raise PageLayoutChangedError("No <form> found on login page.")
    fields = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if name:
            fields[name] = inp.get("value", "")
    action = form.get("action") or SA_LOGIN_URL
    return fields, action


def sa_login() -> requests.Session:
    """Log in to Seattle Avionics and return an authenticated session.

    Raises:
        LoginError if credentials are rejected.
        PageLayoutChangedError if the page structure is unexpected.
        CurrencyError if credentials aren't in Keychain.
    """
    email, password = get_sa_credentials()

    session = requests.Session()
    resp = session.get(SA_LOGIN_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()

    fields, action = _get_form_fields(resp.text)

    # Find username and password fields by name pattern
    username_field = next(
        (f for f in fields if "user" in f.lower() or "email" in f.lower()), None
    )
    password_field = next((f for f in fields if "pass" in f.lower()), None)

    if not username_field or not password_field:
        raise PageLayoutChangedError(
            f"Cannot identify login fields. Fields found: {list(fields.keys())}"
        )

    fields[username_field] = email
    fields[password_field] = password

    post_url = action if action.startswith("http") else SA_LOGIN_URL
    login_resp = session.post(post_url, data=fields, headers=HEADERS, timeout=30)

    # Verify login succeeded by checking Installation.aspx
    install_resp = session.get(SA_INSTALL_URL, headers=HEADERS, timeout=30)
    lowered = install_resp.text.lower()

    if "download" in lowered and "password" in lowered and "login" not in lowered[:500]:
        logger.info("Seattle Avionics login succeeded.")
        return session
    else:
        raise LoginError("Seattle Avionics login failed — credentials rejected or session issue.")


# ---------------------------------------------------------------------------
# Download table parsing
# ---------------------------------------------------------------------------

def parse_download_table(session: requests.Session) -> list[dict]:
    """Fetch Installation.aspx and parse the chart download table.

    Returns:
        List of dicts with keys: description, region, valid_dates, password, download_url.
        De-duplicated by URL (page renders table multiple times).

    Raises:
        PageLayoutChangedError if no download rows can be parsed.
    """
    resp = session.get(SA_INSTALL_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    rows_out = []
    tables = soup.find_all("table")

    for table in tables:
        for tr in table.find_all("tr"):
            link = tr.find("a")
            if not link or not link.get("href"):
                continue
            link_text = link.get_text(strip=True).lower()
            if "download" not in link_text and "zip" not in (link.get("href") or "").lower():
                continue

            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            href = link["href"]
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = "https://seattleavionics.com" + href
            elif not href.startswith("http"):
                href = "https://seattleavionics.com/ChartData/" + href

            row = {
                "description": cells[0] if len(cells) > 0 else "",
                "region": cells[1] if len(cells) > 1 else "",
                "valid_dates": cells[2] if len(cells) > 2 else "",
                "password": cells[3] if len(cells) > 3 else "",
                "download_url": href,
            }
            rows_out.append(row)

    if not rows_out:
        raise PageLayoutChangedError(
            "No download rows parsed from Installation.aspx. "
            f"Found {len(tables)} table(s) but none contained recognizable download links. "
            "The page layout may have changed."
        )

    # De-duplicate by URL (page renders table multiple times)
    seen = set()
    deduped = []
    for row in rows_out:
        if row["download_url"] in seen:
            continue
        seen.add(row["download_url"])
        deduped.append(row)

    # Filter out excluded types
    filtered = [
        r for r in deduped
        if not any(sub in r["description"].lower() for sub in EXCLUDED_SUBSTRINGS)
    ]

    logger.info(
        f"Parsed {len(deduped)} chart entries ({len(deduped) - len(filtered)} excluded), "
        f"{len(filtered)} in scope."
    )
    return filtered


# ---------------------------------------------------------------------------
# Cycle comparison
# ---------------------------------------------------------------------------

def load_cycle_metadata() -> dict:
    """Load locally stored cycle metadata (what we already have downloaded)."""
    if CYCLE_METADATA_FILE.exists():
        try:
            with open(CYCLE_METADATA_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_cycle_metadata(metadata: dict):
    """Save cycle metadata to disk."""
    APP_SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    with open(CYCLE_METADATA_FILE, "w") as f:
        json.dump(metadata, f, indent=2)


def check_for_new_charts(entries: list[dict]) -> list[dict]:
    """Compare parsed chart entries against local metadata to find new/updated cycles.

    Returns:
        List of entries that are new or have changed valid_dates/password.
    """
    metadata = load_cycle_metadata()
    new_entries = []

    for entry in entries:
        url = entry["download_url"]
        stored = metadata.get(url, {})
        if (stored.get("valid_dates") != entry["valid_dates"] or
                stored.get("password") != entry["password"]):
            new_entries.append(entry)

    if new_entries:
        logger.info(f"{len(new_entries)} chart update(s) available.")
    else:
        logger.info("All charts are current.")

    return new_entries


# ---------------------------------------------------------------------------
# Download and extraction
# ---------------------------------------------------------------------------

def _determine_extract_dir(description: str, usb_image_path: str) -> str:
    """Determine the target extraction directory based on chart description.

    MultiDiskImg (LO/SEC) zips go to ChartData root — the extract_chart
    function handles routing individual files to LO/ or SEC/ subdirs.
    """
    desc_lower = description.lower()

    # Plates go to Plates subdirectory
    if "approach plates" in desc_lower or "airport diagrams" in desc_lower:
        if "geo" in desc_lower:
            return os.path.join(usb_image_path, "ChartData", "Plates")
        else:
            return os.path.join(usb_image_path, "ChartData", "Plates")

    # Everything else (LO, SEC, ScannedCharts) goes to ChartData root
    # MultiDiskImg extraction handles LO/SEC subdirectory routing internally
    return os.path.join(usb_image_path, "ChartData")


def download_chart(session: requests.Session, entry: dict, download_dir: str,
                   progress_callback=None) -> str:
    """Download a chart zip file with resume support.

    If a partial file exists from a previous interrupted download, resumes
    from where it left off using HTTP Range requests.

    Args:
        session: Authenticated requests session.
        entry: Chart entry dict from parse_download_table.
        download_dir: Directory to save the zip file.
        progress_callback: Optional callable(filename, pct) called during download.

    Returns:
        Path to the downloaded zip file.
    """
    os.makedirs(download_dir, exist_ok=True)
    filename = entry["download_url"].split("/")[-1]
    zip_path = os.path.join(download_dir, filename)

    # Check for existing partial download
    existing_size = 0
    if os.path.exists(zip_path):
        existing_size = os.path.getsize(zip_path)

    # Set up headers for resume
    dl_headers = dict(HEADERS)
    if existing_size > 0:
        dl_headers["Range"] = f"bytes={existing_size}-"
        logger.info(f"Resuming {filename} from {existing_size / (1024*1024):.1f} MB...")
    else:
        logger.info(f"Downloading {filename} from {entry['download_url']}...")

    resp = session.get(entry["download_url"], headers=dl_headers, stream=True, timeout=1800)

    # Handle resume response
    if resp.status_code == 416:
        # Range not satisfiable — file is already complete
        logger.info(f"{filename} already fully downloaded ({existing_size / (1024*1024):.1f} MB).")
        return zip_path
    elif resp.status_code == 206:
        mode = "ab"
        total_bytes = existing_size
        content_range = resp.headers.get("Content-Range", "")
        # Content-Range: bytes 12345-67890/total
        total_size = int(content_range.split("/")[-1]) if "/" in content_range else None
        logger.info(f"Server accepted resume for {filename}.")
    elif resp.status_code == 200:
        mode = "wb"
        total_bytes = 0
        total_size = int(resp.headers.get("Content-Length", 0)) or None
        if existing_size > 0:
            logger.info(f"Server sent full file (no resume support), restarting {filename}.")
    else:
        resp.raise_for_status()
        mode = "wb"
        total_bytes = 0
        total_size = None

    last_pct_reported = -1
    with open(zip_path, mode) as f:
        for chunk in resp.iter_content(chunk_size=256 * 1024):
            f.write(chunk)
            total_bytes += len(chunk)

            # Report progress every ~5%
            if total_size and progress_callback:
                pct = int(total_bytes * 100 / total_size)
                if pct >= last_pct_reported + 5:
                    last_pct_reported = pct
                    progress_callback(filename, pct)

    logger.info(f"Downloaded {filename}: {total_bytes / (1024*1024):.1f} MB total")
    return zip_path


def extract_chart(zip_path: str, entry: dict, target_dir: str) -> int:
    """Extract a chart zip into the target directory.

    Handles different zip types:
    - MultiDiskImg: Routes files to LO/ or SEC/ subdirs based on filename.
    - Plates PNG: Routes to country subdirs (US/, MM/, etc.) using unzip command.
    - Plates GEO: Extracts metadata files to Plates root using unzip command.
    - Other: Standard extraction via zipfile/pyzipper, falling back to unzip.

    Args:
        zip_path: Path to the downloaded zip file.
        entry: Chart entry dict (for password).
        target_dir: Base directory to extract into (e.g. ChartData root or Plates).

    Returns:
        Number of files extracted.

    Raises:
        CurrencyError on extraction failure.
    """
    os.makedirs(target_dir, exist_ok=True)
    pwd = entry["password"].encode() if entry["password"] else None
    pwd_str = entry["password"] if entry["password"] else None
    basename = os.path.basename(zip_path).lower()
    is_multidisk = "multidiskimg" in basename
    is_plates_png = "plates" in basename and "geo" not in basename and "png" in basename
    is_plates_geo = "plates" in basename and "geo" in basename

    if is_multidisk:
        return _extract_multidisk(zip_path, pwd, target_dir)
    elif is_plates_png:
        return _extract_plates_png(zip_path, pwd_str, target_dir)
    elif is_plates_geo:
        return _extract_with_unzip(zip_path, pwd_str, target_dir)
    else:
        return _extract_simple(zip_path, pwd, target_dir)


def _extract_simple(zip_path: str, pwd: Optional[bytes], target_dir: str) -> int:
    """Standard extraction — extract all files directly into target_dir.

    Tries zipfile, then pyzipper, then falls back to system unzip command.
    """
    # Try standard zipfile first
    try:
        with zipfile.ZipFile(zip_path) as zf:
            bad_file = zf.testzip()
            if bad_file:
                raise zipfile.BadZipFile(f"CRC check failed for {bad_file}")
            zf.extractall(path=target_dir, pwd=pwd)
            n_files = len(zf.namelist())
            logger.info(f"Extracted {n_files} file(s) to {target_dir}")
            return n_files
    except (NotImplementedError, RuntimeError):
        logger.info(f"Standard zipfile can't handle {zip_path}, trying pyzipper (AES)...")
        pass
    except zipfile.BadZipFile as e:
        logger.info(f"zipfile reports bad zip ({e}), will try unzip command...")
        pwd_str = pwd.decode() if pwd else None
        return _extract_with_unzip(zip_path, pwd_str, target_dir)

    # Fallback: pyzipper for AES
    try:
        import pyzipper
        with pyzipper.AESZipFile(zip_path) as zf:
            zf.extractall(path=target_dir, pwd=pwd)
            n_files = len(zf.namelist())
            logger.info(f"Extracted {n_files} file(s) via pyzipper (AES) to {target_dir}")
            return n_files
    except ImportError:
        raise CurrencyError(
            f"Chart zip uses AES encryption and pyzipper is not installed. "
            f"Run: pip install pyzipper"
        )
    except Exception as e:
        # Last resort: try system unzip
        logger.info(f"pyzipper failed ({e}), falling back to unzip command...")
        pwd_str = pwd.decode() if pwd else None
        return _extract_with_unzip(zip_path, pwd_str, target_dir)


def _extract_with_unzip(zip_path: str, pwd_str: Optional[str], target_dir: str) -> int:
    """Extract a zip using the system unzip command.

    Handles zips with non-standard preambles (e.g. 2MB headers from Seattle
    Avionics) that Python's zipfile/pyzipper can't process.

    Args:
        zip_path: Path to the zip file.
        pwd_str: Password as string (or None).
        target_dir: Directory to extract into.

    Returns:
        Number of files extracted.
    """
    os.makedirs(target_dir, exist_ok=True)

    cmd = ["unzip", "-o", "-q"]
    if pwd_str:
        cmd.extend(["-P", pwd_str])
    cmd.extend([zip_path, "-d", target_dir])

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)

    # unzip exit codes: 0=success, 1=finished with warnings, 2=preamble/offset warnings
    # Only codes >= 3 are actual extraction failures
    if result.returncode >= 3:
        raise CurrencyError(
            f"unzip failed for {zip_path} (exit {result.returncode}): "
            f"{result.stderr.strip()[:200]}"
        )

    # Count extracted files
    n_files = sum(1 for line in result.stdout.splitlines() if "inflating:" in line or "extracting:" in line)
    if n_files == 0:
        # Fallback: count files in target_dir
        n_files = sum(1 for _, _, files in os.walk(target_dir) for _ in files)

    logger.info(f"Extracted {n_files} file(s) via unzip to {target_dir}")
    return n_files


# ICAO region prefixes for international plates (Central America/Mexico/Caribbean)
_PLATES_INTL_PREFIXES = ("MG", "MH", "MM", "MN", "MR", "MS", "MZ")


def _extract_plates_png(zip_path: str, pwd_str: Optional[str], plates_dir: str) -> int:
    """Extract Plates PNG zip, routing files to country subdirectories.

    The Plates PNG zip contains all files flat at the root. Files are routed to:
    - Plates/US/ for US plates (filenames starting with digits or non-ICAO prefixes)
    - Plates/<CC>/ for international plates (filenames starting with ICAO region prefix)

    Uses the system unzip command to handle non-standard zip preambles.

    Args:
        zip_path: Path to the plates PNG zip.
        pwd_str: Password as string.
        plates_dir: Target Plates directory (e.g. .../ChartData/Plates).

    Returns:
        Number of files extracted and routed.
    """
    import shutil
    import tempfile

    # Extract to a temp directory first, then route
    tmp_dir = tempfile.mkdtemp(prefix="plates_extract_")

    try:
        cmd = ["unzip", "-o", "-q"]
        if pwd_str:
            cmd.extend(["-P", pwd_str])
        cmd.extend([zip_path, "-d", tmp_dir])

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if result.returncode >= 3:
            raise CurrencyError(
                f"unzip failed for plates: {result.stderr.strip()[:200]}"
            )

        # Route files to country subdirectories
        n_files = 0
        for name in os.listdir(tmp_dir):
            src_path = os.path.join(tmp_dir, name)
            if not os.path.isfile(src_path):
                continue

            if name[:2] in _PLATES_INTL_PREFIXES:
                subdir = name[:2]
            else:
                subdir = "US"

            dest_dir = os.path.join(plates_dir, subdir)
            os.makedirs(dest_dir, exist_ok=True)
            shutil.move(src_path, os.path.join(dest_dir, name))
            n_files += 1

        logger.info(
            f"Extracted and routed {n_files} plate PNG(s) to country subdirs in {plates_dir}"
        )
        return n_files

    finally:
        # Clean up temp directory
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _extract_multidisk(zip_path: str, pwd: Optional[bytes], chartdata_dir: str) -> int:
    """Extract a MultiDiskImg zip, routing files to LO/ or SEC/ based on filename.

    MultiDiskImg zips contain files like:
        2607/W085/W085.00xN31.00.LO.0.png
        2607/W085/W085.00xN31.00.SEC.0.png

    These need to be extracted to:
        ChartData/LO/W085/W085.00xN31.00.LO.0.png
        ChartData/SEC/W085/W085.00xN31.00.SEC.0.png
    """
    import pyzipper

    n_files = 0

    # Try standard zipfile first, fall back to pyzipper
    try:
        zf = zipfile.ZipFile(zip_path)
        members = zf.namelist()
        # Test if we can read with standard zipfile
        zf.testzip()
    except (NotImplementedError, RuntimeError, zipfile.BadZipFile):
        zf = pyzipper.AESZipFile(zip_path)
        members = zf.namelist()

    try:
        for member in members:
            # Skip directories
            if member.endswith('/'):
                continue

            # Get just the filename and its parent W### directory
            parts = member.replace('\\', '/').split('/')
            # Expected: ["2607", "W085", "W085.00xN31.00.LO.0.png"]
            # or: ["W085", "W085.00xN31.00.LO.0.png"]
            filename = parts[-1]
            tile_dir = parts[-2] if len(parts) >= 2 else ""

            # Determine if this is LO or SEC based on filename
            if ".LO." in filename:
                sub_dir = "LO"
            elif ".SEC." in filename:
                sub_dir = "SEC"
            else:
                # Unknown type — put in base chartdata dir
                sub_dir = ""

            if sub_dir and (tile_dir.startswith("W") or tile_dir.startswith("E")):
                dest_dir = os.path.join(chartdata_dir, sub_dir, tile_dir)
            elif sub_dir:
                dest_dir = os.path.join(chartdata_dir, sub_dir)
            else:
                dest_dir = chartdata_dir

            os.makedirs(dest_dir, exist_ok=True)
            dest_path = os.path.join(dest_dir, filename)

            # Extract the file
            data = zf.read(member, pwd=pwd)
            with open(dest_path, 'wb') as f:
                f.write(data)
            n_files += 1

    finally:
        zf.close()

    logger.info(f"Extracted {n_files} MultiDiskImg file(s), routed to LO/SEC in {chartdata_dir}")
    return n_files


# ---------------------------------------------------------------------------
# Main chart update flow
# ---------------------------------------------------------------------------

def check_chart_currency() -> tuple[list[dict], list[dict]]:
    """Check chart currency without downloading.

    Returns:
        Tuple of (all_entries, new_entries) where new_entries are those needing update.

    Raises:
        LoginError, PageLayoutChangedError, CurrencyError on failure.
    """
    session = sa_login()
    entries = parse_download_table(session)
    new_entries = check_for_new_charts(entries)
    return entries, new_entries


def download_charts(entries: list[dict], progress_callback=None) -> dict:
    """Download and extract specific chart entries.

    Args:
        entries: List of chart entry dicts to download.
        progress_callback: Optional callable(filename, pct) for progress updates.

    Returns:
        Dict with results: {"downloaded": int, "errors": list[str]}
    """
    config = load_config()
    usb_image_path = config["usb_image_path"]
    download_cache = str(Path(APP_SUPPORT_DIR) / "chart_downloads")

    results = {"downloaded": 0, "errors": []}
    metadata = load_cycle_metadata()

    session = sa_login()

    for entry in entries:
        try:
            zip_path = download_chart(session, entry, download_cache,
                                      progress_callback=progress_callback)

            # Capture mtime immediately after download (before extraction/deletion)
            download_mtime = str(Path(zip_path).stat().st_mtime)

            target_dir = _determine_extract_dir(entry["description"], usb_image_path)
            extract_chart(zip_path, entry, target_dir)
            results["downloaded"] += 1

            # Update metadata
            metadata[entry["download_url"]] = {
                "description": entry["description"],
                "valid_dates": entry["valid_dates"],
                "password": entry["password"],
                "last_downloaded": download_mtime,
            }
            save_cycle_metadata(metadata)

            # Clean up zip after successful extraction
            if os.path.exists(zip_path):
                os.remove(zip_path)

        except CurrencyError as e:
            logger.error(f"Failed to process {entry['description']}: {e}")
            results["errors"].append(f"{entry['description']}: {e}")
        except Exception as e:
            logger.error(f"Unexpected error processing {entry['description']}: {e}")
            results["errors"].append(f"{entry['description']}: {e}")

    return results


def update_charts(force: bool = False) -> dict:
    """Full chart update flow: login, check, download, extract.

    Args:
        force: If True, re-download all charts regardless of cycle status.

    Returns:
        Dict with results: {"checked": int, "downloaded": int, "errors": list[str]}
    """
    config = load_config()
    usb_image_path = config["usb_image_path"]
    download_cache = str(Path(APP_SUPPORT_DIR) / "chart_downloads")

    results = {"checked": 0, "downloaded": 0, "errors": []}

    try:
        session = sa_login()
    except (LoginError, CurrencyError, PageLayoutChangedError) as e:
        results["errors"].append(str(e))
        return results

    try:
        entries = parse_download_table(session)
    except PageLayoutChangedError as e:
        results["errors"].append(str(e))
        return results

    results["checked"] = len(entries)

    if force:
        to_download = entries
    else:
        to_download = check_for_new_charts(entries)

    if not to_download:
        return results

    metadata = load_cycle_metadata()

    for entry in to_download:
        try:
            zip_path = download_chart(session, entry, download_cache)

            # Capture mtime immediately after download (before extraction/deletion)
            download_mtime = str(Path(zip_path).stat().st_mtime)

            target_dir = _determine_extract_dir(entry["description"], usb_image_path)
            extract_chart(zip_path, entry, target_dir)
            results["downloaded"] += 1

            # Update metadata
            metadata[entry["download_url"]] = {
                "description": entry["description"],
                "valid_dates": entry["valid_dates"],
                "password": entry["password"],
                "last_downloaded": download_mtime,
            }
            save_cycle_metadata(metadata)

            # Clean up zip after successful extraction
            os.remove(zip_path)

        except CurrencyError as e:
            logger.error(f"Failed to process {entry['description']}: {e}")
            results["errors"].append(f"{entry['description']}: {e}")
        except Exception as e:
            logger.error(f"Unexpected error processing {entry['description']}: {e}")
            results["errors"].append(f"{entry['description']}: {e}")

    return results


# ---------------------------------------------------------------------------
# GRT Software / Nav DB checking and downloading (via Playwright for Sucuri bypass)
# ---------------------------------------------------------------------------

GRT_NAV_PROC_URL = "https://grtavionics.com/nav-proc-database-updates/"
GRT_HXR_PRODUCT_URL = "https://grtavionics.com/product/horizon-hxr-efis/"
GRT_MINIAP_PRODUCT_URL = "https://grtavionics.com/product/mini-ap-efis/"

GRT_VERSION_METADATA_FILE = APP_SUPPORT_DIR / "grt_versions.json"


def _load_grt_metadata() -> dict:
    """Load locally stored GRT version metadata."""
    if GRT_VERSION_METADATA_FILE.exists():
        try:
            with open(GRT_VERSION_METADATA_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_grt_metadata(metadata: dict):
    """Save GRT version metadata to disk."""
    APP_SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    with open(GRT_VERSION_METADATA_FILE, "w") as f:
        json.dump(metadata, f, indent=2)


def _check_playwright_browser() -> Optional[str]:
    """Check whether the Playwright Chromium browser is installed.

    Returns:
        None if the browser is available, or an error message describing
        the fix if the browser binary is missing.
    """
    browsers_path = os.path.expanduser("~/Library/Caches/ms-playwright")
    if not os.path.isdir(browsers_path):
        return _browser_missing_message()
    # Look for a chromium/headless-shell executable under the cache
    for entry in os.listdir(browsers_path):
        if entry.startswith("chromium"):
            return None
    return _browser_missing_message()


def _browser_missing_message() -> str:
    return (
        "Playwright browser not installed. Run this once to fix:\n"
        "  ./venv/bin/playwright install chromium"
    )


def _playwright_fetch_grt_page(url: str, timeout: int = 60000) -> str:
    """Fetch a GRT page using Playwright to bypass Sucuri JS challenge.

    Runs in a subprocess to avoid event loop conflicts.

    Returns:
        Page HTML content.
    """
    import subprocess as _sp
    import sys

    script = f'''
import time
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto("{url}", timeout={timeout})
    time.sleep(5)
    print(page.content())
    browser.close()
'''
    env = dict(os.environ)
    env["PLAYWRIGHT_BROWSERS_PATH"] = os.path.expanduser("~/Library/Caches/ms-playwright")
    result = _sp.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=120, env=env
    )
    if result.returncode != 0:
        raise PageLayoutChangedError(f"Playwright fetch failed: {result.stderr[:200]}")
    return result.stdout


def _playwright_download_grt_file(page_url: str, link_selector: str, dest_path: str,
                                   timeout: int = 120000) -> int:
    """Download a file from GRT by clicking a link via Playwright subprocess.

    Args:
        page_url: The GRT page containing the download link.
        link_selector: CSS selector for the download link.
        dest_path: Local path to save the downloaded file.
        timeout: Download timeout in ms.

    Returns:
        File size in bytes.
    """
    import subprocess as _sp
    import sys

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    # Escape quotes in selector for embedding in Python string
    sel_escaped = link_selector.replace('"', '\\"')

    script = f"""
import time, os, sys, shutil, tempfile
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(accept_downloads=True)
    page = context.new_page()
    page.goto("{page_url}", timeout=90000)
    time.sleep(8)

    link = page.locator('{sel_escaped}')
    if link.count() == 0:
        print("LINK_NOT_FOUND", file=sys.stderr)
        browser.close()
        sys.exit(1)

    with page.expect_download(timeout={timeout}) as dl_info:
        link.click()
    download = dl_info.value

    tmp_path = tempfile.mktemp(suffix=".tmp")
    download.save_as(tmp_path)
    shutil.move(tmp_path, "{dest_path}")
    print(os.path.getsize("{dest_path}"))
    browser.close()
"""
    env = dict(os.environ)
    env["PLAYWRIGHT_BROWSERS_PATH"] = os.path.expanduser("~/Library/Caches/ms-playwright")
    result = _sp.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=180, env=env
    )
    if result.returncode != 0:
        if "LINK_NOT_FOUND" in result.stderr:
            raise PageLayoutChangedError(
                f"Download link not found with selector '{link_selector}' on {page_url}"
            )
        raise CurrencyError(f"Playwright download failed: {result.stderr[:200]}")

    size = int(result.stdout.strip())
    return size


def check_and_download_nav_db() -> dict:
    """Check for new nav database and download if updated.

    Returns:
        Dict with: {"status": "current"|"updated"|"error", "message": str}
    """
    config = load_config()
    usb_image_path = config["usb_image_path"]

    # Clear error if the Playwright browser isn't installed
    browser_error = _check_playwright_browser()
    if browser_error:
        logger.error(f"Nav DB check: {browser_error}")
        return {"status": "error", "message": browser_error}

    try:
        html = _playwright_fetch_grt_page(GRT_NAV_PROC_URL)
    except Exception as e:
        logger.error(f"Failed to fetch GRT nav DB page: {e}")
        return {"status": "error", "message": str(e)}

    soup = BeautifulSoup(html, "html.parser")

    # Parse the valid date from the table
    # Table format: "Navigation Database | Valid Date | Posted Date"
    # followed by rows with dates like "8/6/2026 | 8/3/2026"
    valid_date = None
    posted_date = None
    tables = soup.find_all("table")
    for table in tables:
        for tr in table.find_all("tr"):
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            # Look for a row with date-like values
            for cell in cells:
                if re.match(r'\d{1,2}/\d{1,2}/\d{4}', cell):
                    if valid_date is None:
                        valid_date = cell
                    elif posted_date is None:
                        posted_date = cell

    if not valid_date:
        logger.warning("Could not parse valid date from GRT nav DB page.")
        return {"status": "error", "message": "Could not parse valid date from nav DB page."}

    # Compare against local metadata
    metadata = _load_grt_metadata()
    local_valid_date = metadata.get("nav_db_valid_date")

    if local_valid_date == valid_date:
        logger.info(f"Nav DB is current (valid date: {valid_date})")
        return {"status": "current", "message": f"Nav DB current (valid: {valid_date})"}

    # New version available — download both proc and non-proc
    logger.info(f"New nav DB available: valid {valid_date} (was: {local_valid_date})")

    try:
        # Download proc version (HXr)
        proc_dest = os.path.join(usb_image_path, "NAV-proc.DB")
        size = _playwright_download_grt_file(
            GRT_NAV_PROC_URL,
            'a[href*="NAV/proc/NAV.DB"]',
            proc_dest,
        )
        logger.info(f"Downloaded NAV-proc.DB: {size / 1024 / 1024:.1f} MB")

        # Download non-proc version (Mini A/P and others)
        nonproc_dest = os.path.join(usb_image_path, "NAV.DB")
        size2 = _playwright_download_grt_file(
            GRT_NAV_PROC_URL,
            'a[href*="getfile.aspx/NAV.DB"]:not([href*="proc"])',
            nonproc_dest,
        )
        logger.info(f"Downloaded NAV.DB: {size2 / 1024 / 1024:.1f} MB")

        # Update metadata
        metadata["nav_db_valid_date"] = valid_date
        metadata["nav_db_posted_date"] = posted_date
        _save_grt_metadata(metadata)

        return {
            "status": "updated",
            "message": f"Nav DB updated (valid: {valid_date}). "
                       f"Proc: {size/1024/1024:.1f} MB, Non-proc: {size2/1024/1024:.1f} MB",
        }

    except (PageLayoutChangedError, Exception) as e:
        logger.error(f"Nav DB download failed: {e}")
        return {"status": "error", "message": str(e)}


def check_grt_updates() -> dict:
    """Check grtavionics.com for new software/nav DB versions.

    Returns:
        Dict with results per component.
    """
    results = {}

    # Nav DB (automated via Playwright)
    results["nav_db"] = check_and_download_nav_db()

    # EFIS software (automated via Playwright)
    results["software"] = check_and_download_efis_software()

    return results


# Software downloads we care about (link text -> local filename)
HXR_SOFTWARE_TARGETS = {
    "Display Unit Software": "HHXRUp-proc.dat",
}

MINIAP_SOFTWARE_TARGETS = {
    "Display Unit Software": "MiniUp.dat",
    "AHRS Software": "MiniAHRSUp.dat",
}


def check_and_download_efis_software() -> dict:
    """Check for new EFIS/AHRS software and download if updated.

    Compares download URLs against previously seen URLs — if URL changes,
    a new version is available.

    Returns:
        Dict with: {"status": "current"|"updated"|"error", "updated_items": list, "message": str}
    """
    config = load_config()
    usb_image_path = config["usb_image_path"]
    metadata = _load_grt_metadata()
    updated_items = []

    # Clear error if the Playwright browser isn't installed
    browser_error = _check_playwright_browser()
    if browser_error:
        logger.error(f"Software check: {browser_error}")
        return {"status": "error", "updated_items": [], "message": browser_error}

    try:
        # Check HXr page
        hxr_links = _get_grt_download_links(GRT_HXR_PRODUCT_URL)
        for link_text, local_name in HXR_SOFTWARE_TARGETS.items():
            url = hxr_links.get(link_text)
            if not url:
                continue
            stored_url = metadata.get(f"hxr_{local_name}_url")
            if url != stored_url:
                logger.info(f"New HXr software detected: {link_text} -> {url}")
                logger.info(f"Downloading HXr {link_text}...")
                dest = os.path.join(usb_image_path, local_name)
                _playwright_download_grt_file(
                    GRT_HXR_PRODUCT_URL,
                    f'a[href*="{_url_to_selector_fragment(url)}"]',
                    dest,
                )
                logger.info(f"Downloaded HXr {link_text} to {dest}")
                metadata[f"hxr_{local_name}_url"] = url
                updated_items.append(f"HXr {link_text}")

        # Check Mini A/P page
        miniap_links = _get_grt_download_links(GRT_MINIAP_PRODUCT_URL)
        for link_text, local_name in MINIAP_SOFTWARE_TARGETS.items():
            url = miniap_links.get(link_text)
            if not url:
                continue
            stored_url = metadata.get(f"miniap_{local_name}_url")
            if url != stored_url:
                logger.info(f"New Mini A/P software detected: {link_text} -> {url}")
                logger.info(f"Downloading Mini A/P {link_text}...")
                dest = os.path.join(usb_image_path, local_name)
                _playwright_download_grt_file(
                    GRT_MINIAP_PRODUCT_URL,
                    f'a[href*="{_url_to_selector_fragment(url)}"]',
                    dest,
                )
                logger.info(f"Downloaded Mini A/P {link_text} to {dest}")
                metadata[f"miniap_{local_name}_url"] = url
                updated_items.append(f"Mini A/P {link_text}")

        _save_grt_metadata(metadata)

        if updated_items:
            msg = f"Updated: {', '.join(updated_items)}"
            return {"status": "updated", "updated_items": updated_items, "message": msg}
        else:
            return {"status": "current", "updated_items": [], "message": "All EFIS software is current."}

    except Exception as e:
        logger.error(f"EFIS software check failed: {e}")
        return {"status": "error", "updated_items": [], "message": str(e)}


def _get_grt_download_links(page_url: str) -> dict:
    """Fetch a GRT product page and return a dict of link_text -> href for download links.

    Runs Playwright in a subprocess to avoid event loop conflicts with the main app.
    """
    import json as _json
    import subprocess as _sp
    import sys

    script = f'''
import json, time
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto("{page_url}", timeout=90000)
    time.sleep(8)
    links = page.query_selector_all('a[href*=".zip"], a[href*=".dat"], a[href*="getfile"]')
    result = {{}}
    for link in links:
        href = link.get_attribute("href")
        text = link.inner_text().strip()
        if text and href:
            result[text] = href
    browser.close()
    print(json.dumps(result))
'''
    env = dict(os.environ)
    env["PLAYWRIGHT_BROWSERS_PATH"] = os.path.expanduser("~/Library/Caches/ms-playwright")
    result = _sp.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=120, env=env
    )
    if result.returncode != 0:
        raise PageLayoutChangedError(f"Failed to fetch GRT page: {result.stderr[:200]}")

    return _json.loads(result.stdout)


def _url_to_selector_fragment(url: str) -> str:
    """Extract a unique fragment from a URL for use as a CSS selector contains match."""
    # Use the last path segment (filename) as the selector fragment
    parts = url.rstrip('/').split('/')
    # Use last 2-3 segments for uniqueness
    if len(parts) >= 3:
        return '/'.join(parts[-3:])
    return parts[-1]
