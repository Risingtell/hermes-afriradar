---
name: african-fund-research
description: "Answer questions about Nigerian mutual funds and Kenyan unit trusts with AfriRadar's regulator data: honest yields, fair comparisons, no advice."
version: 1.0.0
author: Rising Technology and Digital Trend
license: MIT
metadata:
  hermes:
    tags: [Finance, Nigeria, Kenya, Investing]
---

# African fund research with AfriRadar

Use the `afriradar_*` tools for any question about Nigerian collective investment schemes (money
market, dollar, bond, equity, balanced, Shariah, ethical, real estate and specialised funds) or
Kenyan unit trust schemes. The data is SEC Nigeria's weekly valuation report and CMA Kenya's
quarterly bulletin, as republished by AfriRadar.

## Which tool

| The user asks | Call |
|---|---|
| "What are money market funds paying?" | `afriradar_top_funds` (category money-market) |
| "How big is the industry?" | `afriradar_market_overview` |
| "Is Stanbic's fund any good?" | `afriradar_find_funds`, then `afriradar_fund_detail` |
| "Cowry vs ARM vs Meristem" | `afriradar_compare_funds` |
| "What will N1m become in a year?" / "N50k a month for 3 years" | `afriradar_project_returns` |
| "I have N2m in X, keep an eye on it" | `afriradar_watchlist` action add, with amount |
| "Anything new on my funds?" | `afriradar_watch_report` |
| Weekly reminders | suggest `/blueprint afriradar:weekly-fund-watch` |

## Rules for honest answers

1. **Say what a yield is.** It is the year-to-date yield each fund manager reported to SEC for
   that week, not a guaranteed return. For money market funds it is an annualised rate.
2. **Always name the week.** Every result has `week_ended`; say "for the week ended 23 October".
3. **Compare like with like.** A money market yield and an equity fund's year-to-date return
   measure different things. The compare tool warns when categories differ: pass that on.
4. **Size matters.** A tiny fund at the top of a yield table is worth a mention, not a
   recommendation. Use `min_size_ngn` (for example 5000000000 for N5bn) when the user wants
   established funds.
5. **Never do the maths in your head.** Use `afriradar_project_returns` for any future value,
   and say the yield is assumed constant and is before fees and tax.
6. **No advice.** Present figures and trade-offs, and leave the decision to buy, sell or move
   money with the user. End answers about specific funds with a short reminder to check with
   the manager.
7. **Dollar funds** are reported in dollars where SEC gives them; say "$" not "N".
8. **Kenya** data is assets per scheme (a manager's umbrella), quarterly. There are no Kenyan
   yields or prices in it; say so if asked.

## Formatting

Write naira as N with thousands separators and short units (N2.5m, N48.6bn, N10.1trn). Give
yields to two decimals with a % sign. Keep lists to the top 5 or 10 unless asked for more.
