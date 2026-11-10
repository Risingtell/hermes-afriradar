"""Tool handlers. Every handler takes the model's arguments as a dict and returns a JSON string,
success or error, and never raises (Hermes plugin rule)."""

from __future__ import annotations

import difflib
import json
import re
import statistics
from datetime import date
from typing import Any, Callable, Optional

from .data import CATEGORIES, DataError, DataSource, Fund, normalize_category

DISCLAIMER = ("AfriRadar publishes regulator data, not investment advice. Yields are what each manager "
              "reported to SEC and a past yield does not promise a future one. Check with the fund "
              "manager before you invest.")
MIN_ALTERNATIVE_NGN = 1_000_000_000  # never point anyone at a tiny fund as an "alternative"
WATCH_LIMIT = 25


class Store:
    """Tiny key-value seam over ctx.state so tests can use a dict."""

    def __init__(self, getter: Callable[[str, Any], Any], setter: Callable[[str, Any], None]):
        self.get = getter
        self.set = setter


class Runtime:
    """Everything a handler needs, built once in register()."""

    def __init__(self, source: DataSource, store: Store, alert_bps: int = 50):
        self.source = source
        self.store = store
        self.alert_bps = max(1, int(alert_bps))


def _ok(payload: dict, rt: Runtime) -> str:
    payload.setdefault("disclaimer", DISCLAIMER)
    if rt.source.is_stale():
        payload["data_status"] = ("Could not reach afriradar.com for " + ", ".join(rt.source.stale_names())
                                  + "; those figures come from the copy saved on this machine and may be out of date.")
    if getattr(rt.source, "unavailable", None):
        payload["unavailable_categories"] = sorted(rt.source.unavailable)
    return json.dumps(payload, ensure_ascii=False)


def _err(message: str, **extra) -> str:
    return json.dumps({"error": message, **extra}, ensure_ascii=False)


def _safe(fn):
    def wrapper(rt: Runtime, args: dict, **kwargs) -> str:
        try:
            return fn(rt, args or {}, **kwargs)
        except DataError as exc:
            return _err(str(exc), hint="Check the internet connection; AfriRadar data is read from afriradar.com.")
        except ValueError as exc:
            return _err(str(exc))
        except Exception as exc:  # never let a tool call crash the turn
            return _err(f"Unexpected error: {type(exc).__name__}: {exc}")
    wrapper.__name__ = fn.__name__
    return wrapper


def _int(value, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _float(value, name: str) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        text = re.sub(r"(ngn|naira|usd|us\$|\$|₦|,|\s)", "", value.lower())
        text = re.sub(r"^n(?=[0-9])", "", text)  # "N500k"
        match = re.fullmatch(r"(-?[0-9]*\.?[0-9]+)(k|m|mn|b|bn)?", text)
        if not match:
            raise ValueError(f"{name} must be a number, like 500000 or 500k")
        multiplier = {None: 1, "k": 1e3, "m": 1e6, "mn": 1e6, "b": 1e9, "bn": 1e9}[match.group(2)]
        return float(match.group(1)) * multiplier
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    return float(value)


def _country(args: dict) -> str:
    country = str(args.get("country") or "nigeria").strip().lower()
    if country in ("ng", "nigeria", "nga"):
        return "nigeria"
    if country in ("ke", "kenya", "ken"):
        return "kenya"
    raise ValueError("country must be nigeria or kenya")


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower().replace("&", " and ")).strip()


def _ranked(funds: list[Fund]) -> list[Fund]:
    with_yield = [f for f in funds if f.reported_yield is not None]
    return sorted(with_yield, key=lambda f: (-f.reported_yield, -(f.nav_ngn or 0)))


def _rank_of(fund: Fund, peers: list[Fund]) -> Optional[dict]:
    order = _ranked(peers)
    for index, other in enumerate(order, start=1):
        if other.key == fund.key:
            return {"position": index, "of": len(order), "by": "reported yield in its category"}
    return None


