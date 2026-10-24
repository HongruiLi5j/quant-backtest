"""从想法文本里抽取"事实"（纯函数，不调用任何模型）。

原则：宁可抽不出来，也不要抽错。
- 只在模式明确时产出事实；含糊就不产出，该字段之后在界面上标"无法自动核对"。
- 数字只认阿拉伯数字（含 5%、0.05、1,500）；中文数字（"二十""百分之二十"）本版不支持。
- 同一字段抽到两个不同的值：不产出事实，记入 conflicts。
- 否定语境（不要、不加、without、no、instead of ...）紧邻时：不产出事实，记入 negated。
- 模式只依据系统提示词里的字段清单和设计文档第 7.3 节，不参照任何测试集的想法文本。

返回 Extraction：facts（字段 -> Fact）、conflicts、negated、dates（日期语境里的年份）、relative（"至今"一类相对当前时间的说法）。
"""
import calendar
import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal

# ---------------------------------------------------------------- 数字
_I = r"(?<![\d.])(?<!\d,)((?:\d{1,3}(?:,\d{3})+|\d{1,6}))(?!\d|,\d|\.\d)"      # 整数，允许千分位；不是千分位的 "5,10" 这类写法不抽
_F = r"(?<![\d.])(?<!\d,)((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)(?!\d|,\d)"          # 数字，允许千分位和小数
_D = r"[日天]"
_YEAR = r"((?:19|20)\d\d)"


def _num(s: str, pct: bool = False):
    d = Decimal(s.replace(",", ""))
    if pct:
        d = d / 100
    return int(d) if d == d.to_integral_value() else float(d)


@dataclass(frozen=True)
class Fact:
    field: str
    value: object
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class DateMention:
    year: int
    iso: str | None              # 能唯一确定为一个具体日期时才有（年底、月底、mid-YYYY、完整日期）
    role: str | None             # "end" / "start" / None
    start: int
    end: int
    text: str


@dataclass
class Extraction:
    text: str                                                   # NFKC 规范化后的文本；所有 start/end 都指它
    facts: dict = field(default_factory=dict)
    conflicts: dict = field(default_factory=dict)               # 字段 -> 出现过的不同取值
    negated: set = field(default_factory=set)
    dates: list = field(default_factory=list)
    relative: list = field(default_factory=list)                # [(start, end, text)]

    @property
    def late_dates(self) -> list:
        """日期语境里年份 >= 2024 的提及。"""
        return [d for d in self.dates if d.year >= 2024]

    def value(self, name, default=None):
        f = self.facts.get(name)
        return f.value if f else default


# ---------------------------------------------------------------- 否定语境
_NEG = re.compile(r"(不要|不加|不用|不做|别|没有|不是|而不是|without|\bno\b|\bnot\b|don't|do not|instead of|rather than|excluding|except)", re.I)
_CLAUSE = re.compile(r"[,;。.，；!?！？]")


def _negated(text: str, start: int) -> bool:
    window = _CLAUSE.split(text[max(0, start - 12):start])[-1]      # 只看同一个小句
    return bool(_NEG.search(window))


# ---------------------------------------------------------------- 字段模式
_RULES = []


def _rule(fields, pattern, conv=None, *, neg=True, claim=False, skip=False):
    """fields: 字段名或元组；conv(m) 返回与 fields 等长的取值元组；claim=True 占用匹配区间，skip=True 跳过已被占用的区间。"""
    if isinstance(fields, str):
        fields = (fields,)
    conv = conv or (lambda m: (_num(m.group(1)),))
    _RULES.append((fields, re.compile(pattern, re.I), conv, neg, claim, skip))


def _const(*vals):
    return lambda m: vals


