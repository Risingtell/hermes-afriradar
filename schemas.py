"""Tool schemas: what the model reads to decide when and how to call each tool."""

_CATEGORY = {
    "type": "string",
    "description": ("SEC Nigeria category: money-market, dollar, bond, equity, balanced, reit, shariah, "
                    "ethical or specialised. Common words work too (MMF, eurobond, fixed income, Islamic)."),
}
_COUNTRY = {
    "type": "string",
    "enum": ["nigeria", "kenya"],
    "description": "nigeria (SEC weekly report, default) or kenya (CMA quarterly unit trust assets).",
}
_FUND = {
    "type": "string",
    "description": ("A fund name as the user said it (\"Cowry money market\"), or an exact id from an earlier "
                    "result such as \"money-market/cowry-money-market-fund\"."),
}

MARKET_OVERVIEW = {
    "name": "afriradar_market_overview",
    "description": (
        "Snapshot of a whole African fund market from the regulator's latest report. Nigeria: number of "
        "funds, total net asset value in naira, unitholders, and for each category the median yield and "
        "the highest-yielding fund. Kenya: total unit trust assets and the largest schemes. Use for "
        "questions like 'how big is Nigeria's mutual fund industry' or 'what are money market funds "
        "paying now'."
    ),
    "parameters": {"type": "object", "properties": {"country": _COUNTRY}},
}

FIND_FUNDS = {
    "name": "afriradar_find_funds",
    "description": (
        "Search Nigerian collective investment schemes (or Kenyan unit trust schemes) by fund name or "
        "manager, e.g. 'Stanbic', 'ARM', 'dollar fund', 'Meristem'. Returns each match with its id, "
        "category, latest reported yield, size and link. Use it to find the right id before other tools."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Words from the fund or manager name."},
            "category": _CATEGORY,
            "country": _COUNTRY,
            "limit": {"type": "integer", "description": "Maximum results, 1-50 (default 10)."},
        },
        "required": ["query"],
    },
}

TOP_FUNDS = {
    "name": "afriradar_top_funds",
    "description": (
        "Rank funds in one category from SEC Nigeria's latest weekly report: by reported yield (default), "
        "by size, or by number of unitholders. Use min_size_ngn to leave out very small funds. For Kenya, "
        "ranks unit trust schemes by assets or by quarterly growth. Use for 'best money market funds', "
        "'biggest dollar funds', 'highest yielding bond funds over N10bn'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "category": _CATEGORY,
            "by": {"type": "string", "enum": ["yield", "size", "unitholders", "growth"],
                   "description": "Sort order. growth applies to Kenya only."},
            "min_size_ngn": {"type": "number", "description": "Only funds at least this big, in naira (e.g. 10000000000 for N10bn)."},
            "limit": {"type": "integer", "description": "How many, 1-50 (default 10)."},
            "country": _COUNTRY,
        },
    },
}

FUND_DETAIL = {
    "name": "afriradar_fund_detail",
    "description": (
        "Everything about one Nigerian fund: latest yield, size, unitholders, its rank in its category, "
        "the category median, yield change over 1, 4 and 13 weeks, and its weekly history from SEC "
        "reports since January 2025, with the SEC source file. Use for 'how is my Cowry fund doing' or "
        "'show the yield trend of ARM money market'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "fund": _FUND,
            "category": _CATEGORY,
            "weeks": {"type": "integer", "description": "Weeks of history to return, 2-120 (default 13)."},
        },
        "required": ["fund"],
    },
}

COMPARE_FUNDS = {
    "name": "afriradar_compare_funds",
    "description": (
        "Side-by-side comparison of 2 to 6 Nigerian funds: latest yield, rank, size, unitholders and the "
        "low, high and average yield over the last 13 weeks. Use for 'Stanbic vs ARM money market' style "
        "questions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "funds": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 6,
                      "description": "Fund names or ids."},
        },
        "required": ["funds"],
    },
}

PROJECT_RETURNS = {
    "name": "afriradar_project_returns",
    "description": (
        "Arithmetic for 'what would N500k in this fund be worth in a year' or 'if I save N50k a month': "
        "projects a balance from a fund's current reported yield (or a yield you give), compounded "
        "monthly, with an optional monthly contribution. Always use this instead of doing the maths "
        "yourself, and say the yield is assumed constant."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "amount": {"type": "number", "description": "Starting amount in the fund's currency (naira, or dollars for dollar funds)."},
            "monthly_contribution": {"type": "number", "description": "Added at the end of every month."},
            "months": {"type": "integer", "description": "How many months, 1-360 (default 12)."},
            "fund": _FUND,
            "yield_pct": {"type": "number", "description": "Use this yearly yield in percent instead of a fund's, e.g. 18.5."},
        },
    },
}

WATCHLIST = {
    "name": "afriradar_watchlist",
    "description": (
        "Manage the user's watchlist of Nigerian funds, saved on this machine. action add (optionally with "
        "the amount they hold, so reports can show their income), update (change amount or note), remove, "
        "list or clear. Add a fund when the user says they invest in it or want to keep an eye on it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["add", "update", "remove", "list", "clear"]},
            "fund": _FUND,
            "category": _CATEGORY,
            "amount": {"type": "number", "description": "How much the user holds in this fund, in its currency."},
            "note": {"type": "string", "description": "A short note to keep with the fund."},
        },
        "required": ["action"],
    },
}

WATCH_REPORT = {
    "name": "afriradar_watch_report",
    "description": (
        "What changed for every watched fund since the last report: new SEC week or not, yield change in "
        "basis points with an alert when it moves past the user's threshold, rank movement, fund size "
        "change, the user's income at the new yield, and higher-yielding peers when a yield drops. "
        "By default it records this report as seen, so the next one only shows what is new; pass "
        "mark_seen false to peek."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "mark_seen": {"type": "boolean", "description": "Record this report as seen (default true)."},
        },
    },
}

ALL = [MARKET_OVERVIEW, FIND_FUNDS, TOP_FUNDS, FUND_DETAIL, COMPARE_FUNDS, PROJECT_RETURNS, WATCHLIST, WATCH_REPORT]