def resolve_fund(rt: Runtime, ref: str, category: Optional[str] = None) -> tuple[Optional[Fund], list[Fund]]:
    """(match, candidates). A fund id ("money-market/aiico-money-market-fund"), a slug or a name.

    An exact id, slug or name wins. Otherwise names and managers are scored with difflib and word
    overlap; a clear winner is returned, close calls come back as candidates so the model can ask.
    """
    text = str(ref or "").strip()
    if not text:
        raise ValueError("Give a fund name or id")
    if "/" in text:
        prefix, _, slug = text.partition("/")
        if prefix.strip().lower() == "kenya":
            raise ValueError("Kenyan schemes have no weekly fund detail; CMA publishes quarterly assets only. "
                             "Use afriradar_top_funds or afriradar_market_overview with country kenya.")
        try:
            cat = normalize_category(prefix) or ""
        except ValueError:
            cat = ""  # "ARM/Meristem" and the like: fall through to the name search
        for fund in rt.source.latest(cat) if cat in CATEGORIES else []:
            if fund.slug == slug.strip().lower():
                return fund, []
    pool = rt.source.latest(category) if category else rt.source.all_latest()
    wanted = _norm(text)
    for fund in pool:
        if fund.slug == text.lower() or _norm(fund.name) == wanted:
            return fund, []
    inside = [f for f in pool if wanted and wanted in _norm(f.name)]
    if len(inside) == 1:
        return inside[0], []
    scored = _score(pool, text)
    if not scored or scored[0][0] < 0.4:
        return None, []
    best = scored[0][0]
    close = [f for s, f in scored if best - s < 0.1]
    if best >= 0.75 and len(close) == 1:
        return close[0], []
    candidates = [f for s, f in scored if s >= max(0.4, best * 0.8)]
    brand = _brand(pool, text)
    if brand and brand in _tokens(scored[0][1].name + " " + scored[0][1].manager):
        candidates = [f for f in candidates if brand in _tokens(f.name + " " + f.manager)]
    if len(candidates) == 1 and best >= 0.6:
        return candidates[0], []
    return None, candidates[:6]


_STOP = {"fund", "funds", "the", "of", "and", "plc", "limited", "ltd", "my", "a", "in"}


_EXPAND = {"mmf": "money market", "mm": "money market", "usd": "dollar", "eur": "eurobond",
           "fi": "fixed income", "sharia": "shariah", "islamic": "halal"}


def _tokens(text: str) -> list[str]:
    words = []
    for w in _norm(text).split():
        words.extend(_EXPAND.get(w, w).split())
    return [w for w in words if w not in _STOP]


def _brand(pool: list[Fund], query: str) -> str:
    """The query word that names the fewest funds (usually the manager's brand), if it names any."""
    counts = {}
    for word in set(_tokens(query)):
        n = sum(1 for f in pool if word in _tokens(f.name + " " + f.manager))
        if n:
            counts[word] = n
    return min(counts, key=counts.get) if counts else ""


def _score(pool: list[Fund], query: str) -> list[tuple[float, Fund]]:
    """Rank funds by how much of the query's *distinctive* wording they contain.

    Each query word is weighted by inverse document frequency over the pool, so a brand like
    "Cowry" counts far more than "money" or "market", which half the funds share. A word that is a
    near miss of a fund word ("mony" for "money") counts at 80%.
    """
    import math

    words = _tokens(query)
    if not words:
        return []
    fund_tokens = [(f, set(_tokens(f.name)), set(_tokens(f.manager))) for f in pool]
    df: dict[str, int] = {}
    for _f, name_t, manager_t in fund_tokens:
        for token in name_t | manager_t:
            df[token] = df.get(token, 0) + 1
    vocabulary = list(df)
    total = len(pool)

    def idf(token: str) -> float:
        return math.log((total + 1) / (df.get(token, 0) + 1)) + 0.1

    resolved: list[tuple[str, float]] = []  # (token the word stands for, weight)
    for word in words:
        if word in df:
            resolved.append((word, idf(word)))
            continue
        near = difflib.get_close_matches(word, vocabulary, n=1, cutoff=0.8)
        prefix = [t for t in vocabulary if len(word) >= 3 and t.startswith(word)]
        target = near[0] if near else (prefix[0] if len(prefix) == 1 else "")
        resolved.append((target, idf(target) if target else idf("")))
    denominator = sum(weight for _t, weight in resolved) or 1.0
    exact = set(df)
    out: list[tuple[float, Fund]] = []
    for fund, name_t, manager_t in fund_tokens:
        score = 0.0
        for (target, weight), word in zip(resolved, words):
            factor = 1.0 if word in exact else 0.8
            if target and target in name_t:
                score += weight * factor
            elif target and target in manager_t:
                score += weight * factor * 0.7
        if score:
            out.append((score / denominator, fund))
    out.sort(key=lambda pair: (-pair[0], -(pair[1].nav_ngn or 0)))
    return out


