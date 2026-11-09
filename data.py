"""Reading AfriRadar's public CSV files.

AfriRadar republishes two regulator reports as plain CSV:

* SEC Nigeria's weekly valuation report: every collective investment scheme, grouped in nine
  categories, with net asset value, prices, unitholders and two yield columns.
* CMA Kenya's quarterly statistical bulletin: assets under management for every unit trust scheme.

This module downloads those files with the standard library only, keeps a copy on disk so a
plugin works offline and stays polite to the server, and turns rows into plain dicts. It knows
nothing about Hermes, so it can be tested on its own.
"""

from __future__ import annotations

import csv
import io
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

USER_AGENT = "hermes-afriradar/1.0 (+https://github.com/Risingtell/hermes-afriradar)"
SITE = "https://www.afriradar.com"

# id: (csv stem, plural label, site path)
CATEGORIES: dict[str, tuple[str, str, str]] = {
    "money-market": ("money-market-funds", "Money market funds", "/money-market-funds/"),
    "dollar": ("dollar-funds", "Dollar funds", "/dollar-funds/"),
    "bond": ("bond-funds", "Bond and fixed income funds", "/bond-funds/"),
    "equity": ("equity-funds", "Equity funds", "/equity-funds/"),
    "balanced": ("balanced-funds", "Balanced funds", "/balanced-funds/"),
    "reit": ("reits", "Real estate investment trusts", "/reits/"),
    "shariah": ("shariah-funds", "Shariah-compliant funds", "/shariah-funds/"),
    "ethical": ("ethical-funds", "Ethical funds", "/ethical-funds/"),
    "specialised": ("specialised-funds", "Specialised funds", "/specialised-funds/"),
}
KENYA_STEM = "kenya-unit-trusts"
# The site has a page per fund only for these two categories.
FUND_PAGE_CATEGORIES = ("money-market", "dollar")

CATEGORY_ALIASES = {
    "mmf": "money-market", "money market": "money-market", "money-market-funds": "money-market",
    "moneymarket": "money-market", "usd": "dollar", "dollar funds": "dollar", "eurobond": "dollar",
    "fixed income": "bond", "fixed-income": "bond", "bonds": "bond", "stocks": "equity",
    "equities": "equity", "mixed": "balanced", "real estate": "reit", "reits": "reit",
    "islamic": "shariah", "sharia": "shariah", "halal": "shariah", "specialized": "specialised",
}


class DataError(Exception):
    """A report could not be read and no saved copy exists."""


RETRY_AFTER = 600  # after a failed download, wait this long before trying the network again


def valid_data_url(url: str) -> bool:
    """https anywhere, or plain http only to this machine (a local mirror)."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if parts.scheme == "https" and parts.hostname:
        return True
    return parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1")


def slugify(name: str) -> str:
    """Same rule AfriRadar uses for its URLs and history keys."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower().replace("&", " and ")).strip("-")
    return slug or "fund"


