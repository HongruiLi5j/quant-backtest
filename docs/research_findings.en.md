# Research Findings: Do These Strategies Actually Work? (Honest Version)

[中文版](research_findings.md)

> This page summarizes 14 research tasks (C01–C14) from one overnight research queue in October 2026. **Every number here is copied from the text of the corresponding research report. Nothing was recomputed for this page.** The original reports and large output tables are not in the repository. The scripts that reproduce them are in `scripts/` and are listed at the end of each section. No backtest or study uses data later than 2026-03-31. The one exception is a turnover side table used by C07 and C12, and only as a static size label (see those sections).
>
> Currency note: amounts in CNY were converted from Chinese units only (1 亿 = 100 million, 1 万 = 10,000).

## Overall conclusion

In one sentence: **so far there is no evidence that any strategy in this project has real timing or stock-selection skill. The available data is also not enough to prove that they do not work.**

1. **None survives multiple-testing correction.** The 7 production strategies on 12 ETFs, in-sample and out-of-sample, give 168 cells. After correcting for m = 515 trials, no cell reaches a Deflated Sharpe Ratio (DSR) ≥ 0.95. The best cell scores 0.386. Without correction, 20 of the 84 out-of-sample cells "look significant". (C04)
2. **None beats buy-and-hold.** Every strategy's annual excess return over buy-and-hold is negative in-sample and out-of-sample (C08). At zero trading cost, excess over buy-and-hold is still almost always negative. The only exception is trend_vol on 510300 (+0.26%, with a break-even cost of only 7.4bp) (C11). What these strategies deliver is smaller drawdowns, not outperformance.
3. **The A-share cross-sectional low-frequency rebalancing replay (T008) had 0 survivors out of 485 (strategy, parameter) combinations.** Rule 4 passed 6.2% of candidates, which is the same order of magnitude as the 5.2% pass rate of slowly varying pure-noise factors. It is not evidence of skill (C01). The two "robustness variants" reused the frozen verdicts of the first run, so they are not independent confirmations (C03). Under the pre-registered v2 plan, m = 646 requires performance roughly equivalent to an annualized information ratio of 3.8. The most likely outcome is "no candidate confirmed" (C13).
4. **Parameters and diversification.** The trend ensemble (trend_ens) sits on a parameter plateau in all 5 test universes. The original and improved MA+ADX strategies have no plateau at all, and their median tuning gain is 0.56 Sharpe. That is the typical shape of "you can tune your way to a good Sharpe" (C14). The 3 components of blend2 are worth about 2 independent strategies, not 3 (C06).
5. **Real-world frictions.** Capacity is only meaningful as an order of magnitude. Under middle assumptions it is about CNY 130 million for blend2 and only about CNY 7.63 million for rsi2 (C07). rsi2 breaks even at only 13–24bp of cost (C11). Limit-up/limit-down "can't sell / can't buy" bars cost only 0.02–0.16 percentage points per year among surviving non-ST stocks. The worst cases (ST stocks, delisted stocks) are not in the sample (C12).
6. **The backtesting tools themselves.** No look-ahead bias was found (C05). The three engines implement identical rules, but they differ in 7 places at the implementation level (C09). The Python and AmiBroker AFL rules match, but the CSV exported to AmiBroker keeps only 4 decimals, which creates false mismatches (C10). The random control used by the promotion test is too narrow when strategies hold positions for consecutive days. On synthetic data this raises a nominal 5% false-positive rate to 16%–19% (C02).

Most of these are negative results, and they are reported as they are. The testing tools were calibrated first: pure-noise simulations in C01, C02 and C04, and correctness checks in C05, C09 and C10. Then those tools were used to reach the conclusion "insufficient evidence". That process is what this project is meant to demonstrate.

## Five things to know before reading any number

