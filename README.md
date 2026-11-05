# quant-backtest

A Python backtesting and research system for China A-shares (about 5,200 stocks) plus A-share and US ETFs, with a local dashboard.

> For research and education only. Not investment advice. The project does not connect to any broker and never places orders.

**Short version of what the research found: so far there is no evidence that any strategy here has real timing or stock-selection skill, and the data is not enough to prove they do not work either. See [Research findings](#research-findings).**

## What it does

- **Data.** Downloads daily prices for Shanghai/Shenzhen stocks, A-share ETFs and US ETFs, and updates them every day with a scheduled job. Sources are free public ones (Sina and Tencent for history, BaoStock as a fallback, Yahoo Finance for the US, Eastmoney only for a once-a-day full-market snapshot).
- **Strategies.** Seven single-asset strategies, run on every stock and ETF:
  - trend ensemble (4 moving averages) and trend + volatility targeting (one moving average),
  - Turtle breakout (Donchian System 1),
  - RSI(2) mean reversion,
  - MA crossover + ADX, in an original and an improved version,
  - `blend2`: one third each of trend ensemble, RSI(2) and Turtle.

  There is also a dual-momentum rotation across ETFs (it needs several assets, so it is not run on single stocks). Buy-and-hold is the benchmark. That is 8 strategies plus the benchmark.
- **Local web dashboard.** Today's trade list, full-market results, single-stock charts, a backtest lab where you can change parameters and costs, and engine comparison pages.
- **Signal ledger and promotion test.** Every signal on the daily list is recorded and later scored against what the market did. A strategy can only be promoted to "main strategy" if it beats matched random controls after a multiple-testing correction (Holm), over enough full weeks of signals. With the data of 2026-10-30, none has passed; `blend2` is only the default, not a promoted strategy.
- **Paper trading and holdings log.** Simulated accounts that follow the signals (A-share rules: next-open fills, 100-share lots, T+1, itemized fees), and a notebook for your own positions. Nothing is sent to a broker.
- **Optional extras.** A market monitor, a fundamentals module, an experimental "idea translator" that turns a plain-language idea into a strategy configuration that a person must confirm (disabled unless configured), and an offline research tool that replays cross-sectional strategies day by day (called T008 in the code).
- **Cross-checks.** The same strategy rules are implemented three times: my own engine, [backtrader](https://www.backtrader.com/) and [RQAlpha](https://github.com/ricequant/rqalpha). The Python version of one strategy is also compared with my AmiBroker (AFL) version, trade by trade.

## Design choices

- **No look-ahead.** A signal uses data up to the close of day t only. The trade fills at the open of day t+1. A test cuts the data at day t and checks that the signal does not change (`tests/test_strategies.py`). A broader automated check covers all 7 production strategies and the shadow candidates (`tests/test_no_lookahead_all.py`).
- **A-share rules.** The main engine works on target weights and fills at the next open. If the open is locked at limit-up (cannot buy) or limit-down (cannot sell), the order is deferred to the next day. It does not model 100-share lots. Lots and itemized fees are modeled by the backtrader/RQAlpha wrappers, and lots, T+1 and itemized fees by the paper-trading account.
- **Costs.** In the main engine, costs are one symmetric charge per unit of turnover:

  | Market | Single stocks | ETFs |
  |---|---|---|
  | A-shares | 8 bp one way | 5 bp one way |
  | US | 10 bp one way | 5 bp one way |

  Commission, stamp duty and slippage are folded into that one number, and it is the same for the whole history. The real stamp duty on A-share sells was halved in August 2023, so the true cost of high-turnover strategies was different before and after. Other parts of the project use other cost models: backtrader/RQAlpha use itemized fees with a dated stamp duty, the paper-trading account uses itemized fees, and the T008 replay adds an explicit 5 bp slippage. **The fee rates have not been checked against a real brokerage account.** `docs/cost_model.md` lists every model and what happens if you mix them up.
- **No half-day bars.** A bar is only used once the market is closed and the data is complete: after 15:30 Beijing time for A-shares and after 16:30 New York time for the US.
- **In-sample / out-of-sample reporting.** Results are reported separately for 2013-2019 and for 2020 onward. Default parameters come from published sources, not from fitting. But the list of strategies, and which three go into `blend2`, were fixed after seeing results that included 2020 onward, and the post-2020 numbers have been looked at many times. So "out-of-sample" here only means "later in time", not a clean test.
- **Access model.** The web server listens on 127.0.0.1 only and needs an access token (stored in a local file with owner-only permissions) plus a Host-header check. There are two optional ways to reach it from outside, both off by default:
  1. *Temporary share link.* A Cloudflare quick tunnel forwards a random public address to the local server. The link carries the access token, so treat it like a password. An optional share password can be added. The admin pages answer 404 on this path.
  2. *Owner channel.* Cloudflare Access in front of a fixed domain. A request is accepted only if the Host matches the configured domain, the Access-signed login token is valid, and the email is on an allow list. If it is not configured, the channel is closed.

  The project stores no brokerage credentials and has no trading interface.

## Results

### Full-market scan

The daily scan runs the 7 single-asset strategies on every stock with at least 250 trading days of history. In the scan dated 2026-10-30 it covered 4,937 stocks (196 ST stocks were excluded, and no delisted stocks are loaded). For each strategy, this is the share of stocks where the strategy's full-period Sharpe ratio is higher than buy-and-hold's:

| Strategy | Stocks evaluated | Higher Sharpe than buy-and-hold |
|---|---|---|
| `blend2` | 4,888 | 31.6% |
| `donchian_s1` (Turtle) | 4,888 | 31.8% |
| `trend_vol` | 4,880 | 31.3% |
| `trend_ens` | 4,887 | 31.2% |
| `rsi2` | 4,866 | 28.5% |
| `ma_adx_plus` | 4,862 | 25.6% |
| `ma_adx_original` | 4,888 | 24.9% |

So all 7 strategies have a better Sharpe than buy-and-hold on fewer than a third of the stocks. This is a descriptive count, not a significance test. It moves slightly with every daily update, it ignores survivorship bias (see below), and it uses the simple 8 bp cost.

### Engine check

On one stock (600531), an early check found that the three engines agree for the simple 0/1 position strategies. The original MA + ADX strategy gave annualized returns of 6.06%, 5.91% and 5.99%, and all 98 trades happened on the same dates. This shows the implementation is consistent. It does not show that the strategy makes money. A later, more systematic comparison is summarized in the research findings: the rules are the same in the three codebases, but there are 7 implementation-level differences, and the engines use different cost models, so compare signals and fill dates, not returns.

## Research findings

A separate page collects the results of 14 research tasks run in October 2026: deflated Sharpe ratios, out-of-sample decay, parameter stability, strategy overlap, cost break-even, capacity, limit-up/limit-down effects, a look-ahead audit, and consistency checks between the three engines and AmiBroker. **Most results are negative, and they are reported as they are.** In short:

- After correcting for the number of trials (m = 515), none of the 168 strategy-instrument-period cells reaches a Deflated Sharpe Ratio of 0.95. The best one scores 0.386.
- No strategy beats buy-and-hold on annual excess return, in-sample or out-of-sample. What the strategies deliver is smaller drawdowns.
- The T008 cross-sectional replay had 0 survivors out of 485 (strategy, parameter) combinations.
- The trend ensemble sits on a parameter plateau. The MA + ADX strategies do not.
- No look-ahead bias was found in 28 signals.

Full page: [docs/research_findings.en.md](docs/research_findings.en.md) (中文版: [docs/research_findings.md](docs/research_findings.md)). It also lists the limits of each study, for example that the post-2020 period is not a clean out-of-sample period, and that the stock samples contain no delisted companies.

## Limitations

- **Survivorship bias.** The stock universe does not include delisted stocks and most studies also exclude ST stocks, so the results are too optimistic.
- **Free data.** Prices come from free public sources, not a paid data vendor.
- **Simple execution and costs.** There is no market impact or liquidity model in the main engine. The cost is one symmetric number, the fee rates are unverified, and the stamp-duty change in 2023 is not modeled there. Capacity and limit-lock effects were estimated separately in the research findings.
- **Multiple testing.** I test many strategies, parameters and stocks, so some good-looking results will happen by chance. The research findings quantify this, and the answer is that nothing survives the correction.
- **Not a clean out-of-sample.** See the in-sample / out-of-sample note above.
- **Small samples.** Each ETF has only 6-7 years per period, so the sampling error of a Sharpe ratio is about as large as the differences between strategies.

## How it was built

I built the platform step by step with Claude and ChatGPT, and I directed the design and the rules above. The AmiBroker strategy and the data checks are my own work.

## Quick start

Python 3 on macOS. All commands use the virtual environment's Python.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

.venv/bin/python tests/test_basic.py
.venv/bin/python tests/test_strategies.py
.venv/bin/python run_backtest.py SPY
```

`tests/` holds more than 130 test scripts. The two above are the quick checks to run after changing anything in `src/`. There is no single command that runs all of them yet.

Price data is not included in this repository. Download it with:

```bash
.venv/bin/python daily.py --market cn
.venv/bin/python backfill.py
```

Start the dashboard with `.venv/bin/python app.py`, then open it with `./scripts/open_dashboard.sh`, which opens the local address together with your access token.

backtrader and RQAlpha need pandas 2.x, so they are installed separately in `.engines_lib/` and used with `PYTHONPATH`:

```bash
PYTHONPATH=.engines_lib .venv/bin/python tests/test_engines.py
```

## Comparing with AmiBroker

`amibroker/ma_cross_adx_atr.afl` has the same rules as the Python MA + ADX + ATR strategy. The aim is that two independent implementations produce the same trades.

1. Import the price file that the Python side writes when it downloads a symbol (`data/<SYMBOL>_amibroker.csv`) into AmiBroker, so both sides use the same data. Do not use AmiBroker's own data feed: the price adjustment differs and the results will not match.
2. Run the AFL in the Analysis window, export the trade list to `amibroker/<SYMBOL>_ab_trades.csv`, and optionally export an Exploration to `amibroker/<SYMBOL>_ab_explore.csv`.
3. Compare:

   ```bash
   .venv/bin/python compare_amibroker.py SPY amibroker/SPY_ab_trades.csv --explore amibroker/SPY_ab_explore.csv
   ```

A known trap: the exported CSV keeps only 4 decimals, while Python reads full precision, which can create small false mismatches (see `docs/afl_python_rules_compare.md`). A rule-by-rule comparison of the two versions is in the same file.

## Project structure

```text
src/             indicators, strategies, backtest engine, metrics, costs, data sources,
                 signal ledger, promotion test, paper trading, replay research tool
engines/         backtrader and RQAlpha wrappers, parameter search, custom strategies
dashboard/       web front end (plain JavaScript)
amibroker/       AmiBroker AFL version of the MA + ADX strategy
scripts/         research scripts (deflated Sharpe, cost break-even, capacity, ...), data checks, helpers
tests/           self-checks, including the look-ahead tests
docs/            how-tos, design notes, audits (costs, engine rules, AFL vs Python)
                 and the research findings
app.py           local web server
daily.py         daily job: update data, backtest, build the report
backfill.py      one-time download of the full A-share history
run_backtest.py  command-line backtest for one symbol
config.py        markets, symbols, cost numbers, server address
tunnel.py        optional temporary share link (Cloudflare quick tunnel)
```
