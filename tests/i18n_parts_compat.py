"""i18n「部件」（dashboard/ui/i18n/parts/*.json）退役后的测试辅助。

背景：各任务（A02、A03、A07、A09、A11、A15、G01、G03…）当初把新词条先写在 parts/<任务>.<语言>.json，
等所有者合并进基础字典 zh.json / en.json。U5 已经全部合并，运行时只读基础字典（shared/i18n.js），
D39 之后 t-fallback.js 也不再下载 parts/，逐页实测 0 个 parts 请求（overnight/tools/content_parts_runtime_proof.py）。
parts/ 已移到 _to_delete/（content，D 项「parts 清理」）。

测试仍然要按「任务」检查词条，所以把每个部件当时的键名单存成快照 tests/fixtures/i18n_parts_keys.json，
词条的值一律从基础字典取——页面实际显示的就是基础字典里的值。"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "dashboard" / "ui"
KEYS = json.loads((Path(__file__).resolve().parent / "fixtures" / "i18n_parts_keys.json").read_text(encoding="utf-8"))
_BASE = {}


def base(lang: str) -> dict:
    """基础字典（个人站 dashboard/ui/i18n/<lang>.json）"""
    if lang not in _BASE:
        _BASE[lang] = json.loads((UI / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
    return _BASE[lang]


def part_keys(name: str) -> list:
    return list(KEYS[name])


def part(name: str, lang: str, source: dict | None = None) -> dict:
    """某个任务的词条：键来自快照，值来自基础字典（或传入的 source，例如公开站字典）。缺的键直接报 KeyError，让测试失败"""
    src = base(lang) if source is None else source
    return {k: src[k] for k in KEYS[name]}


def missing_in_base(name: str, lang: str) -> list:
    """快照里有、基础字典里没有的键（应当为空）"""
    b = base(lang)
    return [k for k in KEYS[name] if k not in b]