# 预测周期 h
_rule("signal.h", rf"未来\s*{_I}\s*{_D}")
_rule("signal.h", rf"预测目标\s*{_I}\s*{_D}")
_rule("signal.h", rf"{_I}[- ]days?\s+(?:forward|target)\b")
_rule("signal.h", rf"(?<![\w.])(?:signal\.)?h\s*[=:]\s*{_I}(?!\w)(?:\s*(?:trading\s+)?days?\b)?")
_rule("signal.h", rf"预测(?:周期|期)\s*(?:h\s*)?(?:为|=|:)?\s*{_I}\s*{_D}")
_rule("signal.h", rf"(?:prediction\s+|forecast\s+)?horizon\s*(?:of|=|:)?\s*{_I}\s*(?:trading\s+)?days?\b")
# embargo
_rule("signal.embargo", rf"embargo\s*(?:只留|为|=|:)?\s*{_I}\s*(?:天|日|days?)")
_rule("signal.embargo", rf"\bembargo\s*(?:of|=|:)?\s*{_I}\s*(?:天|日|(?:trading\s+)?days?\b)")
_rule("signal.embargo", rf"{_I}[- ]days?\s+embargo")
_rule("signal.embargo", rf"(?<![\w.])(?:signal\.)?embargo\s*[=:]\s*{_I}(?!\w)")
_rule("signal.embargo", r"不留\s*embargo|\bno\s+embargo\b", _const(0), neg=False)
# 训练窗口、重训间隔、alpha
_rule("signal.train_window", rf"训练窗口\s*{_I}\s*{_D}?")
_rule("signal.train_window", rf"{_I}[- ]days?\s+training\s+window")
_rule("signal.train_window", rf"training\s+window\s+of\s+{_I}\s*days?")
_rule("signal.refit_every", rf"每\s*{_I}\s*{_D}\s*重训")
_rule("signal.refit_every", rf"refit\s+every\s+{_I}\s*days?")
_rule("signal.alpha", rf"alpha\s*(?:=|:|为)?\s*{_F}")
# 均线
_rule(("signal.fast", "signal.slow"), rf"{_I}\s*{_D}\s*/\s*{_I}\s*{_D}\s*均线", lambda m: (_num(m.group(1)), _num(m.group(2))))
_rule(("signal.fast", "signal.slow"), rf"快线\s*{_I}\s*{_D}[、,]?\s*慢线\s*{_I}\s*{_D}", lambda m: (_num(m.group(1)), _num(m.group(2))))
_rule(("signal.fast", "signal.slow"), rf"{_I}\s*/\s*{_I}[- ]days?\s+moving[- ]average", lambda m: (_num(m.group(1)), _num(m.group(2))))
_rule(("signal.fast", "signal.slow"), rf"{_I}[- ]days?\s+fast\s+and\s+{_I}[- ]days?\s+slow", lambda m: (_num(m.group(1)), _num(m.group(2))))
# 快慢线分开写的说法（"快10日、慢50日"、"fast 10 ... slow 50"）；与上面成对写法给出同一个值时不算冲突
_rule("signal.fast", rf"快(?:线)?\s*{_I}\s*{_D}")
_rule("signal.slow", rf"慢(?:线)?\s*{_I}\s*{_D}")
_rule("signal.fast", rf"\bfast\s*(?:=|:|of|at)?\s*{_I}(?!\s*%)")
_rule("signal.slow", rf"\bslow\s*(?:=|:|of|at)?\s*{_I}(?!\s*%)")
# 动量的回看与跳过
_rule("signal.lookback", rf"回看\s*{_I}\s*{_D}")
_rule("signal.lookback", rf"lookback\s*(?:of\s+)?{_I}")
_rule("signal.lookback", rf"{_I}[- ]days?\s+lookback")
_rule("signal.skip", rf"跳过(?:最近)?\s*{_I}\s*{_D}")
_rule("signal.skip", rf"skip(?:ping)?(?:\s+the\s+last)?\s+{_I}")
# 反转的天数：对 residual_reversal 是 L，对 reversal_raw 是 lookback，由 consistency 按配置里的类型决定
_rule("reversal.N", rf"{_I}\s*{_D}(?:简单)?反转")
_rule("reversal.N", rf"做\s*{_I}\s*{_D}均值回归")
_rule("reversal.N", rf"{_I}[- ]days?\s+(?:[a-z-]+\s+){{0,3}}reversal")
_rule("reversal.N", rf"mean\s+reversion\s+of\b.{{0,60}}?\bover\s+{_I}\s+days?")
# 迟滞带（先于 q，并占用区间，免得 "top/bottom 10%" 被当成 q）
_rule("portfolio.band_entry", rf"前\s*/\s*后\s*{_F}\s*%\s*进场", lambda m: (_num(m.group(1), True),), claim=True)
_rule("portfolio.band_exit", rf"掉出前\s*/\s*后\s*{_F}\s*%\s*才离场", lambda m: (_num(m.group(1), True),), claim=True)
_rule("portfolio.band_entry", rf"enter\s+the\s+top\s*/\s*bottom\s*{_F}\s*%", lambda m: (_num(m.group(1), True),), claim=True)
_rule("portfolio.band_exit", rf"exit\s+only\s+beyond\s*{_F}\s*%", lambda m: (_num(m.group(1), True),), claim=True)
# q
_rule("portfolio.q", rf"前后\s*{_F}\s*%", lambda m: (_num(m.group(1), True),), skip=True)
_rule("portfolio.q", rf"top\s*(?:/|and)\s*bottom\s*{_F}\s*%", lambda m: (_num(m.group(1), True),), skip=True)
_rule("portfolio.q", r"十分位|\bdecile\b", _const(0.1), skip=True)
# 单边写法（"持有前20%"、"top 20%"）；"前后"、"前/后"、"top/bottom" 已在上面处理，"之前10%"一类不算
_rule("portfolio.q", rf"(?<![之以])前\s*{_F}\s*%", lambda m: (_num(m.group(1), True),), skip=True)
_rule("portfolio.q", rf"\btop\s+{_F}\s*%", lambda m: (_num(m.group(1), True),), skip=True)
# 调仓频率：必须紧挨"调仓 / rebalance"，免得把别处的"每日""weekly"当成频率
_rule("portfolio.rebalance_every", r"(?:每日|日频|每天)\s*调仓", _const(1))
_rule("portfolio.rebalance_every", r"(?:每周|周频)(?:\s*[(（]\s*每\s*5\s*[日天]\s*[)）])?\s*调仓", _const(5))
_rule("portfolio.rebalance_every", r"(?:每月|月频)(?:\s*[(（]\s*每\s*21\s*[日天]\s*[)）])?\s*调仓", _const(21))
_rule("portfolio.rebalance_every", rf"每\s*{_I}\s*{_D}\s*调仓")
_rule("portfolio.rebalance_every", r"rebalanc\w*\s+daily|daily(?:\s*\(every\s*1\s*days?\))?\s+rebalanc", _const(1))
_rule("portfolio.rebalance_every", r"rebalanc\w*\s+weekly|weekly(?:\s*\(every\s*5\s*days?\))?\s+rebalanc", _const(5))
_rule("portfolio.rebalance_every", r"rebalanc\w*\s+monthly|monthly(?:\s*\(every\s*21\s*days?\))?\s+rebalanc", _const(21))
_rule("portfolio.rebalance_every", rf"rebalanc\w*\s+every\s+{_I}\s+days?")
# 成本：单位必须是 bp / 基点
_rule("market.cost_bp", rf"{_F}\s*(?:bps?\b|basis\s+points?|个?基点)")
# 股票子集：较低的一半必须先于较高的一半（"less liquid half" 里含 "liquid half"）
_rule("universe.subset", r"成交额较低的一半|less\s+liquid\s+half|illiquid\s+half", _const("illiquid_half"), claim=True)
_rule("universe.subset", r"成交额较高的一半|liquid\s+half", _const("liquid_half"), skip=True)
_rule("universe.subset", r"防御型|\bdefensive\b", _const("defensive"))
_rule("universe.subset", r"周期型|\bcyclical\b", _const("cyclical"))
# 信号类型：单独的"反转 / reversal"不产出事实（raw 与 residual 容易混）
_rule("signal.type", r"动量|\bmomentum\b", _const("momentum"))
_rule("signal.type", r"均线|moving[- ]average", _const("ma_cross"))
_rule("signal.type", r"等权|equal[- ]weight", _const("equal_weight"))
_rule("signal.type", r"残差.{0,12}?(?:反转|均值回归)", _const("residual_reversal"))
_rule("signal.type", r"(?:reversal|mean[- ]reversion)\b.{0,60}?\bresiduals?\b|\bresiduals?\b.{0,40}?(?:reversal|mean[- ]reversion)", _const("residual_reversal"))
_rule(("signal.type", "signal.model"), r"岭回归|\bridge\b", _const("ml", "ridge"))
_rule(("signal.type", "signal.model"), r"梯度提升|\bhgb\b|gradient\s+boosting", _const("ml", "hgb"))
_rule(("signal.type", "signal.model"), r"神经网络|\bmlp\b", _const("ml", "mlp"))
_rule("signal.features", r"基础\s*8\s*特征|basic\s*8\b", _const("v1"))
_rule("signal.features", r"扩展\s*21\s*特征|extended\s*21\b", _const("v2"))