1. **Post-2020 data has been looked at many times, so it is not a clean out-of-sample period.** Since 2026-09 the website's "credibility" panel has shown 2013–2019 and 2020+ Sharpe ratios every day, and the market-wide scan computes post-2020 excess Sharpe every day. The strategy list itself was finalized after seeing results that included post-2020 data. Examples are using the Turtle System 1 parameters for donchian_s1 and choosing which three components go into blend2 (C04). So "out-of-sample" on this page only means "later in time". The 2013–2019 period is not fully clean either, because the rules were chosen with knowledge of that history (C08). A truly clean test can only use forward data generated after pre-registration (C04, C13).
2. **Multiple testing.** The headline trial count is m = 515 (7 production + 18 shadow strategies + 490 T008 trials). This is a convention, not an exact count. The trials are highly correlated, so the effective number of independent trials may be much smaller. Many trials were also never recorded: parameter grids, the website's parameter optimizer, and this research batch looking again across many instruments and periods. So the true m is probably larger (C04). Each study in this batch also covers several universes × strategies × metrics, so the tables are descriptive only.
3. **Small samples.** Each ETF period has only about 6–7 years of daily data. The sampling standard deviation of an annualized Sharpe is about 0.38–0.44, which is the same order of magnitude as the Sharpe differences between strategies (C04). The year-by-year tests have only 6–7 observations per group (C08).
4. **Survivorship bias.** The A-share stock samples contain only currently listed stocks, with no delisted names, and most studies also exclude ST stocks (C06, C07, C11, C12, C14).
5. **Simplified transaction costs.** The default cost is symmetric per side (5bp for ETFs, 8bp for stocks). It has no market impact, and the real fee schedule has not been checked against a brokerage account (C11). Market impact (C07) and price-limit locks (C12) are quantified separately.

## At a glance

| ID | Question | One-line answer |
|---|---|---|
| C04 | Do the 7 strategies' Sharpe ratios survive multiple testing? | 0 of 168 cells reach DSR ≥ 0.95 |
| C08 | Is post-2020 performance significantly worse than 2013–2019? | No robust decay in 84 tests, but power is too low to tell |
| C14 | Are default parameters on a plateau or a spike? | trend_ens: all plateaus; both MA+ADX versions: 0 plateaus |
| C06 | Is blend2's diversification real? | 3 components ≈ 2 independent strategies |
| C01 | Are the T008 replay elimination rules calibrated? | Rule 1 is calibrated; Rule 4's 6.2% is at noise level |
| C03 | Do three variants "independently confirm" the result? | No: the variants reused frozen verdicts |
| C02 | Is the promotion test's random control wide enough? | About half as wide as it should be under persistent holdings; 16%–19% false positives |
| C13 | What would a valid v2 replay require? | Pre-registered plan: m = 646, very unlikely to pass |
| C11 | At what cost do the strategies stop making money? | rsi2: only 13–24bp; trend strategies are robust; but already negative vs buy-and-hold |
| C07 | How much capital can the strategies hold? | Order of magnitude only: blend2 ≈ CNY 130M, rsi2 ≈ CNY 7.63M |
| C12 | How much do limit-locked bars hurt? | 0.02–0.16 pp/year among surviving non-ST stocks, which is a lower bound |
| C05 | Is there look-ahead bias? | 0 violations across 28 signals |
| C09 | Do the three engines implement the same rules? | Same rules, 7 implementation differences |
| C10 | Does Python match the AmiBroker AFL? | 20/20 items match; 4-decimal export creates false mismatches |

Strategy codes: trend_ens = trend ensemble; trend_vol = single moving average + volatility targeting; donchian_s1 = Turtle (Donchian System 1); rsi2 = RSI(2) mean reversion; ma_adx_original / ma_adx_plus = MA+ADX original / improved; blend2 = 1/3 each of trend_ens, rsi2 and donchian_s1.

---

## 1. How strong is the evidence for the strategies?

### C04 Deflated Sharpe Ratio

- **Question**: Given how many trials the project has actually run, can the 7 production strategies' Sharpe ratios be told apart from "the best of many pure-noise strategies"?
- **Method**: The project's own backtest functions were reused (signal at close, fill at next open, fees deducted) to compute Sharpe on returns in excess of a cash ETF. In-sample is 2013-01-01 to 2019-12-31 and out-of-sample is 2020-01-01 to 2026-03-31, on 12 ETFs. DSR follows Bailey & López de Prado (2014) with a headline m = 515. The Sharpe variance V is taken two ways: "pure noise" (null) and "actual dispersion across the 7 strategies" (cross). A cell passes only if both are ≥ 0.95. The formula was also calibrated with pure-noise simulations.
- **Key numbers**:
  - 0 of 168 cells pass. The best cell is trend_ens out-of-sample on 518880 (a gold ETF), with DSR 0.386.
  - Uncorrected (PSR(0) ≥ 0.95): 10 of 84 in-sample cells and 20 of 84 out-of-sample cells "look significant". All of them disappear after correction.
  - Break-even trial count m\* (the most trials a given Sharpe can withstand): the highest in the whole table is 4 under null variance and 11 under cross variance (ma_adx_original out-of-sample on QQQ).
  - Example: blend2 on SPY in-sample has Sharpe 0.90, and its DSR is 0.990 at m = 1, 0.833 at m = 7 and 0.255 at m = 515.
  - On SPY in-sample, buy-and-hold's Sharpe of 1.03 is higher than all 7 strategies.
  - Pure-noise calibration: with 515 trials, the best noise strategy has an average annualized Sharpe of 1.175, and the formula's threshold SR0 is 1.162.
