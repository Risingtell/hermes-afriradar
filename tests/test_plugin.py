"""Tests against real AfriRadar CSVs (week ended 23 Oct 2026) saved as fixtures."""

from __future__ import annotations

import json

import pytest

from afriradar import commands, data, tools
from conftest import FIXTURES, FakeCtx, fixture_fetcher


def call(fn, rt, **args):
    return json.loads(fn(rt, args))


# -- data layer -------------------------------------------------------------------------------

def test_parse_counts_every_fund(runtime):
    funds = runtime.source.all_latest()
    assert len(funds) == 218
    assert {f.week for f in funds} == {"2026-10-23"}
    assert len({f.key for f in funds}) == 218  # keys are unique


def test_slug_matches_site_rule():
    assert data.slugify("Stanbic IBTC Money Market Fund") == "stanbic-ibtc-money-market-fund"
    assert data.slugify("FBN Halal & Ethical Fund") == "fbn-halal-and-ethical-fund"


def test_duplicate_names_get_suffix():
    text = ("week_ended,category,fund,yield_ytd\n2026-10-23,bond,Same Fund,0.1\n"
            "2026-10-23,bond,Same Fund,0.2\n2026-10-30,bond,Same Fund,0.3\n")
    keys = [(f.week, f.slug) for f in data.parse_funds(text)]
    assert keys == [("2026-10-23", "same-fund"), ("2026-10-23", "same-fund-2"), ("2026-10-30", "same-fund")]


def test_zero_yield_is_not_reported():
    fund = data.parse_funds("week_ended,category,fund,yield_ytd\n2026-10-23,money-market,X,0\n")[0]
    assert fund.reported_yield is None


@pytest.mark.parametrize("raw,expected", [("MMF", "money-market"), ("money market funds", "money-market"),
                                          ("eurobond", "dollar"), ("Islamic", "shariah"), ("reits", "reit"),
                                          ("all", None), (None, None)])
def test_category_aliases(raw, expected):
    assert data.normalize_category(raw) == expected


def test_unknown_category_raises():
    with pytest.raises(ValueError):
        data.normalize_category("crypto")


def test_cache_reuses_file_then_revalidates(tmp_path):
    calls, now = [], [1000.0]
    source = data.DataSource("https://x.test/", tmp_path, cache_hours=1, fetcher=fixture_fetcher(calls),
                             clock=lambda: now[0])
    source.latest("money-market")
    source.latest("money-market")
    assert len(calls) == 1
    now[0] += 3601
    source._memory.clear()  # a restart: the copy on disk must be used and revalidated
    source.latest("money-market")
    assert len(calls) == 2 and calls[1][1].get("If-None-Match") == '"money-market-funds.csv"'


def test_offline_serves_saved_copy_and_says_so(tmp_path):
    online = data.DataSource("https://x.test/", tmp_path, cache_hours=1, fetcher=fixture_fetcher())
    online.latest("money-market")

    def down(url, headers):
        raise OSError("network unreachable")

    offline = data.DataSource("https://x.test/", tmp_path, cache_hours=1, fetcher=down, clock=lambda: 9e12)
    rt = tools.Runtime(offline, tools.Store(lambda k, d=None: d, lambda k, v: None))
    out = call(tools.top_funds, rt, category="money-market")
    assert out["funds"] and "data_status" in out


def test_html_error_page_never_replaces_data(tmp_path):
    def portal(url, headers):
        return 200, {}, b"<!doctype html><title>Login to Wi-Fi</title>"

    source = data.DataSource("https://x.test/", tmp_path, fetcher=portal)
    rt = tools.Runtime(source, tools.Store(lambda k, d=None: d, lambda k, v: None))
    out = call(tools.top_funds, rt)
    assert "error" in out


def test_rejects_plain_http():
    with pytest.raises(ValueError):
        data.DataSource("http://evil.test/", None)


# -- tools ------------------------------------------------------------------------------------

def test_market_overview_nigeria(runtime):
    out = call(tools.market_overview, runtime)
    assert out["week_ended"] == "2026-10-23" and out["funds"] == 218
    assert 10e12 < out["total_nav_ngn"] < 10.2e12  # N10.10trn that week
    mmf = next(c for c in out["categories"] if c["category"] == "money-market")
    assert mmf["funds"] == 50 and mmf["highest_yield"]["yield_pct"] > mmf["median_yield_pct"]
    assert "disclaimer" in out


