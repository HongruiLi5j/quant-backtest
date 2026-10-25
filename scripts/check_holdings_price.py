"""检查持仓页"最新价"是不是不复权的真实价（T021）。只读本地日线和真实价缓存，不联网，不读持仓数据库。

用法（在项目根目录）：
  .venv/bin/python scripts/check_holdings_price.py 600531 15.465 1200
三个参数：股票代码、成本价、股数（成本价/股数只用来演示盈亏怎么算，不会写入任何地方）。
"""
import sys
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import MARKETS  # noqa: E402
from src import datasource, display_price  # noqa: E402
from src.holdings import calc  # noqa: E402


def main(argv):
    if len(argv) != 4:
        sys.exit(__doc__)
    code, cost, qty = argv[1], D(argv[2]), int(argv[3])
    cfg = MARKETS["cn"]
    df = datasource.drop_incomplete_bar(datasource.load_cached("cn", code), cfg["tz"], cfg["close_ready"])
    if df is None or df.empty:
        sys.exit(f"{code}: 本地没有日线数据")
    last, day = float(df["Close"].iloc[-1]), df.index[-1].strftime("%Y-%m-%d")
    k, note = display_price.cached_scale("cn", code, df)
    print(f"{code}  本地日线最后一根(后复权) {day}: {last:.3f}")
    if note != "前复权":
        print("本地缓存里没有这一天的真实收盘价 -> 持仓页会显示『无现价』（不会显示后复权价）。")
        print("只有那一天的真实价被成功取到并写入缓存、且日期和这根 K 线匹配之后才会有；不保证打开详情页或等每日更新就一定会有。")
        return
    px = round(last * k, 4)
    print(f"还原后的真实收盘价: {px}（缓存系数 k={k:.6f}）")
    trades = [dict(id=1, trade_date="2000-01-01", code=code, side="buy", qty=qty, price_milli=int(cost * 1000), fee_cent=0)]
    for label, p in (("修复后(真实价)", px), ("修复前(后复权价)", last)):
        r = calc.positions(trades, lambda c, p=p: (D(str(p)), day), day)["positions"][0]
        print(f"  {label:10s} 最新价={r['price']}  浮盈={r['floating_pnl']}  涨幅={float(r['floating_pct']) * 100:+.2f}%")
    print(f"  手算 {qty} x ({px} - {cost}) = {qty * (D(str(px)) - cost):.2f}")


if __name__ == "__main__":
    main(sys.argv)
