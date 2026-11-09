"""AfriRadar for Hermes: African fund data from the regulators, a watchlist, and a weekly report.

Registers eight tools, the /afriradar slash command, a bundled skill, and two Automation
Blueprints. Data comes from AfriRadar's public CSV files (SEC Nigeria weekly, CMA Kenya
quarterly); no API key, no account, no payment.
"""

from __future__ import annotations

import logging
from functools import partial
from pathlib import Path

from . import commands, schemas, tools
from .data import DataSource

logger = logging.getLogger(__name__)

PLUGIN = "afriradar"
DEFAULT_DATA_URL = "https://www.afriradar.com/data/"

_HANDLERS = {
    "afriradar_market_overview": tools.market_overview,
    "afriradar_find_funds": tools.find_funds,
    "afriradar_top_funds": tools.top_funds,
    "afriradar_fund_detail": tools.fund_detail,
    "afriradar_compare_funds": tools.compare_funds,
    "afriradar_project_returns": tools.project_returns,
    "afriradar_watchlist": tools.watchlist,
    "afriradar_watch_report": tools.watch_report,
}

_EMOJI = {
    "afriradar_market_overview": "📊", "afriradar_find_funds": "🔎", "afriradar_top_funds": "🏆",
    "afriradar_fund_detail": "📈", "afriradar_compare_funds": "⚖️", "afriradar_project_returns": "🧮",
    "afriradar_watchlist": "👀", "afriradar_watch_report": "🛰️",
}


def _cache_dir() -> Path | None:
    """<hermes home>/plugin-data/afriradar/cache, resolved per call so it follows the profile."""
    try:
        from plugins.plugin_storage import plugin_data_dir
        return plugin_data_dir(PLUGIN) / "cache"
    except Exception as exc:  # pragma: no cover - only outside Hermes
        logger.debug("afriradar: no plugin data dir (%s); caching in memory only", exc)
        return None


class _LazyCacheSource(DataSource):
    """Resolves the cache directory when a file is read, not at registration, so register() does
    no filesystem work and the cache follows the active profile."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, cache_dir=None, **kwargs)

    def _paths(self, name):
        self.cache_dir = _cache_dir()  # every call, so a profile switch moves the cache with it
        return super()._paths(name)


def build_runtime(ctx) -> tools.Runtime:
    data_url = str(ctx.get_config("data_url", default="https://www.afriradar.com/data/") or
                   "https://www.afriradar.com/data/")
    try:
        cache_hours = float(ctx.get_config("cache_hours", default=6) or 6)
    except (TypeError, ValueError):
        cache_hours = 6
    try:
        alert_bps = int(ctx.get_config("alert_bps", default=50) or 50)
    except (TypeError, ValueError):
        alert_bps = 50
    try:
        source = _LazyCacheSource(data_url, cache_hours=cache_hours)
    except ValueError:
        logger.warning("afriradar: data_url %r is not an https:// URL; using %s", data_url, DEFAULT_DATA_URL)
        source = _LazyCacheSource(DEFAULT_DATA_URL, cache_hours=cache_hours)
    store = tools.Store(lambda key, default=None: ctx.state.get(key, default=default),
                        lambda key, value: ctx.state.set(key, value))
    return tools.Runtime(source, store, alert_bps=alert_bps)


def _register_skills(ctx) -> None:
    skills_dir = Path(__file__).parent / "skills"
    if not skills_dir.is_dir():
        return
    for child in sorted(skills_dir.iterdir()):
        skill_md = child / "SKILL.md"
        if child.is_dir() and skill_md.exists():
            ctx.register_skill(child.name, skill_md)


def _register_blueprints(ctx) -> None:
    deliver = {"name": "deliver", "type": "enum", "label": "Where to deliver?", "default": "origin",
               "options": ["origin", "local"], "strict": False}
    day = {"name": "day", "type": "enum", "label": "Which day?", "default": "saturday",
           "options": ["saturday", "sunday", "monday", "friday"]}
    ctx.register_automation_blueprint(
        "weekly-fund-watch",
        title="Weekly fund watch",
        description=("Once a week, check every fund on your AfriRadar watchlist against SEC Nigeria's newest "
                     "report and tell you what moved: yields, rank, and your income at the new yield."),
        category="weekly",
        schedule_template="{minute} {hour} * * {dow}",
        prompt_template=(
            "Call afriradar_watch_report with mark_seen true. If it says there is no new SEC report and no alerts, reply with "
            "one short line saying nothing changed and the week the data is from. Otherwise write a short "
            "message: alerts first (yield moves past the threshold, funds missing from the report), then "
            "one line per watched fund with its yield, the change in basis points and its rank. If a "
            "watched fund's yield fell, mention the higher-yielding peers the report lists as facts, not "
            "advice. Show naira as N with commas. End with: Data from SEC Nigeria via AfriRadar, not "
            "investment advice."
        ),
        slots=[{"name": "time", "type": "time", "label": "What time?", "default": "09:00"}, day, deliver],
        tags=["finance", "weekly", "nigeria"],
    )
    ctx.register_automation_blueprint(
        "money-market-digest",
        title="Money market digest",
        description="A weekly list of the highest-yielding Nigerian money market funds above a size you choose.",
        category="weekly",
        schedule_template="{minute} {hour} * * {dow}",
        prompt_template=(
            "Call afriradar_top_funds with category money-market, by yield, limit {count}, and min_size_ngn "
            "{min_size}. Reply with a numbered list: fund, yield, size in naira (N with bn or m). Say which "
            "SEC week the data is from and that yields are year-to-date figures reported by each manager. "
            "Finish with: not investment advice."
        ),
        slots=[
            {"name": "time", "type": "time", "label": "What time?", "default": "09:00"},
            day,
            {"name": "count", "type": "enum", "label": "How many funds?", "default": "10",
             "options": ["5", "10", "15"]},
            {"name": "min_size", "type": "enum", "label": "Smallest fund to include (naira)",
             "default": "5000000000", "options": ["0", "1000000000", "5000000000", "20000000000"],
             "strict": False},
            deliver,
        ],
        tags=["finance", "weekly", "nigeria"],
    )


def register(ctx) -> None:
    runtime = build_runtime(ctx)
    for schema in schemas.ALL:
        name = schema["name"]
        ctx.register_tool(
            name=name, toolset=PLUGIN, schema=schema,
            handler=partial(_HANDLERS[name], runtime),
            description=schema["description"].split(".")[0], emoji=_EMOJI.get(name, ""),
        )
    ctx.register_command(
        PLUGIN, lambda raw_args="", **_: commands.handle(runtime, raw_args),
        description="African fund data: top funds, one fund, your watchlist", args_hint="[fund | top <category> | watch | market | kenya | help]",
    )
    _register_skills(ctx)
    _register_blueprints(ctx)