def test_market_overview_kenya(runtime):
    out = call(tools.market_overview, runtime, country="kenya")
    assert out["schemes"] == 47 and out["quarter_ended"] == "2026-06-30"
    assert 940e9 < out["total_aum_kes"] < 960e9


def test_top_money_market_is_sorted(runtime):
    out = call(tools.top_funds, runtime, category="MMF", limit=10)
    yields = [f["yield_pct"] for f in out["funds"]]
    assert yields == sorted(yields, reverse=True) and len(yields) == 10
    assert out["funds"][0]["rank"] == 1 and out["funds"][0]["url"].startswith("https://www.afriradar.com/funds/")


def test_top_min_size_filters(runtime):
    out = call(tools.top_funds, runtime, category="money-market", min_size_ngn="20bn")
    assert all(f["nav_ngn"] >= 20e9 for f in out["funds"])


def test_top_by_size_dollar_uses_usd(runtime):
    out = call(tools.top_funds, runtime, category="dollar", by="size", limit=3)
    sizes = [f["nav_usd"] for f in out["funds"]]
    assert sizes == sorted(sizes, reverse=True)


def test_find_by_manager(runtime):
    out = call(tools.find_funds, runtime, query="Stanbic")
    assert out["total"] >= 3 and all("Stanbic" in m["fund"] or "Stanbic" in m["manager"] for m in out["matches"])


def test_find_typo_falls_back_to_fuzzy(runtime):
    out = call(tools.find_funds, runtime, query="aiico mony market")
    assert any(m["id"] == "money-market/aiico-money-market-fund" for m in out["matches"])


def test_fund_detail_by_loose_name(runtime):
    out = call(tools.fund_detail, runtime, fund="AIICO money market")
    assert out["id"] == "money-market/aiico-money-market-fund"
    assert out["yield_pct"] == 16.59
    assert out["rank"]["of"] >= 40 and len(out["history"]) == 13
    assert out["history"][-1]["week_ended"] == "2026-10-23"
    assert out["source"].startswith("https://sec.gov.ng/")


def test_brand_outweighs_common_words(runtime):
    # There is no Cowry money market fund: offer Cowry's funds, not every "money market" fund.
    out = call(tools.fund_detail, runtime, fund="Cowry money market")
    assert "No fund" in out["error"]
    assert out["candidates"] and all("Cowry" in c["fund"] for c in out["candidates"])


def test_mmf_shorthand_resolves(runtime):
    out = call(tools.fund_detail, runtime, fund="chapel hill denham mmf")
    assert out["id"] == "money-market/chapel-hill-denham-money-market-fund"


def test_typo_in_brand_still_resolves(runtime):
    out = call(tools.fund_detail, runtime, fund="Coronaton money market")
    assert out["id"] == "money-market/coronation-money-market-fund"


def test_ambiguous_name_returns_candidates(runtime):
    out = call(tools.fund_detail, runtime, fund="Stanbic")
    assert "error" in out and len(out["candidates"]) >= 2


def test_compare_two_funds(runtime):
    out = call(tools.compare_funds, runtime, funds="AIICO money market vs Afrinvest Plutus")
    assert [f["id"] for f in out["funds"]] == ["money-market/aiico-money-market-fund",
                                               "money-market/afrinvest-plutus-fund"]
    assert out.get("note") is None
    assert all(f["yield_13w_low_pct"] <= f["yield_13w_high_pct"] for f in out["funds"])


def test_compare_warns_across_categories(runtime):
    out = call(tools.compare_funds, runtime, funds=["AIICO money market fund", "Afrinvest Equity Fund"])
    assert "different categories" in out["note"]


def test_project_returns_compounds(runtime):
    out = call(tools.project_returns, runtime, amount="N1m", yield_pct=12, months=12)
    assert out["end_balance"] == pytest.approx(1_120_000, rel=1e-6)


def test_project_returns_with_contributions_from_fund(runtime):
    out = call(tools.project_returns, runtime, amount=0, monthly_contribution="50k", months=24,
               fund="AIICO money market")
    assert out["total_paid_in"] == 1_200_000 and out["end_balance"] > 1_200_000
    assert out["assumed_yield_pct"] == 16.59 and len(out["schedule"]) == 2


def test_project_returns_rejects_nonsense(runtime):
    assert "error" in call(tools.project_returns, runtime, amount="lots", yield_pct=10)
    assert "error" in call(tools.project_returns, runtime, amount=1000)


