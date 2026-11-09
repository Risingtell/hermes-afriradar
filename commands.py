"""The /afriradar slash command: quick answers with no model call, so it is instant and free in
the CLI and in every messaging channel."""

from __future__ import annotations

import json

from . import tools
from .data import CATEGORIES, normalize_category

HELP = """AfriRadar: African fund data from SEC Nigeria and CMA Kenya.

/afriradar                 top 5 money market funds over N1bn by yield
/afriradar top <category>  top 10 in a category (dollar, bond, equity, shariah...)
/afriradar <fund name>     one fund: yield, rank, 4 and 13 week change
/afriradar watch           your watchlist, what changed (does not mark it seen)
/afriradar market          the whole Nigerian market in numbers
/afriradar kenya           Kenya unit trust schemes by assets

Ask Hermes in plain words for more, e.g. "I have N2m in Cowry money market fund, watch it for me"."""


def _naira(value) -> str:
    if value is None:
        return "-"
    value = float(value)
    for size, suffix in ((1e12, "trn"), (1e9, "bn"), (1e6, "m")):
        if abs(value) >= size:
            return f"N{value / size:,.2f}{suffix}"
    return f"N{value:,.0f}"


def _pct(value) -> str:
    return "not reported" if value is None else f"{value:.2f}%"


def _signed(value, unit: str) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.2f}{unit}" if unit == "pp" else f"{value:+d}{unit}"


def _load(raw: str) -> dict:
    return json.loads(raw)


MIN_SIZE_NGN = 1_000_000_000  # quick views leave out funds under N1bn


def _top(rt, category: str, limit: int) -> str:
    data = _load(tools.top_funds(rt, {"category": category, "limit": limit, "min_size_ngn": MIN_SIZE_NGN}))
    if "error" in data:
        return data["error"]
    lines = [f"{data['label']} over N1bn, week ended {data['week_ended']} (year-to-date yield reported to SEC):"]
    for fund in data["funds"]:
        size = f"${fund['nav_usd']:,.0f}" if fund.get("nav_usd") else _naira(fund.get("nav_ngn"))
        lines.append(f"{fund['rank']:>2}. {fund['fund']}  {_pct(fund.get('yield_pct'))}  ({size})")
    return "\n".join(lines)


def _fund(rt, ref: str) -> str:
    data = _load(tools.fund_detail(rt, {"fund": ref, "weeks": 13}))
    if "error" in data:
        options = data.get("candidates") or []
        if options:
            return data["error"].split(". Ask")[0] + ":\n" + "\n".join(f"- {c['fund']}" for c in options)
        return data["error"]
    rank = data.get("rank") or {}
    change = data.get("yield_change_pp") or {}
    return "\n".join([
        f"{data['fund']} ({data['manager']})",
        f"Week ended {data['week_ended']}: yield {_pct(data.get('yield_pct'))}, "
        f"rank {rank.get('position', '-')} of {rank.get('of', '-')} in {CATEGORIES[data['category']][1].lower()}",
        f"Change: 1 week {_signed(change.get('1_week'), 'pp')}, 4 weeks {_signed(change.get('4_weeks'), 'pp')}, "
        f"13 weeks {_signed(change.get('13_weeks'), 'pp')}",
        f"Size {_naira(data.get('nav_ngn'))}"
        + (f", {data['unitholders']:,} unitholders" if isinstance(data.get("unitholders"), int) else ""),
        data["url"],
    ])


def _watch(rt) -> str:
    data = _load(tools.watch_report(rt, {"mark_seen": False}))
    if "error" in data:
        return data["error"]
    if not data.get("funds"):
        return data.get("message", "The watchlist is empty.")
    lines = [f"Watching {data['watching']} fund(s); alerts: {data['alerts']}"]
    for fund in data["funds"]:
        if fund.get("status") == "missing":
            lines.append(f"! {fund['fund']}: not in SEC's latest report")
            continue
        flag = "! " if fund.get("alert") else "- "
        rank = fund.get("rank") or {}
        bps = fund.get("yield_change_bps")
        moved = "no change" if not bps else f"{bps:+d} bps since last report"
        lines.append(f"{flag}{fund['fund']}: {_pct(fund.get('yield_pct'))} ({moved}), "
                     f"rank {rank.get('position', '-')}/{rank.get('of', '-')}")
        income = fund.get("income_at_this_yield")
        if income and income.get("currency") == "NGN":
            lines.append(f"  your {_naira(fund['your_amount'])} earns about {_naira(income['per_month'])} a month at this yield")
    return "\n".join(lines)


def _market(rt) -> str:
    data = _load(tools.market_overview(rt, {}))
    if "error" in data:
        return data["error"]
    lines = [f"Nigeria, week ended {data['week_ended']}: {data['funds']} funds, {_naira(data['total_nav_ngn'])}, "
             f"{data['unitholders']:,} unitholders"]
    for cat in data["categories"]:
        lines.append(f"- {cat['label']}: {cat['funds']} funds, {_naira(cat['nav_ngn'])}, "
                     f"median yield {_pct(cat.get('median_yield_pct'))}")
    return "\n".join(lines)


def _kenya(rt) -> str:
    data = _load(tools.top_funds(rt, {"country": "kenya", "limit": 10}))
    if "error" in data:
        return data["error"]
    lines = ["Kenya unit trust schemes by assets (CMA quarterly bulletin):"]
    for i, scheme in enumerate(data["funds"], start=1):
        lines.append(f"{i:>2}. {scheme['scheme']}  KES {scheme['aum_kes'] / 1e9:,.1f}bn  "
                     f"({scheme['quarter_change_pct']:+.1f}% on the quarter)" if scheme.get("quarter_change_pct") is not None
                     else f"{i:>2}. {scheme['scheme']}  KES {scheme['aum_kes'] / 1e9:,.1f}bn")
    return "\n".join(lines)


def handle(rt, raw_args: str) -> str:
    text = (raw_args or "").strip()
    lower = text.lower()
    try:
        if not text:
            return _top(rt, "money-market", 5)
        if lower in ("help", "-h", "--help", "?"):
            return HELP
        if lower in ("watch", "watchlist", "report"):
            return _watch(rt)
        if lower in ("market", "overview", "nigeria"):
            return _market(rt)
        if lower in ("kenya", "ke"):
            return _kenya(rt)
        if lower.startswith("top"):
            rest = text[3:].strip()
            return _top(rt, normalize_category(rest) or "money-market", 10)
        return _fund(rt, text)
    except ValueError as exc:
        return f"{exc}\n\n{HELP}"
    except Exception as exc:  # a slash command must never crash the session
        return f"AfriRadar could not answer: {exc}"