def _not_found(ref: str, candidates: list[Fund]) -> str:
    if candidates:
        wanted = set(_tokens(str(ref)))
        full = [f for f in candidates if wanted <= set(_tokens(f.name + " " + f.manager))]
        message = (f"More than one fund matches {ref!r}." if full else
                   f"No fund in SEC's latest report is called {ref!r}; these are the closest.")
        return _err(message + " Ask the user which one they mean, then call again with its id.",
                    candidates=[{"fund": f.name, "id": f.key, "manager": f.manager, "category": f.category}
                                for f in candidates])
    return _err(f"No fund matches {ref!r} in SEC Nigeria's latest report. Try afriradar_find_funds.")


# -- tools ------------------------------------------------------------------------------------

@_safe
def market_overview(rt: Runtime, args: dict, **_) -> str:
    if _country(args) == "kenya":
        schemes = rt.source.kenya()
        total = sum(s.aum_kes or 0 for s in schemes)
        biggest = sorted(schemes, key=lambda s: -(s.aum_kes or 0))[:5]
        return _ok({
            "market": "Kenya unit trust schemes (CMA Kenya quarterly bulletin)",
            "quarter_ended": schemes[0].quarter if schemes else None,
            "schemes": len(schemes),
            "total_aum_kes": round(total),
            "largest": [s.summary() for s in biggest],
            "source": schemes[0].source_url if schemes else None,
        }, rt)
    funds = rt.source.all_latest()
    if not funds:
        raise DataError("The SEC report files are empty")
    week = max(f.week for f in funds)
    categories = []
    for cat, (_stem, label, _path) in CATEGORIES.items():
        group = [f for f in funds if f.category == cat]
        yields = [f.reported_yield for f in group if f.reported_yield is not None]
        top = _ranked(group)[:1]
        categories.append({
            "category": cat, "label": label, "funds": len(group),
            "nav_ngn": round(sum(f.nav_ngn or 0 for f in group)),
            "median_yield_pct": round(statistics.median(yields) * 100, 2) if yields else None,
            "highest_yield": top[0].summary() if top else None,
        })
    return _ok({
        "market": "Nigeria collective investment schemes (SEC Nigeria weekly valuation report)",
        "week_ended": week,
        "funds": len(funds),
        "total_nav_ngn": round(sum(f.nav_ngn or 0 for f in funds)),
        "unitholders": sum(f.unitholders or 0 for f in funds),
        "categories": categories,
        "source": funds[0].source_url,
    }, rt)


@_safe
def find_funds(rt: Runtime, args: dict, **_) -> str:
    query = str(args.get("query") or "").strip()
    limit = _int(args.get("limit"), 10, 1, 50)
    if _country(args) == "kenya":
        schemes = rt.source.kenya()
        wanted = _norm(query)
        hits = [s for s in schemes if not wanted or wanted in _norm(s.name) or wanted in s.key]
        return _ok({"query": query, "matches": [s.summary() for s in hits[:limit]], "total": len(hits)}, rt)
    category = normalize_category(args.get("category"))
    pool = rt.source.latest(category) if category else rt.source.all_latest()
    if not query:
        hits = _ranked(pool) or pool
    else:
        wanted = _norm(query)
        words = [w for w in wanted.split() if w not in ("fund", "funds")]
        hits = [f for f in pool
                if wanted in _norm(f.name) or wanted in _norm(f.manager)
                or (words and all(w in _norm(f.name + " " + f.manager) for w in words))]
        if not hits:
            match, candidates = resolve_fund(rt, query, category)
            hits = [match] if match else candidates
    return _ok({"query": query, "category": category, "total": len(hits),
                "matches": [f.summary() for f in hits[:limit]]}, rt)


