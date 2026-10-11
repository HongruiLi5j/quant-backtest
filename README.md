# quant-backtest

A Python backtesting system for China A-shares (about 5,200 stocks), with a local dashboard.

> For research and education only. Not investment advice. The project does not connect to any broker and never places orders.

## What it does

- Downloads daily prices for Shanghai/Shenzhen stocks, A-share ETFs and US ETFs, and updates them every day.
- Backtests 8 strategies on every stock. The strategies include trend following, Turtle breakout, RSI(2) mean reversion, MA crossover + ADX, and dual momentum. Buy-and-hold is the benchmark.
- Runs a local web dashboard: today's signals, full-market results, single-stock charts, a backtest lab where you can change parameters and costs, and signal tracking.
- Checks results with three independent engines: my own engine, [backtrader](https://www.backtrader.com/) and [RQAlpha](https://github.com/ricequant/rqalpha).
- Compares the Python version of one strategy with my AmiBroker (AFL) version, trade by trade.

## Design choices

- **No look-ahead.** A signal uses data up to the close of day t only. The trade fills at the open of day t+1. A test cuts the data at day t and checks that the signal does not change (`tests/test_strategies.py`).
- **A-share rules.** T+1, 100-share lots, no fills at limit-up/limit-down opens, stamp duty and commission.
- **Costs.** 8 bp one way for single stocks, 5 bp for ETFs.
- **No half-day bars.** Before 15:30 Beijing time, today's bar is not used.
- **In-sample / out-of-sample split.** Parameters are chosen on 2013-2019 and tested on 2020 onward. Default parameters come from published sources, not from fitting.
- **Local only.** The web server listens on 127.0.0.1 and needs an access token.

## Results

I ran 7 strategies on 4,929 stocks. None of them beat buy-and-hold on more than 30% of the stocks.

On one stock (600531), the three engines agree for the simple 0/1 position strategies. The original MA + ADX strategy gives annualized returns of 6.06%, 5.91% and 5.99%, and all 98 trades happen on the same dates. This shows the implementation is consistent. It does not show that the strategy makes money.

## Limitations

- **Survivorship bias.** The stock universe does not include delisted stocks, so the results are too optimistic.
- **Free data.** Prices come from free sources (Sina, Tencent, Eastmoney through AKShare), not a paid data vendor.
- **Simple execution.** There is no market impact or liquidity model.
- **Multiple testing.** I test many strategies and stocks, so some good-looking results will happen by chance.

## How it was built

I built the platform step by step with Claude and ChatGPT, and I directed the design and the rules above. The AmiBroker strategy and the data checks are my own work.

## Quick start

Python 3 on macOS.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python tests/test_basic.py
python tests/test_strategies.py
python run_backtest.py SPY
```

Price data is not included in this repository. Download it with:

```bash
python daily.py --market cn
python backfill.py
```

backtrader and RQAlpha need pandas 2.x, so they are installed separately in `.engines_lib/`.

## Project structure

```text
src/             indicators, strategies, backtest engine, metrics, data sources
engines/         backtrader and RQAlpha wrappers, parameter search, custom strategies
dashboard/       web front end (plain JavaScript)
amibroker/       AmiBroker AFL version of the MA + ADX strategy
tests/           self-checks, including the look-ahead test
docs/            earlier notes on the SPY MA + ADX strategy (Chinese)
app.py           local web server
daily.py         daily job: update data, backtest, build the report
```
