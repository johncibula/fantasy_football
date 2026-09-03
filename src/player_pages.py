"""Fantasy Footballers player pages -> data/players/<key>.json

Each player has a page like https://www.thefantasyfootballers.com/fantasy/josh-allen/
with the write-up, news, and (for FootClan members) the premium bits. We pull
the pages for the top of the board with the same logged-in browser session
udk_fetch.py uses, keep the readable text, and the dashboard shows it under
"profile" on each recommendation.

  ./venv/bin/python src/player_pages.py [--top 200] [--force] [--only "Josh Allen,Puka Nacua"]

Pages are re-fetched only when older than --max-age hours (default 72) so the
daily job stays cheap; --force refetches everything. Never blocks: a failed
page is logged and skipped. Requires the UDK session (see udk_fetch.py).
"""

import argparse
import json
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from draft_tracker import DATA, norm_name, load_board
import udk_fetch as UF

PLAYERS_DIR = DATA / "players"
BASE = "https://www.thefantasyfootballers.com/fantasy/"
# Navigation/footer boilerplate we strip from the extracted text.
_NOISE = re.compile(r"^(menu|log ?in|sign ?up|search|subscribe|share|home|podcast|shop|"
                    r"footclan|ultimate draft kit|udk|©.*|privacy.*|terms.*)$", re.I)