def normalize_category(value: Optional[str]) -> Optional[str]:
    """Map what a user or model types ("MMF", "money market funds") to a category id."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text or text in ("all", "any", "*"):
        return None
    if text in CATEGORIES:
        return text
    if text in CATEGORY_ALIASES:
        return CATEGORY_ALIASES[text]
    text = text.replace("_", "-")
    if text in CATEGORIES:
        return text
    for stem_id, (stem, label, _path) in CATEGORIES.items():
        if text in (stem, label.lower()) or text.rstrip("s") == stem_id:
            return stem_id
    raise ValueError(
        f"Unknown category {value!r}. Use one of: {', '.join(CATEGORIES)}."
    )


def _num(value: str | None) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


@dataclass
class Fund:
    """One fund in one SEC week."""

    week: str
    category: str
    name: str
    slug: str
    manager: str
    nav_ngn: Optional[float]
    nav_usd: Optional[float]
    yield_ytd: Optional[float]
    yield_wtd: Optional[float]
    unitholders: Optional[int]
    bid_ngn: Optional[float]
    offer_ngn: Optional[float]
    bid_usd: Optional[float]
    offer_usd: Optional[float]
    subgroup: str
    flags: list[str]
    source_url: str

    @property
    def key(self) -> str:
        return f"{self.category}/{self.slug}"

    @property
    def reported_yield(self) -> Optional[float]:
        """The year-to-date column AfriRadar shows, with a zero read as 'not reported'."""
        if self.yield_ytd is None or self.yield_ytd == 0:
            return None
        return self.yield_ytd

    @property
    def size(self) -> Optional[float]:
        """Sortable size: dollars for dollar funds when SEC gives them, naira otherwise."""
        if self.category == "dollar" and self.nav_usd is not None:
            return self.nav_usd
        return self.nav_ngn

    @property
    def unit_price(self) -> Optional[float]:
        """What one unit is worth (bid, else offer), in dollars for dollar funds. None for money
        market funds, whose price stays at N1 or N100 while the yield pays out."""
        if self.category == "money-market":
            return None
        if self.category == "dollar":
            return self.bid_usd or self.offer_usd or None
        return self.bid_ngn or self.offer_ngn or None

    @property
    def url(self) -> str:
        if self.category in FUND_PAGE_CATEGORIES:
            return f"{SITE}/funds/{self.slug}/"
        return SITE + CATEGORIES[self.category][2]

    def summary(self) -> dict:
        """The fields a model needs, with yields as percentages so nobody has to convert."""
        out = {
            "fund": self.name,
            "id": self.key,
            "manager": self.manager,
            "category": self.category,
            "week_ended": self.week,
            "yield_pct": _pct(self.reported_yield),
            "yield_basis": "year-to-date, as the manager reported it to SEC",
            "nav_ngn": _round(self.nav_ngn, 0),
            "unitholders": self.unitholders,
            "url": self.url,
        }
        if self.category == "dollar":
            out["nav_usd"] = _round(self.nav_usd, 0)
            out["subgroup"] = self.subgroup or None
        if self.yield_wtd not in (None, 0) and self.category != "money-market":
            out["week_to_date_pct"] = _pct(self.yield_wtd)
        price = self.offer_usd if self.category == "dollar" and self.offer_usd else self.offer_ngn
        if price is not None and self.category != "money-market":
            out["offer_price"] = round(price, 4)
        if "ytd_dropped" in self.flags:
            out["note"] = "SEC printed a yield that cannot be real, so it is not shown."
        elif "ytd_scaled" in self.flags:
            out["note"] = "The manager typed this yield as a whole number; it was divided by 100."
        return {k: v for k, v in out.items() if v is not None}


@dataclass
class KenyaScheme:
    quarter: str
    name: str
    key: str
    aum_kes: Optional[float]
    previous_aum_kes: Optional[float]
    change: Optional[float]
    market_share: Optional[float]
    source_url: str

    def summary(self) -> dict:
        return {
            "scheme": self.name,
            "id": f"kenya/{self.key}",
            "quarter_ended": self.quarter,
            "aum_kes": _round(self.aum_kes, 0),
            "previous_quarter_aum_kes": _round(self.previous_aum_kes, 0),
            "quarter_change_pct": _pct(self.change),
            "market_share_pct": _pct(self.market_share),
            "url": f"{SITE}/kenya/unit-trusts/",
            "note": "CMA Kenya publishes assets per scheme (a manager's umbrella of funds), not yields or prices.",
        }


def _pct(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value * 100, 2)


def _round(value: Optional[float], digits: int) -> Optional[float]:
    if value is None:
        return None
    return round(value, digits) if digits else float(round(value))


def parse_funds(text: str) -> list[Fund]:
    """Rows of a SEC category CSV (latest or history) to Fund objects, keyed like AfriRadar.

    Two funds with the same name in the same category and week are told apart by sheet order:
    the second becomes '-2', exactly as the site does.
    """
    reader = csv.DictReader(io.StringIO(text))
    funds: list[Fund] = []
    used: dict[str, set[str]] = {}
    for row in reader:
        name = (row.get("fund") or "").strip()
        category = (row.get("category") or "").strip()
        week = (row.get("week_ended") or "").strip()
        if not name or category not in CATEGORIES or not week:
            continue
        taken = used.setdefault(week, set())
        stem = slugify(name)
        slug, count = stem, 1
        while f"{category}/{slug}" in taken:
            count += 1
            slug = f"{stem}-{count}"
        taken.add(f"{category}/{slug}")
        holders = _num(row.get("unitholders"))
        funds.append(Fund(
            week=week, category=category, name=name, slug=slug,
            manager=(row.get("manager") or row.get("manager_as_printed") or "").strip(),
            nav_ngn=_num(row.get("nav_ngn")), nav_usd=_num(row.get("nav_usd")),
            yield_ytd=_num(row.get("yield_ytd")), yield_wtd=_num(row.get("yield_wtd")),
            unitholders=int(holders) if holders is not None else None,
            bid_ngn=_num(row.get("bid_ngn")), offer_ngn=_num(row.get("offer_ngn")),
            bid_usd=_num(row.get("bid_usd")), offer_usd=_num(row.get("offer_usd")),
            subgroup=(row.get("subgroup") or "").strip(),
            flags=[f for f in (row.get("flags") or "").split(";") if f],
            source_url=(row.get("source_url") or "").strip(),
        ))
    return funds


def parse_kenya(text: str) -> list[KenyaScheme]:
    reader = csv.DictReader(io.StringIO(text))
    out: list[KenyaScheme] = []
    for row in reader:
        name = (row.get("scheme") or "").strip()
        if not name:
            continue
        out.append(KenyaScheme(
            quarter=(row.get("quarter_ended") or "").strip(), name=name,
            key=(row.get("scheme_key") or slugify(name)).strip(),
            aum_kes=_num(row.get("aum_kes")), previous_aum_kes=_num(row.get("previous_quarter_aum_kes")),
            change=_num(row.get("change")), market_share=_num(row.get("market_share")),
            source_url=(row.get("source_url") or "").strip(),
        ))
    return out


Fetcher = Callable[[str, dict], tuple[int, dict, bytes]]


def http_fetch(url: str, headers: dict, timeout: float = 20.0) -> tuple[int, dict, bytes]:
    """GET with the standard library. Returns (status, headers, body); 304 has an empty body."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - https URL from config
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return 304, dict(exc.headers or {}), b""
        raise