@_safe
def top_funds(rt: Runtime, args: dict, **_) -> str:
    limit = _int(args.get("limit"), 10, 1, 50)
    by = str(args.get("by") or "yield").strip().lower()
    if _country(args) == "kenya":
        schemes = rt.source.kenya()
        key = ((lambda s: -s.change if s.change is not None else float("inf"))
               if by in ("growth", "change") else (lambda s: -(s.aum_kes or 0)))
        return _ok({"market": "Kenya unit trust schemes", "by": "quarter growth" if by in ("growth", "change") else "assets",
                    "funds": [s.summary() for s in sorted(schemes, key=key)[:limit]]}, rt)
    category = normalize_category(args.get("category")) or "money-market"
    min_size = _float(args.get("min_size_ngn"), "min_size_ngn") or 0
    pool = [f for f in rt.source.latest(category) if (f.nav_ngn or 0) >= min_size]
    if by in ("yield", "return", "returns"):
        ordered, by = _ranked(pool), "yield"
    elif by in ("size", "nav", "aum", "assets"):
        ordered, by = sorted(pool, key=lambda f: -(f.size or 0)), "size"
    elif by in ("unitholders", "investors", "popularity"):
        ordered, by = sorted(pool, key=lambda f: -(f.unitholders or 0)), "unitholders"
    else:
        raise ValueError("by must be yield, size or unitholders")
    return _ok({
        "category": category, "label": CATEGORIES[category][1], "by": by,
        "week_ended": pool[0].week if pool else None, "min_size_ngn": min_size or None,
        "funds_considered": len(pool),
        "funds": [{"rank": i, **f.summary()} for i, f in enumerate(ordered[:limit], start=1)],
    }, rt)


def _growth_rate(rt: Runtime, fund: Fund) -> tuple[Optional[float], str]:
    """(yearly rate, how it was worked out) for projecting a fund forward.

    Money market funds: the yield the manager reports, which is an annual rate. Every other
    category: SEC's year-to-date column is the return since 1 January, not a yearly rate, so the
    rate comes from the fund's own unit price over the last year of weekly reports (at least six
    months, because a few weeks of prices swing too much), annualised.
    """
    if fund.category == "money-market":
        if fund.reported_yield is None:
            return None, "the manager reported no yield to SEC this week."
        return fund.reported_yield, "the yield reported to SEC this week (an annual rate for money market funds)"
    rows = [r for r in _series(rt, fund, 53) if r.unit_price]
    if len(rows) < 26 or not fund.unit_price:
        return None, ("fewer than 26 weeks of unit prices on file; a shorter run of prices is too "
                      "noisy to turn into a yearly rate. Ask the user for a yield_pct to assume.")
    first, last = rows[0], rows[-1]
    days = (date.fromisoformat(last.week) - date.fromisoformat(first.week)).days
    if days < 182:
        return None, ("less than six months of unit prices on file; a shorter run of prices is too "
                      "noisy to turn into a yearly rate. Ask the user for a yield_pct to assume.")
    growth = last.unit_price / first.unit_price
    rate = growth ** (365 / days) - 1
    return rate, (f"the unit price moved {(growth - 1) * 100:+.2f}% from {first.week} to {last.week}, "
                  f"annualised; past price moves do not repeat on schedule")


def _series(rt: Runtime, fund: Fund, weeks: int) -> list[Fund]:
    rows = [f for f in rt.source.history(fund.category) if f.key == fund.key]
    rows.sort(key=lambda f: f.week)
    return rows[-weeks:]


def _change(rows: list[Fund], back: int) -> Optional[float]:
    """Percentage-point change in reported yield over the last `back` weeks."""
    if len(rows) <= back:
        return None
    now, then = rows[-1].reported_yield, rows[-1 - back].reported_yield
    if now is None or then is None:
        return None
    return round((now - then) * 100, 2)