def _overlaps(spans, a, b):
    return any(a < e and s < b for s, e in spans)


def _same(a, b):
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) and not isinstance(b, bool):
        return abs(float(a) - float(b)) < 1e-9
    return a == b


# ---------------------------------------------------------------- 日期语境
_MONTH = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
# 年份后面紧跟这些，说明是个数量而不是年份（"2048 日训练窗口"）
_NOT_YEAR_AFTER = r"(?!\s*(?:日|天|days?\b|d\b|bp|个|只|股|%|x\b))"
_DATE_PATTERNS = [
    # (正则, 构造函数(match) -> (year, iso))
    (re.compile(rf"(?<![\d-]){_YEAR}-(\d{{1,2}})-(\d{{1,2}})(?!\d)"), lambda m: (int(m[1]), _iso(m[1], m[2], m[3]))),
    (re.compile(rf"{_YEAR}\s*年\s*(\d{{1,2}})\s*月\s*(\d{{1,2}})\s*[日号]"), lambda m: (int(m[1]), _iso(m[1], m[2], m[3]))),
    (re.compile(rf"{_YEAR}\s*年\s*(\d{{1,2}})\s*月\s*(底|末)"), lambda m: (int(m[1]), _month_end(m[1], m[2]))),
    (re.compile(rf"{_YEAR}\s*年\s*(\d{{1,2}})\s*月"), lambda m: (int(m[1]), None)),
    (re.compile(rf"{_YEAR}\s*年\s*(?:年)?(?:底|末)"), lambda m: (int(m[1]), f"{m[1]}-12-31")),
    (re.compile(rf"{_YEAR}\s*年"), lambda m: (int(m[1]), None)),
    (re.compile(rf"(?<![\d/]){_YEAR}/(\d{{1,2}})(?:/(\d{{1,2}}))?(?!\d)"), lambda m: (int(m[1]), _iso(m[1], m[2], m[3]) if m[3] else None)),
    (re.compile(rf"end\s+of\s+(?:the\s+year\s+)?{_YEAR}", re.I), lambda m: (int(m[1]), f"{m[1]}-12-31")),
    (re.compile(rf"mid[- ]?{_YEAR}", re.I), lambda m: (int(m[1]), f"{m[1]}-06-30")),
    (re.compile(rf"\b{_MONTH}\b\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+{_YEAR}", re.I), lambda m: (int(m[2]), None)),
    (re.compile(rf"\b{_MONTH}\b\.?\s+{_YEAR}(?!\d)", re.I), lambda m: (int(m[2]), None)),
    (re.compile(rf"\b(?:in|through|until|till|by|since|from|to|during|before|after)\s+(?:the\s+)?{_YEAR}(?!\d){_NOT_YEAR_AFTER}", re.I), lambda m: (int(m[1]), None)),
    (re.compile(rf"(?:到|至|截至|截止到?|直到)\s*{_YEAR}(?!\d){_NOT_YEAR_AFTER}"), lambda m: (int(m[1]), None)),
]
_END_CUE = re.compile(r"(评估到|评估至|回测到|回测至|截至|截止|直到|到|至|结束于|结束日|through|until|till|up to|\bto\b|ending|end on|by)", re.I)
_START_CUE = re.compile(r"(从|自|起始|开始于|starting|\bstart\b|\bfrom\b|since|begin(?:ning)?)", re.I)