- **Conclusion**: Nothing passes whether m is in the hundreds, thousands or tens of thousands. Cells only pass when m ≤ 4 (null) or m ≤ 11 (cross). This means the data cannot provide strong enough evidence. **It does not prove the strategies are useless.** Calibration shows that the DSR ≥ 0.95 threshold has a 0–2% false-positive rate on pure noise, which is stricter than the nominal 5%.
- **Limitations**: The out-of-sample period has been viewed repeatedly. m is a convention that could be wrong in either direction. PSR assumes i.i.d. daily returns and ignores autocorrelation. Only ETFs were tested, no individual stocks. The table itself is one more look across 12 instruments × 2 periods.
- Script: `scripts/deflated_sharpe_table.py`

### C08 Year-by-year out-of-sample decay

- **Question**: Is post-2020 performance significantly worse than 2013–2019?
- **Method**: Four universes were used: SPY, 510300, an equal-weight US ETF basket and an equal-weight A-share ETF basket. For each calendar year from 2013 to 2025, the study computed annual return, Sharpe, max drawdown and excess over the benchmark. Yearly means of the two periods were compared with Welch's t-test, supplemented by an exact permutation test, Holm correction (across the 7 strategies within each universe and metric) and a power analysis.
- **Key numbers**:
  - There were 4 universes × 7 strategies × 3 metrics = 84 tests. Only 1 has a one-sided p < 0.05 (annual return of trend_vol on 510300, p = 0.046), and it becomes 0.324 after Holm. Luck alone would produce about 4 such results in 84 tests.
  - Power: with 7 in-sample years vs 6 out-of-sample years, a true difference of about 1.71 standard deviations is needed for 80% power. A true difference of 1 standard deviation gives only 0.375 power. Reaching 80% power would take 17 years per group.
  - Sharpe: blend2 on SPY went from 1.04 to 0.63, but buy-and-hold itself fell from 1.30 to 0.96. On 510300, trend_ens went from 0.87 to −0.46 and trend_vol from 0.98 to −0.12 (one-sided p ≈ 0.06, 0.40 after Holm). On SPY, ma_adx_original, rsi2 and ma_adx_plus actually did better out-of-sample.
  - Annual excess over buy-and-hold is negative in both periods. The 7-strategy averages are −9.6% / −8.8% on SPY and −4.6% / −2.5% on 510300. On SPY, the 7 strategies' average yearly max drawdown is −6.1%, against −12.6% for buy-and-hold.
- **Conclusion**: No statistically robust decay was found. But "not significant" here mainly means "cannot tell", not "confirmed no decay". Most of the Sharpe decline comes from the market environment, since buy-and-hold declined too. The strategies' value is drawdown reduction, not excess return.
- **Limitations**: The out-of-sample period is not clean. Years are not independent, and the 7 strategies are highly correlated, so they are not 7 independent pieces of evidence. Holm correction is only applied within each group, not across groups or metrics. A Sharpe for a year spent entirely in cash is recorded as 0, which affects 4 strategy-years on 510300. 510300 has only 6 complete in-sample years.
- Script: `scripts/oos_decay.py`

### C14 Parameter stability (plateau or spike)

- **Question**: When default parameters move ±50%, does Sharpe form a flat plateau or an isolated spike? A spike is a classic overfitting signal.
- **Method**: Only data up to 2019-12-31 was used, so post-2020 data never enters the computation. For each strategy, 2 main parameters were set to ×0.5 / ×0.75 / ×1 / ×1.25 / ×1.5 of their defaults, giving a 5×5 grid of 25 cells. There were 5 universes (SPY, 510300, equal-weight US ETFs, equal-weight A-share ETFs, and an equal-weight basket of 100 A-share stocks), so 35 grids in total. This is a diagnostic only. The results were not used to change any defaults.
- **Key numbers**:
  - 35 grids: 12 plateaus, 9 spikes, 7 transitional, 7 slopes.
  - trend_ens: plateaus in all 5 universes. The best cell is only 1%–5% above the average of its neighbors, and the tuning gain (best − default) is only 0.02–0.14 Sharpe.
  - Original and improved MA+ADX: 0 plateaus, with 6 spikes, 3 slopes and 1 transitional between them. The median default-cell Sharpe is only 0.04 / 0.07, and the median tuning gain is 0.56 for both. A typical case is ma_adx_original on SPY: the default cell is 0.26, the fastest-parameter corner is 1.05, and the cell next to it is 0.59.
  - rsi2: 0 plateaus.
  - In 32 of 35 grids, the best cell is on the edge of the grid. The default cell's Sharpe matches or beats same-window buy-and-hold in only 10 of 35 grids, and in none on SPY or the US ETF basket.