@_safe
def fund_detail(rt: Runtime, args: dict, **_) -> str:
    ref = args.get("fund")
    fund, candidates = resolve_fund(rt, ref, normalize_category(args.get("category")))
    if not fund:
        return _not_found(ref, candidates)
    weeks = _int(args.get("weeks"), 13, 2, 120)
    rows = _series(rt, fund, max(weeks, 14))
    peers = rt.source.latest(fund.category)
    peer_yields = [f.reported_yield for f in peers if f.reported_yield is not None]
    first_holders = next((r.unitholders for r in rows if r.unitholders), None)
    return _ok({
        **fund.summary(),
        "rank": _rank_of(fund, peers),
        "category_median_yield_pct": round(statistics.median(peer_yields) * 100, 2) if peer_yields else None,
        "yield_change_pp": {"1_week": _change(rows, 1), "4_weeks": _change(rows, 4), "13_weeks": _change(rows, 13)},
        "unitholders_change": (fund.unitholders - first_holders) if fund.unitholders and first_holders else None,
        "history": [{"week_ended": r.week, "yield_pct": round(r.reported_yield * 100, 2) if r.reported_yield else None,
                     "nav_ngn": round(r.nav_ngn) if r.nav_ngn else None, "unitholders": r.unitholders}
                    for r in rows[-weeks:]],
        "source": fund.source_url,
    }, rt)


@_safe
def compare_funds(rt: Runtime, args: dict, **_) -> str:
    refs = args.get("funds") or []
    if isinstance(refs, str):
        refs = [r for r in re.split(r"\s*(?:,|;|\bvs\.?\b|\bversus\b)\s*", refs) if r]
    if not 2 <= len(refs) <= 6:
        raise ValueError("Give between 2 and 6 funds to compare")
    rows, problems = [], []
    for ref in refs:
        fund, candidates = resolve_fund(rt, ref)
        if not fund:
            problems.append({"asked": ref, "candidates": [{"fund": f.name, "id": f.key} for f in candidates]})
            continue
        series = [r.reported_yield for r in _series(rt, fund, 13) if r.reported_yield is not None]
        rows.append({
            **fund.summary(),
            "rank": _rank_of(fund, rt.source.latest(fund.category)),
            "yield_13w_low_pct": round(min(series) * 100, 2) if series else None,
            "yield_13w_high_pct": round(max(series) * 100, 2) if series else None,
            "yield_13w_average_pct": round(statistics.mean(series) * 100, 2) if series else None,
        })
    if problems and len(rows) < 2:
        return _err("Some funds could not be identified. Ask the user which they meant.", unresolved=problems)
    mixed = len({r["category"] for r in rows}) > 1
    return _ok({"funds": rows, "unresolved": problems or None,
                "note": "These funds are in different categories, so their yields measure different things."
                if mixed else None}, rt)


@_safe
def project_returns(rt: Runtime, args: dict, **_) -> str:
    amount = _float(args.get("amount"), "amount") or 0
    monthly = _float(args.get("monthly_contribution"), "monthly_contribution") or 0
    months = _int(args.get("months"), 12, 1, 360)
    if amount <= 0 and monthly <= 0:
        raise ValueError("Give an amount, a monthly_contribution, or both")
    if amount < 0 or monthly < 0:
        raise ValueError("Amounts cannot be negative")
    rate_pct = _float(args.get("yield_pct"), "yield_pct")
    fund_info = None
    if args.get("fund"):
        fund, candidates = resolve_fund(rt, args["fund"])
        if not fund:
            return _not_found(args["fund"], candidates)
        rate, basis = _growth_rate(rt, fund)
        if rate is None:
            return _err(f"Not enough data to project {fund.name}: {basis}")
        rate_pct = rate * 100
        fund_info = {**fund.summary(), "rate_basis": basis}
    if rate_pct is None:
        raise ValueError("Give a fund or a yield_pct")
    if not -50 <= rate_pct <= 150:
        raise ValueError("yield_pct must be between -50 and 150")
    monthly_rate = (1 + rate_pct / 100) ** (1 / 12) - 1
    balance, paid_in = amount, amount
    schedule = []
    for month in range(1, months + 1):
        balance = balance * (1 + monthly_rate) + monthly
        paid_in += monthly
        if month % 12 == 0 or month == months:
            schedule.append({"month": month, "balance": round(balance, 2), "paid_in": round(paid_in, 2)})
    currency = "USD" if fund_info and fund_info.get("category") == "dollar" else "NGN"
    return _ok({
        "fund": fund_info,
        "assumed_yield_pct": round(rate_pct, 2),
        "currency": currency,
        "start_amount": amount, "monthly_contribution": monthly, "months": months,
        "end_balance": round(balance, 2), "total_paid_in": round(paid_in, 2),
        "estimated_earnings": round(balance - paid_in, 2),
        "schedule": schedule,
        "method": "Yield held constant, compounded monthly, contributions at month end, before fees and "
                  "withholding tax. Real yields move every week.",
    }, rt)