# 相对当前时间的说法：评估区间必然进入封存期。"up to date" 不算（那是"保持最新"的意思）
_RELATIVE = [re.compile(p, re.I) for p in (
    r"至今|迄今|到现在|到目前|截至目前|截至今天|截止今天|截止到今天|到今天|最新(?:的)?数据",
    r"(?<!up )\bto date\b|up to now|until now|until today|through today|till now|till today|up to the present|to the present|(?:the )?latest data|most recent data",
)]


def _iso(y, m, d):
    try:
        calendar.monthrange(int(y), int(m))
        if not 1 <= int(d) <= calendar.monthrange(int(y), int(m))[1]:
            return None
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
    except (ValueError, calendar.IllegalMonthError):
        return None


def _month_end(y, m):
    try:
        return f"{int(y):04d}-{int(m):02d}-{calendar.monthrange(int(y), int(m))[1]:02d}"
    except (ValueError, calendar.IllegalMonthError):
        return None


def _role(text: str, start: int, end: int):
    """日期在句子里是评估结束日还是起点：看前面最近的提示词；没有提示词就看后面是不是"开始 / 起"。"""
    before = _CLAUSE.split(text[max(0, start - 16):start])[-1]
    e = [m.end() for m in _END_CUE.finditer(before)]
    s = [m.end() for m in _START_CUE.finditer(before)]
    if e or s:
        return "end" if (max(e) if e else -1) > (max(s) if s else -1) else "start"
    if re.match(r"\s*(?:开始|起)", text[end:end + 6]):
        return "start"
    return None