@pytest.mark.parametrize("raw,expected", [("500k", 5e5), ("N2.5m", 2.5e6), ("NGN 1,000,000", 1e6),
                                          ("₦3bn", 3e9), ("250000", 2.5e5)])
def test_amount_parsing(raw, expected):
    assert tools._float(raw, "amount") == expected


# -- watchlist and weekly report ---------------------------------------------------------------

def test_watchlist_add_list_remove(runtime):
    added = call(tools.watchlist, runtime, action="add", fund="AIICO money market", amount="2m")
    assert added["added"]["amount"] == 2_000_000 and added["watching"] == 1
    again = call(tools.watchlist, runtime, action="add", fund="money-market/aiico-money-market-fund", amount=3e6)
    assert again["updated"]["amount"] == 3_000_000
    assert call(tools.watchlist, runtime, action="list")["count"] == 1
    removed = call(tools.watchlist, runtime, action="remove", fund="AIICO Money Market Fund")
    assert removed["watching"] == 0


def test_report_with_no_new_week_is_quiet(runtime):
    call(tools.watchlist, runtime, action="add", fund="AIICO money market", amount=2_000_000)
    out = call(tools.watch_report, runtime)
    assert out["new_report"] is False and out["alerts"] == 0
    fund = out["funds"][0]
    assert fund["income_at_this_yield"]["per_year"] == pytest.approx(2_000_000 * 0.1659)


def test_report_flags_a_yield_drop_and_lists_peers(runtime):
    call(tools.watchlist, runtime, action="add", fund="AIICO money market")
    items = runtime.store.get("watchlist")
    items[0]["last_seen"]["week"] = "2026-10-16"
    items[0]["last_seen"]["yield"] = 0.1759  # pretend it paid 1.00pp more last week
    runtime.store.set("watchlist", items)
    out = call(tools.watch_report, runtime, mark_seen=True)  # what the weekly automation does
    row = out["funds"][0]
    assert out["new_report"] is True and row["alert"] is True and row["yield_change_bps"] == -100
    peers = row["higher_yielding_peers"]
    assert peers and all(p["yield_pct"] > 16.59 and p["nav_ngn"] >= 1e9 for p in peers)
    # marked seen: the next report is quiet
    assert call(tools.watch_report, runtime)["alerts"] == 0


def test_peek_does_not_mark_seen(runtime):
    call(tools.watchlist, runtime, action="add", fund="AIICO money market")
    items = runtime.store.get("watchlist")
    items[0]["last_seen"]["week"] = "2026-10-16"
    runtime.store.set("watchlist", items)
    assert call(tools.watch_report, runtime, mark_seen=False)["new_report"] is True
    assert call(tools.watch_report, runtime, mark_seen=False)["new_report"] is True


def test_missing_fund_is_an_alert(runtime):
    runtime.store.set("watchlist", [{"id": "money-market/gone-fund", "name": "Gone Fund",
                                     "category": "money-market", "last_seen": {"week": "2026-10-16"}}])
    out = call(tools.watch_report, runtime)
    assert out["alerts"] == 1 and out["funds"][0]["status"] == "missing"


def test_empty_watchlist_report(runtime):
    assert call(tools.watch_report, runtime)["watching"] == 0


# -- slash command and registration ------------------------------------------------------------

def test_slash_command_default_and_help(runtime):
    text = commands.handle(runtime, "")
    assert text.startswith("Money market funds over N1bn, week ended 2026-10-23") and text.count("\n") == 5
    assert "RT Briscoe" not in text  # a N738m fund is left out of the quick view
    assert "/afriradar watch" in commands.handle(runtime, "help")


def test_slash_command_fund_and_market(runtime):
    assert "AIICO Money Market Fund" in commands.handle(runtime, "AIICO money market")
    assert "218 funds" in commands.handle(runtime, "market")
    assert "Kenya unit trust" in commands.handle(runtime, "kenya")
    assert "Dollar funds" in commands.handle(runtime, "top eurobond")


def test_slash_command_watch(runtime):
    call(tools.watchlist, runtime, action="add", fund="AIICO money market", amount=2_000_000)
    text = commands.handle(runtime, "watch")
    assert "AIICO Money Market Fund" in text and "a month" in text