- **Conclusion**: The trend ensemble is insensitive to its parameters, partly by design, since it already averages 4 moving averages. MA+ADX's defaults are neither good nor stable. **A plateau does not mean excess return.** trend_ens on SPY is a plateau from 0.67 to 0.84, and the whole plateau is below buy-and-hold's 1.03.
- **Limitations**: The plateau/spike thresholds are descriptive labels. The "slope" category was added **after seeing the data**; the first version had three classes: 12 plateaus, 10 spikes, 13 transitional. So this is not a pre-registered test. The in-sample period is only about 7 years, and US equities rose almost the whole time. Taking the best of 875 backtests creates a winner's curse. trend_ens's real core parameters (the lengths of its 4 moving averages) were not scanned. The stock sample has survivorship bias.
- Script: `scripts/param_stability.py`

### C06 Is blend2's diversification real?

- **Question**: blend2 holds 1/3 each of trend ensemble, RSI2 and Turtle. Is that three independent bets, or just a comforting story?
- **Method**: The window is 2020-01-01 to 2026-03-31, covering 5 A-share ETFs, 7 US ETFs and 1,000 A-share stocks (fixed-seed sample). The study measured holdings overlap (Jaccard) and daily-return correlation matrices (per instrument, at portfolio level, and as residuals after removing the market). It computed the "effective number of independent strategies" from the eigenvalues of the correlation matrix, with intervals from a block bootstrap using 20-day blocks.
- **Key numbers**:
  - Daily-return correlations: trend ensemble–Turtle 0.61–0.68, trend ensemble–RSI2 0.31–0.39, and RSI2–Turtle only 0.03–0.04.
  - Effective number of independent components (participation ratio): 2.0–2.3 per instrument and 1.7–2.1 at portfolio level. Fully independent would be 3.
  - blend2's volatility is 0.80 / 0.81 / 0.85 of its components' average volatility. Fully independent would be 0.58 and perfectly synchronized would be 1.00.
  - All three components hold a position at the same time in only 1.5% / 1.1% / 1.1% of cells.
  - The trend ensemble and trend_vol correlate at 0.99 / 0.98 / 0.96, so they are essentially two versions of one strategy.
  - From 2025 onward, the average pairwise correlation of the 3 components on ETFs rose to about 0.48, and the effective number fell below 1.9.
- **Conclusion**: blend2 is about 2 independent bets. The RSI2–Turtle complementarity is real, but the trend ensemble and Turtle overlap heavily. This **does not** show that blend2 has better risk-adjusted returns. No return inference was made here.
- **Limitations**: The window is only about 6.25 years and one market cycle, with 2022 the only bear year, and correlations change with market regime. Only linear correlation was measured, not tail dependence. The stock sample has survivorship bias. Only default parameters were used.
- Script: `scripts/strategy_overlap.py`

---

## 2. The A-share cross-sectional replay (T008) and the promotion test

T008 is a cross-sectional "low-frequency rebalancing" replay on A-share stocks. 485 (strategy, parameter) combinations went through four elimination rules in sequence: Rule 1, not below the 5th percentile of matched random portfolios; Rule 2, turnover; Rule 3, holding ratio; Rule 4, monotonicity. The result was 0 survivors. Once an elimination verdict is written to the ledger it is frozen, so it cannot be re-run to "get" a better result.

### C01 Power calibration of replay Rules 1 and 4

- **Question**: Do Rules 1 and 4 have the designed false-positive rate on pure noise? Does Rule 4's 6.2% pass rate in the real replay (30/485) indicate stock-selection skill?
- **Method**: A synthetic market of 500 stocks and 363 trading days was built, with no real market data read. Factors with known strength (IC) were planted in it and judged by the project's real decision code, which was not modified or rewritten and wrote no ledger. Pure noise was repeated 500 times and every other setting 200 times. Factor noise was either "independent each day" (iid) or "slowly varying" (daily autocorrelation 0.98, ar98).
- **Key numbers**:
  - Rule 1: 5.6% of pure-noise factors were eliminated (28/500, 95% interval 3.9%–8.0%), in line with the designed 5%. 97.5% of bad factors with IC = −0.02 were eliminated. Good factors with IC ≥ +0.02 were wrongly eliminated 0 times out of 200.
  - Rule 4 pass rate on pure noise: 0.4% for iid (2/500) and 5.2% for ar98 (26/500, interval 3.6%–7.5%).
  - Rule 4 has limited power on slow factors: at IC = 0.02 only 52% pass (104/200).
  - If the 485 combinations were independent with a 5.2% noise pass rate each, the probability of ≥ 30 passes by chance would be about 0.19.