def slug(name: str) -> str:
    """'Ja'Marr Chase' -> 'jamarr-chase', 'Marvin Harrison Jr.' -> 'marvin-harrison-jr'."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"[.'’]", "", s).lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


def key_for(name: str) -> str:
    return norm_name(name).replace(" ", "_")


_SUFFIX_RE = re.compile(r"\s+(jr|sr|ii|iii|iv|v)\.?$", re.I)
# Nicknames the board uses where the site's slug is the legal name.
NICKNAMES = {"chig": "chigoziem", "cam": "cameron", "tank": "nathaniel", "bucky": "bucky",
             "hollywood": "marquise", "deebo": "deebo", "dj": "dj", "aj": "aj"}


def slug_candidates(name: str, espn_name: str | None = None) -> list[str]:
    """Slugs to try, in order. The site drops generational suffixes
    (james-cook, not james-cook-iii) but sometimes needs one to disambiguate
    (brian-robinson-jr), and uses legal first names where the board has a
    nickname (chigoziem-okonkwo). ESPN's player list carries legal names, so
    the caller can pass that as a second source."""
    cands: list[str] = []
    for nm in [name, espn_name]:
        if not nm:
            continue
        bare = _SUFFIX_RE.sub("", nm.strip())
        first, *rest = bare.split()
        cands += [slug(bare), slug(nm)]
        alt = NICKNAMES.get(first.lower().replace(".", ""))
        if alt and alt != first.lower():
            cands.append(slug(" ".join([alt] + rest)))
        cands += [slug(bare) + sfx for sfx in ("-jr", "-ii", "-iii", "-sr")]
    return list(dict.fromkeys(c for c in cands if c))


def _espn_name(name: str) -> str | None:
    try:
        import market_feed
        r = market_feed.market_for(name)
        return r.get("name") if r else None
    except Exception:  # noqa: BLE001
        return None


def _clean_text(raw: str, max_chars: int = 6000) -> tuple[str, list[str]]:
    lines, headings = [], []
    for ln in raw.splitlines():
        t = ln.strip()
        if not t or _NOISE.match(t) or len(t) < 3:
            continue
        lines.append(t)
    text = "\n".join(lines)
    return text[:max_chars], headings


_AGO_RE = re.compile(r"^(?P<head>.+?)\s+(?P<age>(?:\d+|an?)\s+(?:minute|hour|day|week|month)s?\s+ago)$", re.I)
_INJ_RE = re.compile(r"^Injured:\s*(?P<status>[A-Za-z]+)(?:\s+with\s+(?P<part>[^|]+?))?\s*$", re.I)
_SECTION_END = re.compile(r"^(PODCASTS|ARTICLES|MORE|Premium Tools.*|Registrations.*|\d{4} UDK\+?)$", re.I)


def parse_profile(name: str, raw_text: str, headings: list[str]) -> dict:
    """Structured fields from a player page's text. The page reads like:
    NAME | TEAM | #num | [date] | [Injured: Questionable with Groin] | Week 1 |
    HT/WT ... | ADP | 1.07 | ANDY #3 | JASON #2 | MIKE #2 | ... |
    NAME OUTLOOK | <paragraphs...> | PODCASTS ...
    News items appear as headings like 'Back at practice Sunday 4 DAYS AGO'."""
    lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
    up = name.upper()
    out = {"injury": None, "injury_date": None, "news": [], "outlook": "", "adp": None,
           "hosts": {}, "age": None, "experience": None}
    # header block: first ~40 lines
    head = lines[:60]
    for i, ln in enumerate(head):
        m = _INJ_RE.match(ln)
        if m:
            out["injury"] = (m.group("status").title() + (f" ({m.group('part').strip()})" if m.group("part") else ""))
            if i > 0 and re.match(r"^[A-Z][a-z]{2} \d{1,2}, \d{4}$", head[i - 1]):
                out["injury_date"] = head[i - 1]
        if ln == "ADP" and i + 1 < len(head) and re.match(r"^\d+\.\d+$", head[i + 1]):
            out["adp"] = head[i + 1]
        mh = re.match(r"^(ANDY|JASON|MIKE)\s+#(\d+)$", ln)
        if mh:
            out["hosts"][mh.group(1).title()] = int(mh.group(2))
        ma = re.match(r"^AGE\s+([\d.]+)$", ln)
        if ma:
            out["age"] = float(ma.group(1))
        me = re.match(r"^EXPERIENCE\s+(.+)$", ln)
        if me:
            out["experience"] = me.group(1)
    for h in headings:
        m = _AGO_RE.match(h.strip())
        if m:
            out["news"].append({"headline": m.group("head").strip(), "age": m.group("age").lower()})
    # outlook: everything after "<NAME> OUTLOOK" until the next site section
    try:
        start = next(i for i, ln in enumerate(lines) if ln.upper() == f"{up} OUTLOOK")
        body = []
        for ln in lines[start + 1:]:
            if _SECTION_END.match(ln) or ln.upper().endswith(" OUTLOOK"):
                break
            body.append(ln)
        out["outlook"] = " ".join(body).strip()[:4000]
    except StopIteration:
        pass
    return out


def extract(page, name: str = "") -> dict:
    """Pull the readable content of a player page plus the structured fields
    parse_profile() understands (injury, news, outlook, ADP, host ranks)."""
    main = None
    for sel in ("main", "article", "[role='main']", ".entry-content", "#content"):
        loc = page.locator(sel)
        if loc.count():
            main = loc.first
            break
    raw = (main or page.locator("body")).inner_text(timeout=15_000)
    headings = [h.strip() for h in page.locator("h1, h2, h3, h4").all_inner_texts() if h.strip()][:60]
    text, _ = _clean_text(raw)
    title = page.title()
    fields = parse_profile(name or title.split(" Fantasy")[0], raw, headings)
    paras = [p for p in text.split("\n") if len(p) > 120]
    summary = fields["outlook"] or (paras[0] if paras else text[:600])
    return {"title": title, "headings": headings, "summary": summary[:1500], "text": text, **fields}


def fetch_pages(names: list[str], force: bool = False, max_age_h: float = 72.0,
                headed: bool = False, delay_s: float = 1.0) -> dict:
    from playwright.sync_api import sync_playwright

    PLAYERS_DIR.mkdir(parents=True, exist_ok=True)
    if not UF.SESSION.exists() and not UF.auto_login():
        print("No UDK session; run udk_fetch.py --login (or add UDK_EMAIL/UDK_PASSWORD to .env)")
        return {"ok": 0, "skipped": 0, "failed": len(names)}
    now = datetime.now(timezone.utc)
    ok = skipped = failed = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed)
        ctx = browser.new_context(storage_state=str(UF.SESSION))
        page = ctx.new_page()
        for name in names:
            out = PLAYERS_DIR / f"{key_for(name)}.json"
            if out.exists() and not force:
                try:
                    prev = json.loads(out.read_text())
                    age = (now - datetime.fromisoformat(prev["fetched_at"])).total_seconds() / 3600
                    if age < max_age_h:
                        skipped += 1
                        continue
                except Exception:  # noqa: BLE001
                    pass
            try:
                resp, url = None, None
                for cand in slug_candidates(name, _espn_name(name)):
                    url = BASE + cand + "/"
                    resp = page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                    if resp is not None and resp.status < 400:
                        break
                if resp is None or resp.status >= 400:
                    failed += 1
                    print(f"  ! {name}: HTTP {resp.status if resp else '?'} at {url}")
                    continue
                page.wait_for_timeout(800)
                data = extract(page, name)
                data.update({"name": name, "url": url, "fetched_at": now.isoformat(),
                             "gated": bool(UF.PAYWALL_RE.search(data["text"][:1500]))})
                out.write_text(json.dumps(data, indent=1))
                ok += 1
                time.sleep(delay_s)
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"  ! {name}: {type(e).__name__}: {e}")
        try:
            ctx.storage_state(path=str(UF.SESSION))
        except Exception:  # noqa: BLE001
            pass
        browser.close()
    print(f"player pages: {ok} fetched, {skipped} fresh, {failed} failed")
    return {"ok": ok, "skipped": skipped, "failed": failed}


_MEMO: dict = {}


def profile_for(name: str) -> dict | None:
    """Cached read of a player's page JSON (summary/url) for the dashboard."""
    path = PLAYERS_DIR / f"{key_for(name)}.json"
    try:
        st = path.stat()
    except OSError:
        return None
    key = (st.st_mtime_ns, st.st_size)
    hit = _MEMO.get(path)
    if hit and hit[0] == key:
        return hit[1]
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    slim = {"url": d.get("url"), "summary": d.get("summary"), "fetched": (d.get("fetched_at") or "")[:10],
            "injury": d.get("injury"), "injury_date": d.get("injury_date"),
            "news": (d.get("news") or [])[:4], "adp": d.get("adp"), "hosts": d.get("hosts") or {}}
    _MEMO[path] = (key, slim)
    return slim


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=200, help="board players by UDK rank")
    ap.add_argument("--only", type=str, help="comma-separated names")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--max-age", type=float, default=72.0)
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()
    if args.only:
        names = [n.strip() for n in args.only.split(",") if n.strip()]
    else:
        board = load_board()
        seen, names = set(), []
        for p in sorted((p for p in board.values() if p.get("overall_rank")),
                        key=lambda p: p["overall_rank"]):
            if id(p) in seen or p["pos"] == "DST":
                continue
            seen.add(id(p))
            names.append(p["name"])
            if len(names) >= args.top:
                break
    r = fetch_pages(names, force=args.force, max_age_h=args.max_age, headed=args.headed)
    return 0 if r["failed"] < max(1, len(names) // 2) else 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import espn_client  # noqa: F401  (loads .env)
    sys.exit(main())