# -- watchlist --------------------------------------------------------------------------------

def _watchlist(rt: Runtime) -> list[dict]:
    items = rt.store.get("watchlist", [])
    return items if isinstance(items, list) else []


def _snapshot(fund: Fund, peers: list[Fund]) -> dict:
    rank = _rank_of(fund, peers)
    return {"week": fund.week, "yield": fund.reported_yield, "nav_ngn": fund.nav_ngn,
            "unitholders": fund.unitholders, "rank": rank["position"] if rank else None,
            "price": fund.unit_price}


@_safe
def watchlist(rt: Runtime, args: dict, **_) -> str:
    action = str(args.get("action") or "list").strip().lower()
    items = _watchlist(rt)
    if action == "list":
        return _ok({"watching": items, "count": len(items),
                    "tip": "Run afriradar_watch_report to see what changed, or schedule /blueprint afriradar:weekly-fund-watch."}, rt)
    if action == "clear":
        rt.store.set("watchlist", [])
        return _ok({"cleared": len(items)}, rt)
    ref = args.get("fund")
    if action == "remove":
        raw = str(ref or "").strip().lower()
        wanted = _norm(raw)
        keep = [i for i in items if raw != i["id"].lower() and wanted != _norm(i["name"])
                and raw != i["id"].split("/")[-1]]
        if len(keep) == len(items):
            fund, _ = resolve_fund(rt, ref)
            keep = [i for i in items if not fund or i["id"] != fund.key]
        if len(keep) == len(items):
            return _err(f"{ref!r} is not on the watchlist", watching=[i["name"] for i in items])
        rt.store.set("watchlist", keep)
        return _ok({"removed": [i["name"] for i in items if i not in keep], "watching": len(keep)}, rt)
    if action not in ("add", "update"):
        raise ValueError("action must be add, update, remove, list or clear")
    fund, candidates = resolve_fund(rt, ref, normalize_category(args.get("category")))
    if not fund:
        return _not_found(ref, candidates)
    amount = _float(args.get("amount"), "amount")
    existing = next((i for i in items if i["id"] == fund.key), None)
    if existing:
        if amount is not None:
            existing["amount"] = amount
        if args.get("note") is not None:
            existing["note"] = str(args["note"])[:200]
        rt.store.set("watchlist", items)
        return _ok({"updated": existing}, rt)
    if len(items) >= WATCH_LIMIT:
        raise ValueError(f"The watchlist holds at most {WATCH_LIMIT} funds")
    entry = {
        "id": fund.key, "name": fund.name, "category": fund.category, "manager": fund.manager,
        "amount": amount, "currency": "USD" if fund.category == "dollar" else "NGN",
        "note": str(args.get("note") or "")[:200] or None,
        "added_week": fund.week, "last_seen": _snapshot(fund, rt.source.latest(fund.category)),
    }
    items.append(entry)
    rt.store.set("watchlist", items)
    return _ok({"added": entry, "watching": len(items), **fund.summary()}, rt)