- **Conclusion**: Rule 1 is calibrated. Rule 4's 6.2% is the same order of magnitude as the 5.2% for slowly varying noise, so it is not evidence of skill. Conversely, Rule 4 misses nearly half of medium-strength slow signals, so "455 failed Rule 4" does not prove that those strategies carry no information either. The cause is that a bootstrap block length of 20 is too short for slow factors, which makes the intervals too narrow. (This finding must not be used to retroactively change v1's rules. It can only go into the pre-registration of a new experiment.)
- **Limitations**: The synthetic market is too clean: normal returns, no industry structure, no price limits. Rule 1's cost was set to 0, so the known risk around cost matching in its null distribution was **not tested**. Only one persistence level (φ = 0.98) and 3 IC levels were tested.
- Script: `scripts/t008_power_calibration.py`

### C03 Correcting the "three variants independently confirm" claim

- **Question**: Can "three robustness variants agree" in the T008 conclusion report count as independent confirmation?
- **Method**: The wording of `reports/T008_跨市场结论总结.md` (the T008 cross-market conclusion report) was rewritten against the project handoff document, and a correction log was added. No new statistical evidence was produced.
- **Key facts**: Both variants, `replay_universe300` and `replay_top10`, got their 485/485 verdicts by reusing the frozen verdicts from `replay_500` (`frozen=true`). Agreement among the three is a mechanical consequence of that reuse. The C03 report also cites the handoff document on several points. Of `replay_500`'s 485 verdicts, 13 reused even earlier frozen verdicts (null_k=100) and 472 were new evaluations (null_k=1000). Rule 1's 309/485 are uncorrected raw p-values. The null distribution's cost uses the random controls' same-day average cost rather than each candidate's own trading path, which may favor candidates. With m=490 and null_k=1000, passing a Holm correction is impossible.
- **Conclusion**: Agreement among the three variants is **not** robustness evidence. It cannot be used to claim that the result is robust to the liquidity threshold or to holding concentration. "0 survivors" currently rests on the single `replay_500` run.
- **Limitations**: Only wording was changed. The same T008 report still has two phrases ("statistically significant", "the strongest significance statistically achievable") that are stronger than the handoff document supports. C03 left them for the author to decide; until then, use this page and the corrected wording. The two reports also describe the T008 training period differently (C01: "2024-01 to 2025-06"; C03: "2024-01 to 2026-03"). This page does not resolve the difference.

### C02 The promotion test's random control: block sampling that preserves holding persistence

- **Question**: The promotion test's random control picks stocks independently every day. Real strategies often signal the same stock for several consecutive days, and adjacent 5-day returns overlap by 4/5. Does that make the control too narrow and cause false positives?
- **Method**: A new "block sampling" null was written: once a random holding picks a stock, it keeps that stock for L consecutive signal days. The false-positive rates of the two nulls were compared on a synthetic market of 3,000 stocks, with strategies that have **no selection skill at all**, average holding-run lengths of 1/3/5/10, 12 complete weeks and 400 repetitions per tier.
- **Key numbers**:
  - With no persistence (run length 1) both nulls are correct, with false-positive rates of 3.7% / 3.3% at α = 5%.
  - With any persistence, the independent daily null is only 50%–61% as wide as the true variation. Skill-less strategies are then judged significant 16%–19% of the time at α = 5%. At Holm's first-step threshold of 0.002, they pass 11, 16 and 23 times out of 400 (2.75%, 4.0%, 5.75%), where the rate should be ≤ 0.2%.
  - The block null matches the true variation (standard-deviation ratio 0.99–1.02), and its false-positive rate returns to 4.0% at α = 5%.
- **Conclusion**: This does not change the conclusion that no strategy has passed the promotion test yet. Every strategy is stuck at the sample-size gate, with 1 complete week against a required 12. But once 12 weeks accumulate, keeping the independent daily null would make persistent but skill-less strategies more likely to pass by mistake.
- **Limitations**: **It was not run on the real signal ledger**, because all ledger data is from 2026-10, after the research cutoff. The synthetic persistence mechanism matches the block null exactly, which favors the block null. Neither null models correlation between same-day signals, such as sector or size tilts.
- Script: `scripts/promotion_block_null.py`

