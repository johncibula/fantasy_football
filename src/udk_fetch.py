"""Download the Fantasy Footballers UDK rankings CSVs with a logged-in browser.

The UDK has no API and no direct CSV links: each position page has a "More"
control with a "Download CSV" item, behind the FootClan login. So we drive a
real browser (Playwright/Chromium) with a saved login session.

Login:     Put UDK_EMAIL and UDK_PASSWORD in .env and the fetcher logs in by
           itself (and re-logs in whenever the session expires). Without
           them: ./venv/bin/python src/udk_fetch.py --login opens a visible
           browser, you log in, press Enter, and the session cookies are
           saved to data/udk_session.json (gitignored).

Daily:     ./venv/bin/python src/udk_fetch.py [--headed] [--only rb,wr]
           Headless. Exports every position + top 200 into data/udk/, keeping
           the previous files in data/udk/prev/. A file is only replaced when
           the download parses with the expected header. Exit codes:
             0 ok · 2 session expired (run --login again) · 3 export failed
           On failure a screenshot + HTML land in data/udk_debug/ so the
           selectors can be fixed.
"""

import argparse
import csv
import io
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from draft_tracker import DATA

UDK_DIR = DATA / "udk"
PREV_DIR = UDK_DIR / "prev"
DEBUG_DIR = DATA / "udk_debug"
SESSION = DATA / "udk_session.json"

SEASON = 2026
BASE = f"https://www.thefantasyfootballers.com/{SEASON}-ultimate-draft-kit"
LOGIN_URL = ("https://www.thefantasyfootballers.com/login/?redirect_to="
             f"{BASE}/udk-position-rankings/")

# file stem -> (page url, expected header columns that must be present)
PAGES = {
    "qb":     (f"{BASE}/udk-position-rankings/?position=QB",   {"Name", "Rank", "Points", "Tier"}),
    "rb":     (f"{BASE}/udk-position-rankings/?position=RB",   {"Name", "Rank", "Points", "Tier"}),
    "wr":     (f"{BASE}/udk-position-rankings/?position=WR",   {"Name", "Rank", "Points", "Tier"}),
    "te":     (f"{BASE}/udk-position-rankings/?position=TE",   {"Name", "Rank", "Points", "Tier"}),
    "dst":    (f"{BASE}/udk-position-rankings/?position=D",    {"Name", "Rank"}),
    "k":      (f"{BASE}/udk-position-rankings/?position=K",    {"Name", "Rank"}),
    "flex":   (f"{BASE}/udk-position-rankings/?position=FLEX", {"Name", "Rank", "Points"}),
    "top200": (f"{BASE}/udk-top-200-list/",                    {"Name", "Rank", "Pos"}),
}

PAYWALL_RE = re.compile(r"unlock|get the \d{4} udk|join the footclan|log in to", re.I)


def validate_csv(text: str, required: set[str]) -> tuple[bool, str]:
    """A download is good when it parses, has the required columns and rows."""
    try:
        rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    except csv.Error as e:
        return False, f"csv parse error: {e}"
    if not rows:
        return False, "no rows"
    missing = required - set(rows[0].keys())
    if missing:
        return False, f"missing columns {sorted(missing)}; got {list(rows[0].keys())[:8]}"
    if len(rows) < 10:
        return False, f"only {len(rows)} rows"
    return True, f"{len(rows)} rows"


def _dump_debug(page, stem: str, why: str) -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        page.screenshot(path=str(DEBUG_DIR / f"{stem}-{ts}.png"), full_page=True)
        (DEBUG_DIR / f"{stem}-{ts}.html").write_text(page.content())
    except Exception:  # noqa: BLE001
        pass
    print(f"  ! {stem}: {why} — debug saved to {DEBUG_DIR}")


def _is_paywalled(page) -> bool:
    body = page.locator("body").inner_text(timeout=10_000)
    head = body[:4000]
    return bool(PAYWALL_RE.search(head)) and "Rank" not in head


def _click_download(page):
    """Open the 'More' menu and click the CSV item. Returns the Download."""
    more_candidates = [
        page.get_by_role("button", name=re.compile(r"^\s*more\s*$", re.I)),
        page.get_by_role("tab", name=re.compile(r"more", re.I)),
        page.get_by_role("link", name=re.compile(r"^\s*more\s*$", re.I)),
        page.get_by_text(re.compile(r"^\s*more\s*$", re.I)),
    ]
    for loc in more_candidates:
        try:
            if loc.count():
                loc.first.click(timeout=5_000)
                break
        except Exception:  # noqa: BLE001
            continue
    csv_candidates = [
        page.get_by_role("link", name=re.compile(r"csv", re.I)),
        page.get_by_role("button", name=re.compile(r"csv", re.I)),
        page.get_by_role("menuitem", name=re.compile(r"csv", re.I)),
        page.get_by_text(re.compile(r"download.*csv|csv", re.I)),
    ]
    for loc in csv_candidates:
        try:
            if loc.count():
                with page.expect_download(timeout=20_000) as dl:
                    loc.first.click(timeout=5_000)
                return dl.value
        except Exception:  # noqa: BLE001
            continue
    raise RuntimeError("could not find a CSV download control")