def _alternatives(fund: Fund, peers: list[Fund]) -> list[dict]:
    better = [f for f in _ranked(peers)
              if f.key != fund.key and (f.nav_ngn or 0) >= MIN_ALTERNATIVE_NGN
              and fund.reported_yield is not None and f.reported_yield > fund.reported_yield]
    return [{"fund": f.name, "id": f.key, "yield_pct": round(f.reported_yield * 100, 2),
             "nav_ngn": round(f.nav_ngn), "url": f.url} for f in better[:3]]


@_safe
def watch_report(rt: Runtime, args: dict, **_) -> str:
    """Compare every watched fund with the snapshot taken at the last report, then (by default)
    store the new snapshot so the next report only shows what is new."""
    items = _watchlist(rt)
    if not items:
        return _ok({"watching": 0, "message": "The watchlist is empty. Add a fund with afriradar_watchlist "
                                               "(action add) first."}, rt)
    # Default False: a question in chat must not use up the changes the weekly automation reports.
    mark_seen = args.get("mark_seen", False) in (True, "true", "yes", 1)
    threshold = rt.alert_bps
    lines, alerts, new_week = [], 0, False
    for item in items:
        category = item.get("category")
        if category not in CATEGORIES:
            continue
        peers = rt.source.latest(category)
        fund = next((f for f in peers if f.key == item["id"]), None)
        last = item.get("last_seen") or {}
        if fund is None:
            lines.append({"fund": item["name"], "id": item["id"], "status": "missing",
                          "message": "Not in SEC's latest report. The manager may have skipped a week, or the fund was renamed."})
            alerts += 1
            continue
        is_new = fund.week != last.get("week")
        new_week = new_week or is_new
        change_bps = None
        if fund.reported_yield is not None and last.get("yield") is not None:
            change_bps = round((fund.reported_yield - last["yield"]) * 10000)
        rank = _rank_of(fund, peers)
        nav_change = None
        if fund.nav_ngn and last.get("nav_ngn"):
            nav_change = round((fund.nav_ngn / last["nav_ngn"] - 1) * 100, 2)
        flagged = change_bps is not None and abs(change_bps) >= threshold
        alerts += 1 if flagged else 0
        row = {
            "fund": fund.name, "id": fund.key, "week_ended": fund.week, "previous_week": last.get("week"),
            "new_data": is_new,
            "yield_pct": round(fund.reported_yield * 100, 2) if fund.reported_yield is not None else None,
            "yield_change_bps": change_bps, "alert": flagged,
            "rank": rank, "rank_before": last.get("rank"),
            "fund_size_change_pct": nav_change,
            "unitholders": fund.unitholders,
            "higher_yielding_peers": _alternatives(fund, peers) if flagged and change_bps < 0 else None,
            "url": fund.url,
        }
        if item.get("amount"):
            row["your_amount"] = item["amount"]
            if fund.category == "money-market" and fund.reported_yield:
                # The same monthly compounding afriradar_project_returns uses, so the two always agree.
                yearly = item["amount"] * fund.reported_yield
                monthly = item["amount"] * ((1 + fund.reported_yield) ** (1 / 12) - 1)
                row["income_at_this_yield"] = {"per_year": round(yearly, 2), "per_month": round(monthly, 2),
                                               "currency": item.get("currency", "NGN"),
                                               "basis": "yield held constant, compounded monthly, before fees and tax"}
            elif fund.unit_price and last.get("price"):
                moved = fund.unit_price / last["price"] - 1
                row["unit_price_change_pct"] = round(moved * 100, 2)
                row["your_value_change"] = {"amount": round(item["amount"] * moved, 2),
                                            "currency": item.get("currency", "NGN"),
                                            "basis": f"unit price {last.get('week')} to {fund.week}"}
        lines.append({k: v for k, v in row.items() if v is not None})
        if mark_seen:
            item["last_seen"] = _snapshot(fund, peers)
    if mark_seen:
        rt.store.set("watchlist", items)
    return _ok({
        "watching": len(items),
        "new_report": new_week,
        "alerts": alerts,
        "alert_threshold_bps": threshold,
        "funds": lines,
        "summary_hint": ("No new SEC report since the last check. Say so in one line." if not new_week and not alerts
                         else "Lead with alerts, then one line per fund: yield, change in basis points, rank."),
    }, rt)
