import numpy as np
import pandas as pd
import pandas_ta as ta

close = np.array([100,102,101,103,105,104,106,108,107,109,111,110,112,114,113,115,117,116,118,120], dtype=float)
df = pd.DataFrame({'close': close})

df['rsi_5'] = ta.rsi(df['close'], length=5)
df['sma_5'] = ta.sma(df['close'], length=5)

macd_df = ta.macd(df['close'], fast=5, slow=10, signal=3)
print("MACD 列名:", list(macd_df.columns))
df['macd'] = macd_df.iloc[:, 0]
df['macd_sig'] = macd_df.iloc[:, 1]
df['macd_hist'] = macd_df.iloc[:, 2]

bb_df = ta.bbands(df['close'], length=5)
print("BBands 列名:", list(bb_df.columns))
df['bb_lower'] = bb_df.iloc[:, 0]
df['bb_mid'] = bb_df.iloc[:, 1]
df['bb_upper'] = bb_df.iloc[:, 2]

print("\n=== 结果 ===")
print(df.round(2))
