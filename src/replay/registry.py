"""T008 逐日回放:候选策略清单的读取、校验与哈希。

清单文件是 strategies_catalog.json(100 个候选,含 5 个随机对照)。
本模块只做三件事,不依赖 pandas/numpy,不联网,不读 data/:
  1. load_catalog:读取 JSON;
  2. validate_catalog:检查 ID 唯一、参数 ≤3、每个参数网格 3~5 档、总数=100、各类别数量、
     随机对照 5 个且种子固定且互不相同、版本号与"只用 T 日收盘前数据"声明齐全;
  3. catalog_hash:对清单做规范化后求 SHA-256。任何字段改动(包括描述、网格、种子)都会改变哈希,
     试验记录里要写下这个哈希,这样事后能证明"跑的是哪一版清单"。

为什么要哈希:100 个策略里最好的那个大概率是运气(多重检验)。如果清单可以悄悄增删改,
试验次数就无法核对,Holm 校正的 m 也就不可信。哈希把"候选集合"钉死。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path

CATALOG_PATH = Path(__file__).with_name("strategies_catalog.json")

EXPECTED_TOTAL = 100
MAX_PARAMS = 3
MIN_GRID = 3
MAX_GRID = 5
CONTROL_CATEGORY = "随机对照"
CONTROL_PREFIX = "ctrl_"          # 与 src/promotion.py 的 is_control 约定一致
CONTROL_COUNT = 5

# 各类别应有的策略数(PROJECT_SCHEDULE 第 3 节 T008 S0)
EXPECTED_COUNTS = {
    "动量": 8, "反转": 5, "均线": 9, "突破": 6, "低波动": 5, "特质波动": 3,
    "量能流动性": 8, "量价形态": 6, "波动收敛": 6, "超买超卖": 6, "行业动量": 6,
    "隔夜": 5, "52周高点": 3, "极端收益": 5, "低成交额代理": 4, "复合": 10,
    CONTROL_CATEGORY: CONTROL_COUNT,
}

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}$")
_VER_RE = re.compile(r"^\d+\.\d+$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DIRECTION = "higher_better"        # 所有非对照策略:分数越高越靠前,事后不得翻转
CONTROL_DIRECTION = "random"
_REQUIRED = ("id", "category", "name", "description", "params", "version", "point_in_time", "direction")


class CatalogError(ValueError):
    """清单不合规。消息里带出所有问题,方便一次改完。"""


def load_catalog(path=None) -> dict:
    """读取清单 JSON(默认读包内的 strategies_catalog.json)。只读,不校验。"""
    p = Path(path) if path is not None else CATALOG_PATH
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check_params(sid, params, errs):
    if not isinstance(params, dict):
        errs.append(f"{sid}: params 必须是对象")
        return
    if len(params) > MAX_PARAMS:
        errs.append(f"{sid}: 参数 {len(params)} 个,超过上限 {MAX_PARAMS}")
    for name, grid in params.items():
        if not isinstance(grid, list):
            errs.append(f"{sid}.{name}: 网格必须是列表")
            continue
        if not MIN_GRID <= len(grid) <= MAX_GRID:
            errs.append(f"{sid}.{name}: 网格 {len(grid)} 档,须在 {MIN_GRID}~{MAX_GRID} 档")
        if not all(_is_num(v) for v in grid):
            errs.append(f"{sid}.{name}: 网格值必须是数字")
        elif len(set(grid)) != len(grid):
            errs.append(f"{sid}.{name}: 网格有重复值")
        elif grid != sorted(grid):
            errs.append(f"{sid}.{name}: 网格须升序")


def _check_fixed_params(sid, fixed, params, errs):
    """fixed_params:不进网格的固定常数(阈值、窗口、口径开关等)。
    值只能是数字或字符串;键名不得与 params 重名。它整体进清单哈希(改了哈希就变),
    但不参与 grid_size / total_trials,所以不会虚增试验次数,也不会被当成可调参数。"""
    if not isinstance(fixed, dict) or not fixed:
        errs.append(f"{sid}: fixed_params 必须是非空对象(没有固定常数就不要写这个字段)")
        return
    for k, v in fixed.items():
        if not (isinstance(k, str) and k.strip()):
            errs.append(f"{sid}: fixed_params 键必须是非空字符串")
        elif isinstance(params, dict) and k in params:
            errs.append(f"{sid}.{k}: 同时出现在 params 和 fixed_params 里")
        if not (_is_num(v) or (isinstance(v, str) and v.strip())):
            errs.append(f"{sid}.{k}: fixed_params 的值必须是数字或非空字符串")


def _is_real_date(v) -> bool:
    """格式为 YYYY-MM-DD 且是真实存在的日期(挡掉 2024-13-45)。"""
    if not (isinstance(v, str) and _DATE_RE.match(v)):
        return False
    try:
        date.fromisoformat(v)
    except ValueError:
        return False
    return True


def _check_segments(seg, errs):
    """三段日期:训练/验证/留存(holdout,即封存段),须首尾衔接、不重叠。封存段终点为 None(开放)。"""
    try:
        tr, va, se = seg["train"], seg["valid"], seg["holdout"]
    except (TypeError, KeyError):
        errs.append("segments 必须含 train/valid/holdout")
        return
    for nm, pair in (("train", tr), ("valid", va), ("holdout", se)):
        if not (isinstance(pair, list) and len(pair) == 2 and _is_real_date(pair[0])
                and (pair[1] is None or _is_real_date(pair[1]))):
            errs.append(f"segments.{nm} 格式应为 [起, 止] 的 YYYY-MM-DD(封存段止可为 null)")
            return
    if tr[1] is None or va[1] is None:
        errs.append("训练段和验证段必须有终点")
        return
    if not (tr[0] <= tr[1] < va[0] <= va[1] < se[0]):
        errs.append("三段日期必须按 训练<验证<封存 顺序且不重叠")


def check_catalog(cat) -> list:
    """返回问题列表(空列表=合规)。"""
    errs: list = []
    if not isinstance(cat, dict):
        return ["清单顶层必须是对象"]
    items = cat.get("strategies")
    if not isinstance(items, list):
        return ["缺少 strategies 列表"]
    if cat.get("expected_total") != EXPECTED_TOTAL:
        errs.append(f"expected_total 必须为 {EXPECTED_TOTAL}")
    if not _VER_RE.match(str(cat.get("catalog_version", ""))):
        errs.append("catalog_version 缺失或格式不是 主.次")
    _check_segments(cat.get("segments"), errs)
    if len(items) != EXPECTED_TOTAL:
        errs.append(f"策略总数 {len(items)},应为 {EXPECTED_TOTAL}")

    seen: set = set()
    counts: dict = {}
    seeds: list = []
    for i, s in enumerate(items):
        if not isinstance(s, dict):
            errs.append(f"第 {i} 项不是对象")
            continue
        sid = s.get("id", f"#{i}")
        miss = [k for k in _REQUIRED if k not in s]
        if miss:
            errs.append(f"{sid}: 缺字段 {miss}")
        if not isinstance(sid, str) or not _ID_RE.match(sid):
            errs.append(f"{sid}: ID 须为小写字母开头的 [a-z0-9_]")
        if sid in seen:
            errs.append(f"{sid}: ID 重复")
        seen.add(sid)
        for k in ("name", "description"):
            if k in s and not (isinstance(s[k], str) and s[k].strip()):
                errs.append(f"{sid}: {k} 不能为空")
        if "version" in s and not _VER_RE.match(str(s["version"])):
            errs.append(f"{sid}: 版本号格式应为 主.次")
        # 数据可得性声明:必须明确写了"T日收盘"类说明(防止有人清空这个字段)
        pit = s.get("point_in_time")
        if not (isinstance(pit, str) and "T日收盘" in pit):
            errs.append(f"{sid}: 缺少'只用 T 日收盘及以前数据'声明")
        if "params" in s:
            _check_params(sid, s["params"], errs)
        if "fixed_params" in s:
            _check_fixed_params(sid, s["fixed_params"], s.get("params"), errs)
        cat_name = s.get("category")
        counts[cat_name] = counts.get(cat_name, 0) + 1
        is_ctrl = cat_name == CONTROL_CATEGORY
        if is_ctrl != (isinstance(sid, str) and sid.startswith(CONTROL_PREFIX)):
            errs.append(f"{sid}: 随机对照必须且只能用 {CONTROL_PREFIX} 前缀的 ID")
        # 方向写死:非对照一律"越高越靠前",防止看了结果再选正负号(等于隐形多做一次试验)
        want_dir = CONTROL_DIRECTION if is_ctrl else DIRECTION
        if "direction" in s and s["direction"] != want_dir:
            errs.append(f"{sid}: direction 必须为 {want_dir}")
        if is_ctrl:
            seed = s.get("seed")
            if not isinstance(seed, int) or isinstance(seed, bool):
                errs.append(f"{sid}: 随机对照必须有整数固定种子 seed")
            else:
                seeds.append(seed)
        elif "seed" in s:
            errs.append(f"{sid}: 非随机对照不应有 seed")

    if len(set(seeds)) != len(seeds):
        errs.append("随机对照的种子必须互不相同")
    for c, n in EXPECTED_COUNTS.items():
        if counts.get(c, 0) != n:
            errs.append(f"类别 {c}: {counts.get(c, 0)} 个,应为 {n}")
    for c in counts:
        if c not in EXPECTED_COUNTS:
            errs.append(f"未知类别 {c}")
    return errs


def validate_catalog(cat) -> dict:
    """校验通过返回原清单;否则抛 CatalogError(消息列出全部问题)。"""
    errs = check_catalog(cat)
    if errs:
        raise CatalogError(";".join(errs))
    return cat


def catalog_hash(cat) -> str:
    """规范化(键排序、无空白、保留中文)后的 SHA-256 十六进制串。
    清单里任何值的改动都会改变它(包括 fixed_params 里的固定常数);键的书写顺序和缩进不影响它。"""
    blob = json.dumps(cat, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def load_validated(path=None) -> tuple:
    """读取 + 校验 + 求哈希,返回 (清单, 哈希)。试验记录里同时写下哈希。"""
    cat = validate_catalog(load_catalog(path))
    return cat, catalog_hash(cat)


def strategy_ids(cat) -> list:
    return [s["id"] for s in cat["strategies"]]


def grid_size(strategy: dict) -> int:
    """该策略粗网格的组合数(无参数则为 1)。试验次数按组合数计,淘汰者也要计入。
    只看 params;fixed_params 是固定常数,不计入试验次数。"""
    n = 1
    for g in strategy["params"].values():
        n *= len(g)
    return n


def total_trials(cat) -> int:
    """全部候选的网格组合总数,用作多重检验的 m 的下限参考。"""
    return sum(grid_size(s) for s in cat["strategies"])