def _dates(text: str) -> list:
    out, claimed = [], []
    for pat, build in _DATE_PATTERNS:
        for m in pat.finditer(text):
            if _overlaps(claimed, m.start(), m.end()):
                continue
            year, iso = build(m)
            claimed.append((m.start(), m.end()))
            out.append(DateMention(year, iso, _role(text, m.start(), m.end()), m.start(), m.end(), m.group(0)))
    return sorted(out, key=lambda d: d.start)


# ---------------------------------------------------------------- 入口
def extract(idea: str) -> Extraction:
    text = unicodedata.normalize("NFKC", idea if isinstance(idea, str) else "")
    ex = Extraction(text=text)
    claimed, cands = [], {}
    for fields, pat, conv, neg, claim, skip in _RULES:
        for m in pat.finditer(text):
            if skip and _overlaps(claimed, m.start(), m.end()):
                continue
            if claim:
                claimed.append((m.start(), m.end()))
            if neg and _negated(text, m.start()):
                ex.negated.update(fields)
                continue
            for f, v in zip(fields, conv(m)):
                cands.setdefault(f, []).append(Fact(f, v, m.start(), m.end(), m.group(0)))
    ex.dates = _dates(text)
    for d in ex.dates:                                   # 评估结束日：只认能唯一确定的具体日期，且前面有"到 / through"一类提示
        if d.role == "end" and d.iso:
            cands.setdefault("period.end", []).append(Fact("period.end", d.iso, d.start, d.end, d.text))
    for f, facts in cands.items():
        distinct = []
        for ft in facts:
            if not any(_same(ft.value, v) for v in distinct):
                distinct.append(ft.value)
        if len(distinct) > 1:
            ex.conflicts[f] = distinct                   # 不产出事实
        else:
            ex.facts[f] = facts[0]
    for pat in _RELATIVE:
        ex.relative += [(m.start(), m.end(), m.group(0)) for m in pat.finditer(text)]
    ex.relative.sort()
    return ex