def test_register_wires_everything(monkeypatch, tmp_path):
    import afriradar
    monkeypatch.setattr(afriradar, "_cache_dir", lambda: tmp_path)
    ctx = FakeCtx()
    afriradar.register(ctx)
    import yaml
    manifest = yaml.safe_load((FIXTURES.parent.parent / "plugin.yaml").read_text(encoding="utf-8"))
    assert sorted(ctx.tools) == sorted(manifest["provides_tools"])
    assert set(ctx.commands) == {"afriradar"}
    assert set(ctx.skills) == {"african-fund-research"}
    assert set(ctx.blueprints) == {"weekly-fund-watch", "money-market-digest"}
    for entry in ctx.tools.values():
        assert entry["toolset"] == "afriradar" and entry["schema"]["parameters"]["type"] == "object"


def test_handler_through_registration_uses_state(monkeypatch, tmp_path):
    import afriradar
    monkeypatch.setattr(afriradar, "_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(data, "http_fetch", fixture_fetcher())
    ctx = FakeCtx()
    afriradar.register(ctx)
    handler = ctx.tools["afriradar_watchlist"]["handler"]
    out = json.loads(handler({"action": "add", "fund": "AIICO money market"}, task_id="t1"))
    assert out["watching"] == 1 and ctx.state.data["watchlist"][0]["id"] == "money-market/aiico-money-market-fund"


# -- review fixes -------------------------------------------------------------------------------

def test_chat_report_does_not_use_up_the_weekly_changes(runtime):
    call(tools.watchlist, runtime, action="add", fund="AIICO money market")
    items = runtime.store.get("watchlist")
    items[0]["last_seen"]["week"] = "2026-10-16"
    runtime.store.set("watchlist", items)
    assert call(tools.watch_report, runtime)["new_report"] is True          # a chat question
    assert call(tools.watch_report, runtime, mark_seen=True)["new_report"] is True  # the automation still sees it


def test_offline_backs_off_instead_of_retrying_every_call(tmp_path):
    now = [1000.0]
    data.DataSource("https://x.test/", tmp_path, cache_hours=1, fetcher=fixture_fetcher(),
                    clock=lambda: now[0]).latest("money-market")
    attempts = []

    def down(url, headers):
        attempts.append(url)
        raise OSError("offline")

    now[0] += 7200
    source = data.DataSource("https://x.test/", tmp_path, cache_hours=1, fetcher=down, clock=lambda: now[0])
    for _ in range(5):
        source.latest("money-market")
    assert len(attempts) == 1
    now[0] += data.RETRY_AFTER + 1
    source.latest("money-market")
    assert len(attempts) == 2


def test_fresh_install_offline_fails_fast_after_first_try(tmp_path):
    attempts = []

    def down(url, headers):
        attempts.append(url)
        raise OSError("offline")

    source = data.DataSource("https://x.test/", tmp_path, fetcher=down)
    for _ in range(3):
        with pytest.raises(data.DataError):
            source.latest("bond")
    assert len(attempts) == 1


def test_one_broken_category_does_not_break_lookups(tmp_path):
    good = fixture_fetcher()

    def flaky(url, headers):
        if url.endswith("/ethical-funds.csv"):
            return 404, {}, b"<html>not found</html>"
        return good(url, headers)

    source = data.DataSource("https://x.test/", tmp_path, fetcher=flaky)
    rt = tools.Runtime(source, tools.Store(lambda k, d=None: d, lambda k, v: None))
    out = call(tools.fund_detail, rt, fund="AIICO money market")
    assert out["id"] == "money-market/aiico-money-market-fund"
    assert out["unavailable_categories"] == ["ethical"]


def test_kenya_id_gets_a_clear_message(runtime):
    out = call(tools.fund_detail, runtime, fund="kenya/sanlam")
    assert "quarterly" in out["error"]


def test_slash_in_a_name_falls_back_to_name_search(runtime):
    out = call(tools.fund_detail, runtime, fund="ARM/Money Market")
    assert out["id"] == "money-market/arm-money-market-fund"


def test_top_needs_a_word_boundary(runtime):
    fake = data.parse_funds("week_ended,category,fund,yield_ytd,nav_ngn\n"
                            "2026-10-23,money-market,Topaz Money Market Fund,0.18,2000000000\n")
    runtime.source._memory.clear()
    runtime.source.latest("money-market")  # warm, then splice the fake fund in
    entry = runtime.source._memory["money-market-funds.csv"]
    entry.parsed["rows"] = entry.parsed["rows"] + fake
    assert commands.handle(runtime, "Topaz Money Market Fund").startswith("Topaz Money Market Fund")


def test_non_money_market_projection_uses_a_year_of_unit_prices(runtime):
    out = call(tools.project_returns, runtime, amount=1_000_000, months=12, fund="ARM Fixed Income Fund")
    assert "unit price" in out["fund"]["rate_basis"] and "2025-10" in out["fund"]["rate_basis"]
    # the rate is the annualised price move (8.5%), not SEC's year-to-date column (12.6%)
    assert 8 < out["assumed_yield_pct"] < 9 and out["fund"]["yield_pct"] > 12


def test_short_price_history_refuses_to_project(runtime):
    # Cowry Eurobond has dollar prices only from May 2026: too short to annualise honestly.
    out = call(tools.project_returns, runtime, amount=10_000, months=12, fund="Cowry Eurobond Fund")
    assert "six months" in out["error"] or "26 weeks" in out["error"]


def test_watch_report_values_a_non_money_market_holding_by_price(runtime):
    call(tools.watchlist, runtime, action="add", fund="Afrinvest Equity Fund", amount=1_000_000)
    items = runtime.store.get("watchlist")
    price = items[0]["last_seen"]["price"]
    assert price
    items[0]["last_seen"]["price"] = price / 1.05  # pretend the price rose 5% since the last report
    runtime.store.set("watchlist", items)
    row = call(tools.watch_report, runtime)["funds"][0]
    assert "income_at_this_yield" not in row
    assert row["unit_price_change_pct"] == pytest.approx(5.0, abs=0.01)
    assert row["your_value_change"]["amount"] == pytest.approx(50_000, rel=1e-3)


def test_kenya_growth_sort_treats_zero_as_zero(runtime):
    schemes = runtime.source.kenya()
    schemes[0].change, schemes[1].change, schemes[2].change = 0.0, -0.05, None
    order = [s["scheme"] for s in call(tools.top_funds, runtime, country="kenya", by="growth", limit=50)["funds"]]
    assert order.index(schemes[0].name) < order.index(schemes[1].name) < order.index(schemes[2].name)


def test_bad_data_url_falls_back_instead_of_breaking_the_plugin(monkeypatch, tmp_path):
    import afriradar
    monkeypatch.setattr(afriradar, "_cache_dir", lambda: tmp_path)
    ctx = FakeCtx({"data_url": "http://localhost.evil.com/data/"})
    afriradar.register(ctx)
    assert len(ctx.tools) == 8


@pytest.mark.parametrize("url,ok", [("https://www.afriradar.com/data/", True), ("http://localhost:8000/", True),
                                    ("http://localhost.evil.com/", False), ("http://afriradar.com/", False),
                                    ("ftp://x/", False)])
def test_data_url_check(url, ok):
    assert data.valid_data_url(url) is ok


def test_kenya_slash_survives_blank_assets(runtime):
    runtime.source.kenya()[0].aum_kes = None
    assert "assets not reported" in commands.handle(runtime, "kenya") or "Kenya" in commands.handle(runtime, "kenya")


def test_watch_income_matches_project_returns(runtime):
    call(tools.watchlist, runtime, action="add", fund="AIICO money market", amount=2_000_000)
    monthly = call(tools.watch_report, runtime)["funds"][0]["income_at_this_yield"]["per_month"]
    one_month = call(tools.project_returns, runtime, amount=2_000_000, months=1, fund="AIICO money market")
    assert monthly == pytest.approx(one_month["estimated_earnings"], abs=0.01)


@pytest.mark.parametrize("message,routed", [
    ("If I save N50k a month in Chapel Hill Denham MMF for 3 years, what do I end up with?", True),
    ("What are the best money market funds in Nigeria?", True),
    ("Compare Stanbic IBTC, ARM and Coronation money market funds", True),
    ("How big are Kenya's unit trusts?", True),
    ("Is the Cowry eurobond fund doing well?", True),
    ("Write me a Python function to sort a list", False),
    ("What's the weather in Lagos today?", False),
    ("Explain how index funds work in the US", False),
])
def test_routing_hook(message, routed):
    from afriradar import routing
    assert (routing.route(user_message=message) is not None) is routed


def test_hook_is_registered(monkeypatch, tmp_path):
    import afriradar
    monkeypatch.setattr(afriradar, "_cache_dir", lambda: tmp_path)
    ctx = FakeCtx()
    afriradar.register(ctx)
    assert set(ctx.hooks) == {"pre_llm_call"}