### C13 Pre-registration plan for a v2 low-frequency replay (plan only, not run)

- **Question**: What would make a T008 v2 a valid test?
- **Method**: A pre-registration plan was written in `docs/replay_v2_preregistration.md`. It is a draft and has not been frozen.
- **Key numbers**:
  - Candidates: 13 signal settings × 6 rebalancing schedules = 78, plus 6 random controls.
  - Trial count m = 490 (v1) + 72 (another search, an upper bound) + 84 (v2) = 646. If that other search was never formally run, m = 574. Holm requires `null_k` ≥ 12,919, so the plan fixes `null_k = 20,000` (v1's 1000 makes significance mathematically impossible).
  - Only forward data after the freeze date can be used. With a freeze date of 2026-11-02, 52 full weeks end on 2027-11-01.
- **Conclusion**: There is currently no clean data. At m = 646, one year of data requires z ≈ 3.78, roughly an annualized information ratio of 3.8, which is almost unattainable. "No candidate confirmed" is written in advance as a legitimate outcome.
- **Limitations**: Power is extremely low. The candidate list was written after learning that v1 failed completely, so researcher degrees of freedom cannot be fully ruled out. Fee rates are still placeholders.

---

## 3. Real-world frictions: costs, capacity, price limits

### C11 Transaction-cost sensitivity and break-even costs

- **Question**: How much of each strategy's return is left at one-way costs of 0 / 8 / 15 / 30bp? At what cost does net excess return over cash reach zero?
- **Method**: The window is 2013-01-01 to 2026-03-31, with 5 universes: SPY, 510300, equal-weight US ETFs, equal-weight A-share ETFs, and an equal-weight basket of 300 A-share stocks. The study reused the cost parameter of the project's backtest, interpolated or extrapolated linearly to find break-even points, and used a block bootstrap for intervals.
- **Key numbers**:
  - 32 of 35 cells have a break-even point. 3 lose money even at zero cost.
  - rsi2 breaks even at only 13–24bp in all 5 universes, and at 30bp its net excess is negative in all 5 (−0.87% to −2.33%).
  - blend2 breaks even at 35–67bp. At 8bp its net excess is 2.1%–5.0% across the 5 universes.
  - Trend strategies: trend_ens 89–245bp and trend_vol 84–222bp. All 5 universes are extrapolated values, meaning net excess was still > 0 at 30bp.
  - First-order approximation: break-even ≈ zero-cost net excess ÷ annual turnover. For example, rsi2's annual turnover on SPY is 16.87×, while trend_ens's is only 2.95×.
  - Bootstrap intervals are wide. blend2's 67bp on SPY has interval [31, 107], blend2 on 510300 has [0, 113], and all 7 strategies in the stock universe have a lower bound of 0.
  - Against buy-and-hold, excess is almost always negative even at zero cost. The only exception is trend_vol on 510300 (+0.26%, break-even 7.4bp).
- **Conclusion**: Low-turnover trend strategies are insensitive to costs, and high-turnover rsi2 is very sensitive. But costs only subtract further from results that already trail the market. In none of the 35 cells is the break-even cost **significantly** above realistic costs. Only the point estimates can be read.
- **Limitations**: No multiple-testing correction. The cost model is a symmetric bp charge: it ignores the sell-side-only stamp duty, historical tax-rate changes and minimum commissions. The stock universe has survivorship bias; its buy-and-hold returns 16.49% a year, which is clearly too high. "Realistic A-share costs are about 3–8bp for ETFs, 8–15bp for large caps and 15–40bp for small/mid caps" is the report author's unverified assumption.
- Script: `scripts/cost_breakeven.py`

### C07 Strategy capacity

- **Question**: As capital grows, a single rebalance becomes a large share of a stock's daily turnover and moves its price. How much capital can each strategy hold before impact costs eat its returns?
- **Method**: The window is 2020-01-01 to 2026-03-31 on 1,000 A-share stocks, with capital split equally across N = 20 stocks. Impact uses a square-root model, c·σ·√(participation rate). c is an assumed value, set to 0.1 / 0.5 / 1.0. Capacity is defined as the capital at which annualized impact drag equals 1 percentage point.
- **Key numbers** (N = 20, c = 0.5):
  - Capacity: blend2 ≈ CNY 130 million, trend_ens ≈ CNY 540 million, rsi2 only ≈ CNY 7.63 million, ma_adx_original ≈ CNY 18.57 million.
  - Range including uncertainty in c and in turnover estimates: blend2 ≈ CNY 16.25 million to 6.5 billion, which spans more than two orders of magnitude.
  - With capital of CNY 100k, 1M or 10M, none of the 7 strategies has a single rebalance above 5% participation. At CNY 100M, 5.0% of blend2's trades exceed 5%, compared with 43.3% for rsi2 and 45.6% for ma_adx_original.
  - Gross annual excess over cash is already small: blend2 1.25%, trend_ens 0.99%, rsi2 1.89%, ma_adx_plus −0.11%.
- **Conclusion**: These numbers are orders of magnitude only. High-turnover, all-in/all-out strategies (rsi2, original MA+ADX) have far less capacity. In this sample, "small caps have low capacity" comes almost entirely from low turnover, not from higher volatility or higher strategy turnover.
- **Limitations**: **Average daily turnover (ADV) comes from a side table dated 2026-09-11 to 2026-10-29, after the research cutoff.** It is applied backward only as a static liquidity level, and signals, fills and returns all stop at 2026-03-31. A single stock's ADV estimate can be off by a factor of two. c was not calibrated; since capacity K\* ∝ 1/c², moving c from 0.5 to 0.1 multiplies capacity by 25. The execution assumption (the whole order fills at the open) can err in either direction. The sample has survivorship bias, and no confidence intervals were given.
- Script: `scripts/strategy_capacity.py`

### C12 Limit-locked bars and suspensions ("can't sell")

- **Question**: The backtest assumes every order fills at the next open. How much does that overstate returns when a stock opens and stays locked at limit-up (can't buy) or limit-down (can't sell)? Is the problem worse for small caps?
- **Method**: 4,813 Shanghai/Shenzhen stocks from 2013 to 2026-03 were tested with the project's existing limit-locked-bar rule. A sample check of 164,932 bars against the original function found 0 mismatches. "Ideal fill" was compared with "defer while limit-locked".
- **Key numbers**:
  - Ideal fill overstates returns by 0.02–0.16 percentage points per year (per-strategy means of 0.022–0.164). More than 70% of stocks never hit a locked bar, and the median difference is 0.
  - Hit rates are a few per thousand: 0.177% of sell signals hit a limit-down lock and 0.230% of buy signals hit a limit-up lock.
  - Each hit is costly. The median adverse price move is about 10% (9.93% for sells, 9.98% for buys), and the buy-side P90 is 44.01% (missing consecutive limit-up days).
  - Events are highly clustered: on 2020-02-03 alone, 1,328 stocks (28% of the sample) were locked limit-down.
  - Across turnover quintiles there is no monotonic "smaller is worse" gradient.
  - The project's existing price-limit handling is 0.02–0.09 percentage points per year more optimistic than the defer-on-lock method. One reason is that ChiNext's 10% limit before 2020-08-24 is treated as 20%.
- **Conclusion**: The effect on surviving non-ST stocks is small compared with the 8bp one-way cost. But it is a **lower bound**. The worst "can't sell" cases (ST stocks, consecutive limit-downs until delisting, suspension followed by delisting) are not in the sample at all, so this does **not** show that small caps are free of the problem.
- **Limitations**: There are no delisted stocks and ST stocks are excluded. The size label comes from **an autumn-2026 turnover side table, after the cutoff**. It is used only as a static label and is only weakly related to each stock's real size at the time. The bootstrap assumes stocks are independent while events cluster on a few days, so the intervals are too narrow. "Defer" is itself optimistic, because reality is worse during consecutive locked days.
- Script: `scripts/limit_lock_impact.py`

---

## 4. Are the backtesting tools correct?

### C05 Automated look-ahead detection

- **Question**: Do the 7 production strategies or the 21 shadow candidates have signals that "peek at tomorrow", or executions that do not fill on the next bar?
- **Method**: A "time travel" experiment on synthetic prices: after deleting or tampering with all data after day t, every signal on or before t must be bit-identical. An impulse test confirms that fills happen at the next bar's open. As a self-check, the detector must catch 4 deliberately cheating signals and 2 cheating executions.
- **Key numbers**:
  - 28 signals × 3 price paths × 20 cut points, under both delete-future and tamper-future tests: 0 violations.
  - Exhaustive version: 899 cut points per signal on one path, 0 violations in total.
  - Mutation tests: replacing the moving average with a centered one makes all 18 strategies that use moving averages fail. Changing fills to the same bar makes the impulse test report 57 violations. So the detector does fire.
  - Power against rare leaks is limited. If a strategy peeks on only 1% / 2% / 5% / 10% / 30% of days, sampling 20 cut points catches it with probability of about 21% / 37% / 69% / 91% / 100%.
- **Conclusion**: No look-ahead bias and no fill-timing errors were found. A new test, `tests/test_no_lookahead_all.py` (15 tests, about 7 seconds), was added.
- **Limitations**: "Not detected" is not "impossible". The synthetic prices have no suspensions, adjustment jumps or half-day bars. The backtrader and AFL versions were not tested. This result **only shows that signals do not peek at the future. It says nothing about whether the strategies make money.**

### C09 Rule consistency across the three engines (custom / backtrader / RQAlpha)

- **Question**: Do the three strategy codebases really implement the same rules?
- **Method**: Static line-by-line comparison, an automated check of parameter defaults, and dynamic comparison on synthetic prices (20 seeds × 7 strategies × 3 engine pairs).
- **Key numbers**:
  - Of 58 (strategy, parameter) items, 35 have two or more comparable sources, and 0 of those disagree.
  - Days with differing target positions are at most 0.0806% (the test's upper bound is 0.5%). With random valid parameters there are 0 open/close disagreements.
  - In 13 of 14 end-to-end runs the fill dates are identical. The other run is offset because of D1 below.
  - 7 implementation-level differences (D1–D7) were found, and none changes a strategy rule. The main one is D1: backtrader waits until every indicator has a value before it starts, so trend_ens and blend2 have a different warm-up and later fill dates may shift. Another is D3: when the close exactly equals the moving average, the three summation methods round differently, and a strict inequality can flip.
- **Conclusion**: The rules are consistent, but the engines are not bit-identical. The custom engine also has a different cost model and lot-size handling from the other two, so comparisons should use signals and fill dates, not returns. A new regression guard, `tests/test_engine_rules.py`, was added, and the difference list is in `docs/engine_rules_compare.md`.
- **Limitations**: The synthetic prices have no price limits, suspensions or corporate actions. D6 and D7 were only checked statically. There were only 14 end-to-end runs, so "1 in 14" is not a frequency estimate.

### C10 Python MA+ADX+ATR vs the AmiBroker AFL

- **Question**: Does the Python implementation follow the same rules as the AmiBroker `.afl` file?
- **Method**: A 20-item static comparison, plus a line-by-line translation of the AFL loop into Python whose trades were compared one by one with `backtest.run`. AmiBroker is not installed on this machine, so these results do not come from running AmiBroker itself.
- **Key numbers**:
  - All 20 rule items match.
  - Synthetic prices (not rounded): signals agree in all 120 runs, with 5,019 trades per side and 0 trade mismatches. With prices rounded to the cent, which creates exact moving-average ties, 111 of 120 runs agree, and every difference sits next to a tie bar.
  - 8 real ETFs (2005-01-03 to 2026-03-31, 491 trades in total): 0 trade mismatches.
  - However, the CSV exported to AmiBroker keeps only 4 decimals (`%.4f`) while Python reads full precision. Running the Python version on both precisions of the same data gives signal differences in 4 of 8 ETFs (for example, QQQ goes from 69 trades to 68).
- **Conclusion**: No rule-level mismatch was found. But if the two sides of the comparison tool `compare_amibroker.py` read data at different precisions, it will report data differences as rule differences. A new test, `tests/test_afl_consistency.py`, was added, and the comparison table is in `docs/afl_python_rules_compare.md`.
- **Limitations**: There is no real AmiBroker output, and the translation may misread some AFL semantics. Whether the Wilder smoothing seed and warm-up match AmiBroker cannot be verified. This study does not address strategy effectiveness.

---

## How this page was written

- Every number is taken from the text of the corresponding research report. Nothing was recomputed or estimated. The only change in the English version is unit conversion (亿 / 万 CNY → millions / billions).
- C07 and C12 use a turnover side table dated after 2026-03-31, but only as a static size label that never enters signals, fills or returns. Both reports say so, and that note is kept here.
- C02 did not compute p-values on the real signal ledger, because that data is later than the cutoff. That is also stated here as it is.
- For a one-line summary suitable for a résumé, use: **"After correcting for the project's actual number of trials, none of the 7 production strategies passed on 12 ETFs; they reduced drawdowns but did not beat buy-and-hold."** Do not quote the p-value of a single strategy on a single instrument.