def fetch(only: list[str] | None = None, headed: bool = False, _retry: bool = False) -> int:
    from playwright.sync_api import sync_playwright

    if not SESSION.exists() and not auto_login():
        print(f"No saved session at {SESSION}. Add UDK_EMAIL/UDK_PASSWORD to .env or run: "
              "./venv/bin/python src/udk_fetch.py --login")
        return 2
    stems = only or list(PAGES)
    UDK_DIR.mkdir(parents=True, exist_ok=True)
    PREV_DIR.mkdir(parents=True, exist_ok=True)
    ok, failed = [], []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed)
        ctx = browser.new_context(storage_state=str(SESSION), accept_downloads=True)
        page = ctx.new_page()
        for stem in stems:
            url, required = PAGES[stem]
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                page.wait_for_timeout(1500)
                if _is_paywalled(page):
                    _dump_debug(page, stem, "paywall shown — session expired?")
                    browser.close()
                    if not _retry and auto_login():
                        return fetch(only=only, headed=headed, _retry=True)
                    print("Session expired. Run: ./venv/bin/python src/udk_fetch.py --login")
                    return 2
                dl = _click_download(page)
                tmp = UDK_DIR / f".{stem}.download"
                dl.save_as(str(tmp))
                text = tmp.read_text(encoding="utf-8-sig", errors="replace")
                good, why = validate_csv(text, required)
                if not good:
                    tmp.unlink(missing_ok=True)
                    _dump_debug(page, stem, f"bad csv: {why}")
                    failed.append(stem)
                    continue
                target = UDK_DIR / f"{stem}.csv"
                if target.exists():
                    shutil.copy2(target, PREV_DIR / f"{stem}.csv")
                tmp.replace(target)
                ok.append(stem)
                print(f"  {stem:7s} ok ({why})")
            except Exception as e:  # noqa: BLE001
                _dump_debug(page, stem, f"{type(e).__name__}: {e}")
                failed.append(stem)
        # refresh the saved session so the cookies don't age out
        try:
            ctx.storage_state(path=str(SESSION))
        except Exception:  # noqa: BLE001
            pass
        browser.close()
    print(f"UDK export: {len(ok)} ok, {len(failed)} failed{(' (' + ', '.join(failed) + ')') if failed else ''}")
    return 0 if not failed else 3


def _env_creds() -> tuple[str, str]:
    """(email, password) from .env — UDK_EMAIL / UDK_PASSWORD (or UDK_USER)."""
    return (os.environ.get("UDK_EMAIL") or os.environ.get("UDK_USER") or "",
            os.environ.get("UDK_PASSWORD") or "")


def _fill_login(page, user: str, password: str) -> None:
    """The FootClan login is a custom form with 'Username' and 'Password'
    fields and a Log In button (plus a Patreon button we must not click)."""
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(1000)
    user_box = None
    for loc in (page.get_by_label(re.compile(r"user ?name|email", re.I)),
                page.locator("input[name='log'], input#user_login, input[type='email'], input[name*='user' i]")):
        if loc.count():
            user_box = loc.first
            break
    pass_box = None
    for loc in (page.get_by_label(re.compile(r"password", re.I)),
                page.locator("input[type='password']")):
        if loc.count():
            pass_box = loc.first
            break
    if user_box is None or pass_box is None:
        raise RuntimeError("login form fields not found")
    user_box.fill(user)
    pass_box.fill(password)
    submit = page.get_by_role("button", name=re.compile(r"^\s*log ?in\s*$", re.I))
    if not submit.count():
        submit = page.locator("input[type='submit'], button[type='submit']")
    submit.first.click(timeout=10_000)
    page.wait_for_load_state("domcontentloaded", timeout=60_000)
    page.wait_for_timeout(2000)


def auto_login(headed: bool = False) -> bool:
    """Log in with .env credentials and save the session. False on failure."""
    from playwright.sync_api import sync_playwright

    user, password = _env_creds()
    if not (user and password):
        return False
    DATA.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed)
        ctx = browser.new_context(accept_downloads=True)
        page = ctx.new_page()
        try:
            _fill_login(page, user, password)
            page.goto(PAGES["rb"][0], wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(1500)
            if _is_paywalled(page):
                _dump_debug(page, "login", "still paywalled after credential login")
                return False
            ctx.storage_state(path=str(SESSION))
            print(f"Logged in with .env credentials; session saved to {SESSION}")
            return True
        except Exception as e:  # noqa: BLE001
            _dump_debug(page, "login", f"{type(e).__name__}: {e}")
            return False
        finally:
            browser.close()


def login() -> int:
    from playwright.sync_api import sync_playwright

    if all(_env_creds()):
        return 0 if auto_login(headed=True) else 2
    DATA.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        ctx = browser.new_context(accept_downloads=True)
        page = ctx.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
        print("\nA browser window is open. Log in to the FootClan, wait until the RB rankings")
        print("table is visible, then come back here and press Enter.")
        try:
            input()
        except EOFError:
            # non-interactive: poll for the rankings to appear for up to 5 min
            for _ in range(60):
                time.sleep(5)
                try:
                    if not _is_paywalled(page):
                        break
                except Exception:  # noqa: BLE001
                    pass
        page.goto(PAGES["rb"][0], wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(1500)
        if _is_paywalled(page):
            print("Still seeing the paywall — login did not take. Nothing saved.")
            browser.close()
            return 2
        ctx.storage_state(path=str(SESSION))
        browser.close()
    print(f"Session saved to {SESSION}. Test it: ./venv/bin/python src/udk_fetch.py --only rb --headed")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true", help="one-time interactive login")
    ap.add_argument("--headed", action="store_true", help="show the browser while exporting")
    ap.add_argument("--only", type=str, help="comma list of stems, e.g. rb,wr,top200")
    args = ap.parse_args()
    if args.login:
        sys.exit(login())
    only = [s.strip().lower() for s in args.only.split(",")] if args.only else None
    bad = [s for s in (only or []) if s not in PAGES]
    if bad:
        ap.error(f"unknown stems {bad}; choose from {list(PAGES)}")
    sys.exit(fetch(only=only, headed=args.headed))


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import espn_client  # noqa: F401  (loads .env)
    main()
