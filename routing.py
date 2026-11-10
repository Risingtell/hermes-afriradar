"""pre_llm_call hook: point the model at the AfriRadar tools when a turn is about Nigerian or
Kenyan funds.

Without it, a model that also has web search will often search instead, find a stale week on
some aggregator, and do the compounding in its head. This adds two sentences to that turn only;
it never touches other turns, makes no network call and keeps no state.
"""

from __future__ import annotations

import re

_FUND_WORDS = re.compile(
    r"\b(money[\s-]?market|mmf|mutual\s+funds?|unit\s+trusts?|eurobond|dollar\s+funds?|"
    r"fixed[\s-]income\s+funds?|bond\s+funds?|equity\s+funds?|balanced\s+funds?|halal\s+funds?|"
    r"shariah|reits?|collective\s+investment|fund\s+managers?|afriradar|watchlist|"
    r"(?:fund|funds)\b.*\b(?:yield|interest|returns?|nav)|(?:yield|interest|returns?)\b.*\bfunds?)",
    re.IGNORECASE,
)
_PLACE_WORDS = re.compile(r"\b(nigeria\w*|naija|lagos|abuja|kano|sec|kenya\w*|nairobi|cma|naira|kes|ksh)\b|₦|\bN\d",
                          re.IGNORECASE)
_FUND_NAME = re.compile(r"\b(stanbic|arm|coronation|cowry|meristem|cordros|chapel\s+hill|fbn|united\s+capital|"
                        r"aiico|afrinvest|cardinalstone|first\s+ally|zedcrest|greenwich|lotus|fsdh|"
                        r"sanlam|britam|cic|ncba|old\s+mutual)\b", re.IGNORECASE)

NOTE = ("[AfriRadar plugin] For Nigerian or Kenyan fund figures, call the afriradar_* tools: they read "
        "SEC Nigeria's latest weekly report and CMA Kenya's bulletin, while web pages often show an older "
        "week. Use afriradar_project_returns for any future value instead of working it out, and say "
        "which week the figures are from.")


def is_fund_question(message: str) -> bool:
    if not message:
        return False
    text = message[:2000]
    if not _FUND_WORDS.search(text):
        return False
    return bool(_PLACE_WORDS.search(text) or _FUND_NAME.search(text)
                or re.search(r"money[\s-]?market|mmf|unit\s+trust|eurobond|afriradar|watchlist", text, re.I))


def route(user_message: str = "", **_) -> dict | None:
    try:
        return {"context": NOTE} if is_fund_question(user_message) else None
    except Exception:
        return None  # a routing hint must never stop a turn
