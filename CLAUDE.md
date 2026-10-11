# 给 Claude / Claude Code 的说明

**开始工作前先读 `项目交接.md`**：里面有项目当前状态、代码结构、做过的决定和待办。

## 关于我
- UC Berkeley 数学系学生，对量化金融感兴趣，这个项目会放到简历和 GitHub 上。
- 用中文和我交流，回复要详细；代码注释用中文。
- 我需要能在面试里讲清楚每一行：改动前先说明打算怎么改、为什么；涉及统计或金融概念时解释清楚。
- 给我的终端命令不要带 `#` 注释（我用 zsh，会报错）。

## 项目约定
- 运行环境：`.venv/bin/python`，依赖在 requirements.txt。
- 改了 `src/` 后先跑 `python tests/test_strategies.py` 和 `python tests/test_basic.py`。
- 外部引擎（`engines/`）：backtrader / RQAlpha 装在 `.engines_lib/`（需要 pandas 2.x），运行时加 `PYTHONPATH=.engines_lib`；改了 `engines/` 后跑 `PYTHONPATH=.engines_lib .venv/bin/python tests/test_engines.py`。
- 三个引擎（自研 / backtrader / RQAlpha）的策略规则要保持一致；改一个时提醒我同步另外两个（`src/strategies.py`、`engines/bt_strategies.py`、`engines/rq_strategies.py`）。
- 严禁未来函数：信号只能用当根 K 线收盘及以前的数据，成交在下一根。
- A 股：北京时间 15:30 前的当天 K 线不能用（盘中半天数据）。
- 东方财富接口有频率限制，只用于每天一次的全市场快照；历史回补用新浪/腾讯。
- 安全：网页服务只能监听 127.0.0.1；不保存任何账号密码；不接交易接口；`.secret/` 和 `data/` 不上传。
- 回测结果好不代表策略有效。给我看结果时，同时指出过拟合、多重检验、样本量、交易成本等问题。
- Python 版本和 `amibroker/ma_cross_adx_atr.afl` 的规则要保持一致；改其中一个时提醒我同步另一个。