@dataclass
class _Cached:
    text: str
    fetched_at: float
    etag: str = ""
    last_modified: str = ""
    parsed: dict = field(default_factory=dict)


class DataSource:
    """Downloads and caches AfriRadar CSVs.

    * A file is reused for ``cache_hours``; after that a conditional GET (ETag / Last-Modified)
      asks the server whether it changed, so an unchanged report costs a few hundred bytes.
    * Copies live in ``cache_dir`` so a restart, or a lost connection, still has data. When the
      server cannot be reached the saved copy is served and marked stale instead of failing.
    """

    def __init__(self, base_url: str, cache_dir: Optional[Path], cache_hours: float = 6,
                 fetcher: Optional[Fetcher] = None, clock: Callable[[], float] = time.time):
        if not valid_data_url(base_url):
            raise ValueError("data_url must be an https:// URL")
        self.base_url = base_url if base_url.endswith("/") else base_url + "/"
        self.cache_dir = cache_dir
        self.ttl = max(1.0, min(72.0, float(cache_hours))) * 3600
        # Looked up at call time so tests (and a mirror) can swap the module-level fetcher.
        self.fetcher = fetcher or (lambda url, headers: http_fetch(url, headers))
        self.clock = clock
        self._memory: dict[str, _Cached] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self.stale: set[str] = set()
        self._failed: dict[str, tuple[float, str]] = {}

    # -- raw files ---------------------------------------------------------------------------
    def _paths(self, name: str) -> tuple[Optional[Path], Optional[Path]]:
        if self.cache_dir is None:
            return None, None
        return self.cache_dir / name, self.cache_dir / f"{name}.meta.json"

    def _load_disk(self, name: str) -> Optional[_Cached]:
        body, meta = self._paths(name)
        if body is None or not body.exists():
            return None
        try:
            info = json.loads(meta.read_text(encoding="utf-8")) if meta and meta.exists() else {}
            return _Cached(text=body.read_text(encoding="utf-8"), fetched_at=float(info.get("fetched_at", 0)),
                           etag=info.get("etag", ""), last_modified=info.get("last_modified", ""))
        except (OSError, ValueError):
            return None

    def _save_disk(self, name: str, entry: _Cached) -> None:
        body, meta = self._paths(name)
        if body is None:
            return
        try:
            body.parent.mkdir(parents=True, exist_ok=True)
            tmp = body.with_suffix(body.suffix + ".tmp")
            tmp.write_text(entry.text, encoding="utf-8")
            tmp.replace(body)
            meta.write_text(json.dumps({"fetched_at": entry.fetched_at, "etag": entry.etag,
                                        "last_modified": entry.last_modified}), encoding="utf-8")
        except OSError:
            pass  # a read-only disk only costs us the offline copy

    def _lock(self, name: str) -> threading.Lock:
        """One lock per file, so nine category files can download at the same time."""
        with self._locks_guard:
            return self._locks.setdefault(name, threading.Lock())

    def text(self, name: str) -> str:
        with self._lock(name):
            return self._get(name).text

    def _get(self, name: str) -> _Cached:
        now = self.clock()
        entry = self._memory.get(name) or self._load_disk(name)
        if entry is not None and now - entry.fetched_at < self.ttl:
            self._memory[name] = entry
            return entry
        failed = self._failed.get(name)
        if entry is None and failed and now - failed[0] < RETRY_AFTER:
            raise DataError(failed[1])  # do not wait on the same dead network again for every call
        headers = {}
        if entry is not None:
            if entry.etag:
                headers["If-None-Match"] = entry.etag
            if entry.last_modified:
                headers["If-Modified-Since"] = entry.last_modified
        try:
            status, resp_headers, body = self.fetcher(self.base_url + name, headers)
        except Exception as exc:  # network down, DNS, timeout, 5xx
            return self._fall_back(name, entry, now, f"Could not download {name}: {exc}")
        self._failed.pop(name, None)
        self.stale.discard(name)
        lower = {k.lower(): v for k, v in resp_headers.items()}
        if status == 304 and entry is not None:
            entry.fetched_at = now
            self._memory[name] = entry
            self._save_disk(name, entry)
            return entry
        text = body.decode("utf-8-sig")
        if not text.lstrip().startswith(("week_ended", "quarter_ended")):
            # A 200 that is not our CSV (an HTML error page, a captive portal) must never replace data.
            return self._fall_back(name, entry, now, f"{name} did not look like an AfriRadar CSV")
        fresh = _Cached(text=text, fetched_at=now, etag=lower.get("etag", ""),
                        last_modified=lower.get("last-modified", ""))
        self._memory[name] = fresh
        self._save_disk(name, fresh)
        return fresh

    def _fall_back(self, name: str, entry: Optional[_Cached], now: float, message: str) -> _Cached:
        """Serve the saved copy (marked stale) and hold off retrying for RETRY_AFTER seconds."""
        self._failed[name] = (now, message)
        if entry is None:
            raise DataError(message)
        self.stale.add(name)
        # Pretend it was fetched so it is reused until RETRY_AFTER has passed, not refetched per call.
        entry.fetched_at = max(entry.fetched_at, now - self.ttl + RETRY_AFTER)
        self._memory[name] = entry
        return entry

    def _parsed(self, name: str, parser: Callable[[str], list]) -> list:
        with self._lock(name):
            entry = self._get(name)
            if "rows" not in entry.parsed:
                entry.parsed["rows"] = parser(entry.text)
            return entry.parsed["rows"]

    # -- typed views -------------------------------------------------------------------------
    def latest(self, category: str) -> list[Fund]:
        return self._parsed(f"{CATEGORIES[category][0]}.csv", parse_funds)

    def history(self, category: str) -> list[Fund]:
        return self._parsed(f"{CATEGORIES[category][0]}-history.csv", parse_funds)

    def all_latest(self) -> list[Fund]:
        """Every category. A category that cannot be read is left out (and listed in
        ``unavailable``) instead of failing lookups in the categories that did load."""
        from concurrent.futures import ThreadPoolExecutor

        def one(category: str):
            try:
                return category, self.latest(category), None
            except DataError as exc:
                return category, [], exc

        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(one, CATEGORIES))
        errors = [exc for _c, _f, exc in results if exc]
        if len(errors) == len(results):
            raise errors[0]
        self.unavailable = {c for c, _f, exc in results if exc}
        return [fund for _c, group, _e in results for fund in group]

    def kenya(self) -> list[KenyaScheme]:
        return self._parsed(f"{KENYA_STEM}.csv", parse_kenya)

    unavailable: set = set()

    def stale_names(self) -> list[str]:
        return sorted(self.stale)

    def is_stale(self) -> bool:
        return bool(self.stale)
