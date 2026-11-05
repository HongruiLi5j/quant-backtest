"""AI 翻译 S1–S4 的检查：python tests/test_ai_translator.py（只用标准库，不加载任何模型）

覆盖：配置加载与回退（设计文档 8.3 的 6 号中与配置相关的部分、7 号）、导入隔离（33 号）、防护栏（8.2）、
后端接口约定、红线字符串、S2 纯逻辑与闸门、S3 假 worker 进程监督。被测对象用 tempfile 临时目录。
"""
import os

os.environ["QB_TESTING"] = "1"      # 必须在导入被测模块之前：打开防护栏

import ast  # noqa: E402
import contextlib  # noqa: E402
import ctypes  # noqa: E402
import datetime  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import select  # noqa: E402
import signal  # noqa: E402
import stat  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import unittest  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest import mock  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from src.ai_translator import api, api_llm, backend, config, consistency, extract, gate, local_lora, logstore, paths, plainview, precheck, registry  # noqa: E402
from env_guard import STRICT, skip_unless  # noqa: E402  缺外部目录时跳过（QB_REQUIRE_FULL_ENV=1 则不跳过）
_S12_ONLY = __name__ == "__main__" and sys.argv[1:] == ["--s12-only"]
_S13_NO_SOCKET = __name__ == "__main__" and sys.argv[1:] == ["--s13-no-socket"]
_S13_ONLY = __name__ == "__main__" and (sys.argv[1:] == ["--s13-only"] or _S13_NO_SOCKET)
_ISOLATED = _S12_ONLY or _S13_ONLY
if not _ISOLATED:
    from realdata_guard import ZRealDataUntouchedTests  # noqa: E402,F401  真实数据守卫

PKG = REPO / "src" / "ai_translator"
_tick = [0]


def external_fingerprint(ai=None, research=None):
    """补足本阶段外部守卫：只计算指定文件字节哈希，不解析任何登记数据。"""
    ai = Path(ai) if ai is not None else REPO.parent / "ai_layer"
    research = Path(research) if research is not None else REPO.parent / "regime-statarb"
    files = [ai / "runs.jsonl", ai / "MODEL_REGISTRY.json", research / "registry" / "trials.jsonl"]
    files += sorted((ai / "adapters" / "run2_best").glob("*"))
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None for p in files}


EXTERNAL_BEFORE = None if _ISOLATED else external_fingerprint()


class Rec:
    """记录审计调用，签名同 admin.audit。"""

    def __init__(self):
        self.calls = []

    def __call__(self, action, detail=None, ok=True, error=None):
        self.calls.append((action, detail, ok))

    def actions(self):
        return [c[0] for c in self.calls]


def write(p: Path, content):
    """写文件，并让 mtime 每次都不同（避免同一时刻改动骗过 stat 缓存）。"""
    if isinstance(content, (dict, list)):
        content = json.dumps(content)
    p.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    _tick[0] += 1
    t = 1_700_000_000_000_000_000 + _tick[0] * 10_000_000_000
    os.utime(p, ns=(t, t))


def make(content=None):
    root = Path(tempfile.mkdtemp(prefix="qb_ai_"))
    (root / "data").mkdir()
    p = root / "data" / "ai_translator.json"
    if content is not None:
        write(p, content)
    rec = Rec()
    return config.ConfigLoader(root, rec), rec, p, root


GOOD = {"schema": 1, "enabled": True, "backend": "local_lora"}


# ---------------------------------------------------------------- 配置加载
def test_missing_file_is_disabled():
    loader, rec, p, _ = make()
    st = loader.load()
    assert not st.enabled and st.backend == "disabled" and st.problem is None and not st.exists
    assert st.cfg == config.defaults()
    assert rec.calls == []                      # 文件不存在是正常情况，不审计
    assert not p.exists()                       # 加载不创建文件


def test_valid_config_and_defaults():
    loader, rec, _, _ = make(GOOD)
    st = loader.load()
    assert st.enabled and st.backend == "local_lora" and st.problem is None and st.exists
    assert st.cfg["timeouts"]["idle_s"] == 120          # 本版默认：空闲回收 120 秒
    assert st.cfg["log"]["retention_days"] == 90        # 本版默认：日志保留 90 天
    assert st.cfg["ai_layer_dir"] is None               # 不写死外部项目位置
    assert st.cfg["limits"] == {"max_input_chars": 500, "daily_limit": 200}
    assert "shadow" not in st.cfg                       # 在线影子模式本版不做
    assert rec.calls == []                              # 第一次加载没有“改动前”状态，不审计开关


def test_enabled_must_be_strict_true():
    for bad in ("true", 1, "yes", None, [True]):
        loader, rec, _, _ = make({"enabled": bad, "backend": "local_lora"})
        st = loader.load()
        assert not st.enabled and st.problem == "CONFIG_INVALID", bad
    loader, _, _, _ = make({"enabled": False, "backend": "local_lora"})
    assert not loader.load().enabled


def test_enabled_with_disabled_backend_is_off():
    loader, _, _, _ = make({"enabled": True, "backend": "disabled"})
    st = loader.load()
    assert not st.enabled and st.problem is None


CORRUPT = [
    ("syntax", "{not json"),
    ("syntax", '{"limits": {"max_input_chars": NaN}}'),
    ("syntax", '{"enabled": true, "enabled": false}'),
    ("syntax", b"\xff\xfe\x00bad"),
    ("too_large", "{" + " " * 70000 + "}"),
    ("type", "[1, 2]"),
    ("type", '"enabled"'),
    ("type", {"limits": 5}),
    ("type", {"limits": {"max_input_chars": 1.5}}),
    ("type", {"timeouts": {"request_s": True}}),
    ("type", {"timeouts": {"request_s": "60"}}),
    ("unknown_backend", {"backend": "gpt"}),
    ("unknown_backend", {"backend": "fake"}),            # 下面单独测：QB_TESTING=1 时 fake 合法，生产里非法
    ("range", {"timeouts": {"request_s": -1}}),
    ("range", {"timeouts": {"request_s": 0}}),
    ("range", {"timeouts": {"load_s": 99999}}),
    ("range", {"limits": {"max_input_chars": 4001}}),
    ("range", {"limits": {"max_input_chars": 0}}),
    ("range", {"log": {"retention_days": 0}}),
    ("secret_field", {"api_key": "SUPERSECRET"}),
    ("secret_field", {"limits": {"token": 1}}),
    ("secret_field", {"api_llm": {"password": "x"}}),
    ("secret_field", {"api_llm": {"api_key_env": "sk-SUPERSECRET"}}),
    ("secret_field", {"api_llm": {"api_key_env": "lowercase_name"}}),
    ("secret_field", {"api_llm": {"base_url": "https://u:SUPERSECRET@host/v1"}}),
    ("unknown_key", {"shadow": {"enabled": False}}),
    ("unknown_key", {"limits": {"extra": 1}}),
    ("schema", {"schema": 2}),
    ("schema", {"schema": True}),
]


def test_corrupt_configs_fall_back_without_raising():
    """设计文档 8.3 的 7 号：不抛异常，enabled=False，problem=CONFIG_INVALID，审计一次且不含文件内容。"""
    for reason, content in CORRUPT:
        loader, rec, _, _ = make(content)
        with mock.patch.dict(os.environ):
            if content == {"backend": "fake"}:
                os.environ.pop("QB_TESTING")               # 生产环境里出现 fake 一律判损坏
            st = loader.load()
            assert not st.enabled and st.backend == "disabled", content
            assert st.problem == "CONFIG_INVALID" and st.reason == reason, (content, st.reason)
            assert rec.actions() == ["ai_translator_config_invalid"], content
            assert rec.calls[0][1] == {"reason": reason} and rec.calls[0][2] is False
            assert "SUPERSECRET" not in repr(rec.calls)
            loader.load()
            loader.load()
            assert len(rec.calls) == 1, "内容没变不应重复审计"


def test_fake_backend_allowed_only_in_testing():
    loader, _, _, _ = make({"enabled": True, "backend": "fake"})
    st = loader.load()
    assert st.enabled and st.backend == "fake"


def test_unreadable_path_does_not_raise():
    loader, rec, p, _ = make()
    p.mkdir()                                            # 配置路径是个目录：读取失败
    st = loader.load()
    assert not st.enabled and st.problem == "CONFIG_INVALID" and st.reason == "unreadable"
    assert rec.actions() == ["ai_translator_config_invalid"]


def test_invalid_audited_again_only_when_content_changes():
    loader, rec, p, _ = make("{bad")
    loader.load()
    write(p, "{bad2")
    loader.load()
    loader.load()
    assert rec.actions() == ["ai_translator_config_invalid", "ai_translator_config_invalid", "ai_translator_config_changed"]
    write(p, GOOD)
    assert loader.load().enabled
    write(p, "{bad")                                     # 修好之后再坏，重新审计
    loader.load()
    assert rec.actions().count("ai_translator_config_invalid") == 3


def test_reload_on_change_and_audit():
    loader, rec, p, _ = make({"enabled": False, "backend": "local_lora"})
    assert not loader.load().enabled
    write(p, GOOD)
    assert loader.load().enabled
    assert rec.actions() == ["ai_translator_config_changed", "ai_translator_toggle"]
    assert rec.calls[0][1] == {"via": "file"} and rec.calls[1][1] == {"enabled": True, "via": "file"}
    write(p, "{bad")                                     # 开着的时候配置坏了：回退关闭，也要留下开关审计
    st = loader.load()
    assert not st.enabled and st.problem == "CONFIG_INVALID"
    assert rec.actions()[2:] == ["ai_translator_config_invalid", "ai_translator_config_changed", "ai_translator_toggle"]
    assert rec.calls[-1][1] == {"enabled": False, "via": "file"}


def test_load_only_stats_when_unchanged():
    loader, _, p, _ = make(GOOD)
    n = []
    orig = loader._read
    loader._read = lambda: (n.append(1), orig())[1]
    loader.load()
    loader.load()
    loader.load()
    assert len(n) == 1
    write(p, {**GOOD, "enabled": False})
    loader.load()
    assert len(n) == 2
    p.unlink()
    assert not loader.load().exists and len(n) == 3
    loader.load()
    assert len(n) == 3                                   # “文件不存在”也被缓存


# ---------------------------------------------------------------- 原子写
def test_save_is_atomic_and_private():
    loader, rec, p, root = make()
    data_dir = root / "data"
    st = loader.save({"enabled": True, "backend": "local_lora"}, via="admin")
    assert st.enabled and p.exists()
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert on_disk["enabled"] is True and on_disk["timeouts"]["idle_s"] == 120     # 补全了默认值
    assert [f.name for f in data_dir.iterdir()] == ["ai_translator.json"], "不应残留临时文件"
    assert rec.calls == [("ai_translator_toggle", {"enabled": True, "via": "admin"}, True)]
    loader.load()
    assert len(rec.calls) == 1, "自己写的不应当作来源不明的改动"
    loader.save({"enabled": False, "backend": "local_lora"}, via="admin")
    assert rec.calls[-1][1] == {"enabled": False, "via": "admin"}


def test_save_creates_missing_data_dir():
    root = Path(tempfile.mkdtemp(prefix="qb_ai_"))
    loader = config.ConfigLoader(root)
    loader.save({"enabled": False})
    assert (root / "data" / "ai_translator.json").exists()


def test_save_rejects_bad_input_and_keeps_old_file():
    loader, _, p, root = make(GOOD)
    before = p.read_bytes()
    for bad in ({"backend": "gpt"}, {"api_key": "x"}, {"timeouts": {"request_s": -1}}, "x"):
        try:
            loader.save(bad)
        except config.ConfigError:
            pass
        else:
            raise AssertionError(bad)
    assert p.read_bytes() == before
    assert [f.name for f in (root / "data").iterdir()] == ["ai_translator.json"]


def test_save_failure_cleans_temp_and_keeps_old_file():
    loader, _, p, root = make(GOOD)
    before = p.read_bytes()
    with mock.patch("src.ai_translator.config.os.replace", side_effect=OSError("boom")):
        try:
            loader.save({"enabled": False, "backend": "local_lora"})
        except OSError:
            pass
        else:
            raise AssertionError("应抛出 OSError")
    assert p.read_bytes() == before
    assert [f.name for f in (root / "data").iterdir()] == ["ai_translator.json"]


def test_defaults_are_valid_and_secret_free():
    assert config.validate(config.defaults()) == config.defaults()
    names = []

    def walk(n):
        if isinstance(n, dict):
            for k, v in n.items():
                names.append(k)
                walk(v)
    walk(config.DEFAULTS)
    bad = [k for k in names if any(w in k.lower() for w in config.SECRET_WORDS)]
    assert bad == ["api_key_env"]


# ---------------------------------------------------------------- 开关关闭：不启动任何进程（6 号中配置相关的部分）
def test_disabled_never_starts_a_process():
    # 环境隔离：本测试假设没配 API 密钥；运行环境里若设了 AI_TRANSLATOR_API_KEY，在本测试内临时去掉（退出后原样恢复）
    with mock.patch.dict(os.environ), mock.patch("subprocess.Popen", side_effect=AssertionError("不应启动进程")) as popen:
        os.environ.pop("AI_TRANSLATOR_API_KEY", None)
        for content in (None, {"enabled": False}, {"enabled": True, "backend": "api_llm"}, "{bad"):
            loader, _, _, _ = make(content)
            st = loader.load()
            if st.backend == "api_llm":
                b = api_llm.ApiLlmBackend(st.cfg, root=Path(tempfile.mkdtemp()))
                assert b.status()["problem"] == "TRANSLATOR_UNAVAILABLE"
                b.close()
            else:
                assert not st.enabled
        assert popen.call_count == 0


# ---------------------------------------------------------------- 后端接口约定
def test_api_llm_unconfigured_is_unavailable():
    b = api_llm.ApiLlmBackend(root=Path(tempfile.mkdtemp()))
    assert isinstance(b, backend.TranslatorBackend)
    with mock.patch.dict(os.environ):
        os.environ.pop("AI_TRANSLATOR_API_KEY", None)
        assert b.status()["state"] == "unavailable"
        assert b.translate("x")["refusal"]["code"] == "TRANSLATOR_UNAVAILABLE"
    b.close()
    b.close()


def test_backend_is_abstract():
    try:
        backend.TranslatorBackend()
    except TypeError:
        pass
    else:
        raise AssertionError("抽象类不应能直接实例化")


def test_make_result_contract():
    ok = backend.make_result(config={"a": 1}, parse_mode="strict", backend_id="fake:v", model_version="v", latency_ms=12.7)
    assert ok["refusal"] is None and ok["config"] == {"a": 1} and ok["latency_ms"] == 12
    assert set(ok) == {"config", "refusal", "raw_output", "parse_mode", "backend_id", "model_version", "latency_ms"}
    bad_calls = [
        dict(),                                                           # 两个都空
        dict(config={}, refusal={"code": "X"}),                           # 两个都有
        dict(config=[1]),
        dict(refusal={"nocode": 1}),
        dict(config={}, parse_mode="lenient"),                            # 本版没有宽松解析
    ]
    for kw in bad_calls:
        try:
            backend.make_result(backend_id="b", model_version="v", **kw)
        except ValueError:
            pass
        else:
            raise AssertionError(kw)
    r = backend.make_result(refusal={"code": "TRANSLATOR_TIMEOUT"}, backend_id="b", model_version="v")
    assert r["config"] is None and r["parse_mode"] == "none"


def test_error_codes():
    assert backend.ERROR_HTTP["TRANSLATOR_TIMEOUT"] == 504 and backend.ERROR_HTTP["TRANSLATOR_BUSY"] == 429
    assert backend.ERROR_HTTP["TRANSLATOR_REQUEST_EXPIRED"] == 410 and backend.ERROR_HTTP["TRANSLATOR_DISABLED"] == 503
    for code in backend.ERROR_HTTP:
        assert code.startswith("TRANSLATOR_")
        e = backend.TranslatorError(code)
        assert str(e) == code and e.http_status == backend.ERROR_HTTP[code]       # 只有码，不带细节
    try:
        backend.TranslatorError("NOPE")
    except ValueError:
        pass
    else:
        raise AssertionError("未知码应被拒绝")


# ---------------------------------------------------------------- 防护栏（8.2）
def test_tripwire_blocks_real_locations():
    assert os.environ["QB_TESTING"] == "1"
    real_ai = paths.ROOT.parent / "ai_layer"
    for fn in (lambda: paths.data_dir(), lambda: paths.config_path(), lambda: paths.state_dir(),
               lambda: paths.lock_path(), lambda: paths.log_path("202610"), lambda: config.ConfigLoader(),
               lambda: config.ConfigLoader(paths.ROOT), lambda: paths.ai_layer_dir(str(real_ai)),
               lambda: paths.guard(real_ai / "adapters" / "x")):
        try:
            fn()
        except paths.TripwireError:
            pass
        else:
            raise AssertionError("防护栏没有拦住")
    tmp = Path(tempfile.mkdtemp(prefix="qb_ai_"))
    assert paths.config_path(tmp) == (tmp / "data" / "ai_translator.json").resolve()
    assert paths.ai_layer_dir(str(tmp)) == tmp.resolve()
    assert paths.ai_layer_dir(None) is None
    with mock.patch.dict(os.environ):
        os.environ.pop("QB_TESTING")                       # 非测试模式只算路径，不拦（这里不碰磁盘）
        assert paths.data_dir() == (paths.ROOT / "data").resolve()


def test_paths_reject_odd_month():
    tmp = Path(tempfile.mkdtemp(prefix="qb_ai_"))
    for bad in ("2026", "../../x", "20261x", 202610, ""):
        try:
            paths.log_path(bad, tmp)
        except ValueError:
            pass
        else:
            raise AssertionError(bad)
    assert paths.log_path("202610", tmp).name == "log_202610.jsonl"


# ---------------------------------------------------------------- 导入隔离（33 号）
def test_import_isolation():
    code = (
        "import sys, json\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "import src.ai_translator\n"
        "import src.ai_translator.paths, src.ai_translator.config, src.ai_translator.backend, src.ai_translator.api_llm\n"
        "import src.ai_translator.extract, src.ai_translator.precheck, src.ai_translator.consistency, src.ai_translator.plainview, src.ai_translator.gate\n"
        "import src.ai_translator.local_lora\n"
        "import src.ai_translator.api, src.ai_translator.logstore\n"
        "bad = [m for m in ('mlx', 'mlx_lm', 'ai_lib', 'statarb') if any(k == m or k.startswith(m + '.') for k in sys.modules)]\n"
        "print(json.dumps(bad))\n"
    )
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == [], out.stdout


def test_package_only_imports_stdlib():
    allowed = {"abc", "atexit", "calendar", "copy", "ctypes", "dataclasses", "datetime", "decimal", "fcntl", "hashlib",
               "ipaddress", "json", "math", "os", "pathlib", "re", "select", "signal", "subprocess", "tempfile", "threading", "time",
               "unicodedata", "weakref", "contextlib", "urllib", "socket", "sys"}
    for f in sorted(PKG.glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = set() if node.level else {(node.module or "").split(".")[0]}
            else:
                continue
            assert names <= allowed, (f.name, names - allowed)


# ---------------------------------------------------------------- 红线字符串
def test_red_line_strings_not_in_package():
    banned = ["seal" + "ed", "test" + "2", "." + "secret", "trials" + ".jsonl", "regis" + "try/"]
    for f in sorted(PKG.glob("*.py")):
        text = f.read_text(encoding="utf-8").replace("SEALED_PERIOD", "").lower()       # 拒绝码本身（校验器的枚举值）不算
        for s in banned:
            assert s not in text, (f.name, s)
        assert not re.search(r"/users/|/home/|documents/projects", text), f"{f.name} 不应写死绝对路径"
        assert not re.search(r"\bsk-[a-z0-9]", text), f"{f.name} 看起来含密钥"
    assert (PKG / "api_llm.py").exists()


# ================================================================ S2：纯逻辑与闸门
class Skip(unittest.SkipTest):
    """依赖的外部项目（ai_layer、校验器所在项目）不在本机，跳过并在汇总里如实报告。
    继承 unittest.SkipTest：pytest 把它记成“跳过”而不是“失败”；脚本方式运行时由底部的 except 捕获。"""


_AI_DIR = Path(os.environ.get("QB_AI_LAYER_DIR") or REPO.parent / "ai_layer")
_STATARB_SRC = Path(os.environ.get("QB_STATARB_SRC") or REPO.parent / "regime-statarb" / "src")
HAVE_STATARB_SRC = (_STATARB_SRC / "statarb" / "schema.py").exists()
HAVE_EXTERNAL = (_AI_DIR / ".venv" / "bin" / "python").exists() and HAVE_STATARB_SRC

# 跳过条件只看目录/文件是否存在；原因写明缺什么。东西在而测试失败仍是失败。
requires_external = skip_unless(
    HAVE_EXTERNAL, f"缺外部项目：需要 {_AI_DIR}/.venv/bin/python（ai_layer）和 {_STATARB_SRC}/statarb/schema.py（校验器源码）；"
                   "可用 QB_AI_LAYER_DIR / QB_STATARB_SRC 指向它们")
requires_statarb_src = skip_unless(
    HAVE_STATARB_SRC, f"缺校验器源码 {_STATARB_SRC}/statarb/schema.py（regime-statarb 项目）；可用 QB_STATARB_SRC 指向它")


def _external():
    ai, src = _AI_DIR, _STATARB_SRC
    py = ai / ".venv" / "bin" / "python"
    if not HAVE_EXTERNAL:
        if STRICT:      # 严格模式：缺环境不许悄悄跳过
            raise AssertionError("QB_REQUIRE_FULL_ENV=1，但找不到 ai_layer 的 python 或校验器源码目录")
        raise Skip("找不到 ai_layer 的 python 或校验器源码目录")
    return ai, py, src


_gate_cache = []


def real_gate():
    if not _gate_cache:
        _, py, src = _external()
        _gate_cache.append(gate.Gate(py, src))
    return _gate_cache[0]


class AlwaysLegal:
    """单元测试用的假 oracle：只测规则本身，不依赖真实校验器。"""

    def __init__(self):
        self.calls = []

    def validate(self, doc):
        self.calls.append(doc)
        return {"legal": True, "errors": [], "code": None}


def cfg_of(signal, every=None, q=None, subset="all", band=None, cost=5, start="auto", end="2023-12-31"):
    return {"schema_version": "1.0", "universe": {"subset": subset}, "signal": signal,
            "portfolio": {"q": q, "rebalance_every": every, "band_entry": band[0] if band else None, "band_exit": band[1] if band else None},
            "market": {"name": "us", "cost_bp": cost}, "period": {"start": start, "end": end},
            "stats": {"bootstrap_reps": 2000, "block": 21, "seed": 0}}


def eq_cfg(**kw):
    return cfg_of({"type": "equal_weight"}, **kw)


def mom_cfg(**kw):
    return cfg_of({"type": "momentum", "lookback": 252, "skip": 21}, every=1, q=0.1, **kw)


def resid_cfg(**kw):
    return cfg_of({"type": "residual_reversal", "residual": "market", "L": 5}, every=5, q=0.1, **kw)


def ml_cfg(h=5, embargo=None, **kw):
    sig = {"type": "ml", "model": "ridge", "features": "v1", "h": h, "train_window": 756, "refit_every": 63,
           "embargo": h + 1 if embargo is None else embargo, "alpha": 100.0}
    return cfg_of(sig, every=h, q=0.1, **kw)


def facts_of(idea):
    return {k: v.value for k, v in extract.extract(idea).facts.items()}


# ---------------------------------------------------------------- 测试 2、3、5：闸门
@requires_external
def test_gate_accepts_legal_documents():
    g = real_gate()
    before = g.calls
    for cfg in (eq_cfg(), mom_cfg(), resid_cfg(), ml_cfg(), resid_cfg(band=(0.1, 0.3)), eq_cfg(cost=0.5)):
        r = g.validate(cfg)
        assert r == {"legal": True, "errors": [], "code": None}, (cfg["signal"], r)
    rej = {"schema_version": "1.0", "reject": {"code": "SEALED_PERIOD", "message": "no"}}
    assert g.validate(rej)["legal"]                                   # 拒绝文档本身也是合法文档
    assert g.calls == before + 7


@requires_external
def test_gate_rejects_with_validator_codes():
    g = real_gate()
    bad_signal = eq_cfg()
    bad_signal["signal"] = {"type": "made_up"}
    no_stats = eq_cfg()
    del no_stats["stats"]
    cases = [
        (eq_cfg(end="2024-06-30"), "SEALED_PERIOD", "period.end"),
        (eq_cfg(start="2024-02-01"), "SEALED_PERIOD", "period.start"),
        (eq_cfg(cost=7), "OUT_OF_RANGE", "market.cost_bp"),
        (cfg_of({"type": "momentum", "lookback": 252, "skip": 21}, every=1, q=0.2), "OUT_OF_RANGE", "portfolio.q"),
        (ml_cfg(h=5, embargo=3), "EMBARGO_VIOLATION", "signal.embargo"),
        (bad_signal, "UNSUPPORTED_REQUEST", "signal.type"),
        (no_stats, "MISSING_REQUIRED", "stats"),
        ({"schema_version": "1.0", "reject": {"code": "NOPE", "message": "x"}}, "OUT_OF_RANGE", "reject.code"),
        ({"schema_version": "1.0", "reject": {"code": "SEALED_PERIOD", "message": "  "}}, "MISSING_REQUIRED", "reject.message"),
        (["not", "an", "object"], "UNSUPPORTED_REQUEST", ""),
    ]
    for doc, code, path in cases:
        r = g.validate(doc)
        assert not r["legal"] and r["code"] == code, (doc, r)
        assert r["errors"][0]["path"] == path and set(r["errors"][0]) == {"code", "path", "message"}, r
    r = g.validate({**eq_cfg(), "schema_version": "9"})               # BAD_SCHEMA_VERSION 不是拒绝码：映射到 UNSUPPORTED_REQUEST
    assert r["code"] == "UNSUPPORTED_REQUEST" and r["errors"][0]["code"] == "BAD_SCHEMA_VERSION"


@requires_external
def test_gate_module_allowlist_and_script_agree():
    _, py, src = _external()
    spec = __import__("importlib.util").util.spec_from_file_location("gate_script", REPO / "scripts" / "ai_gate_validate.py")
    mod = __import__("importlib.util").util.module_from_spec(spec)
    spec.loader.exec_module(mod)                                       # 只定义函数，main() 受 __main__ 保护
    assert mod.ALLOWED == set(gate.ALLOWED_STATARB_MODULES)
    out = subprocess.run([str(py), "-I", "-B", str(REPO / "scripts" / "ai_gate_validate.py"), "--src", str(src)],
                         input=json.dumps({"doc": eq_cfg()}), capture_output=True, text=True, timeout=60)
    res = json.loads(out.stdout)
    assert res["ok"] and res["issues"] == [] and res["denied"] == 0
    assert set(res["modules"]) <= gate.ALLOWED_STATARB_MODULES and "statarb.schema" in res["modules"]
    assert not any(m in res["modules"] for m in ("statarb.registry", "statarb.api"))


@requires_external
def test_import_opens_no_jsonl_or_registry_files():
    """notes 第 9.4 节：在隔离环境里导入校验器模块，记录期间所有被打开的文件。"""
    _, py, src = _external()
    code = (
        "import sys, os, json\n"
        "ev = []\n"
        "def hook(e, a):\n"
        "    if e == 'open' and isinstance(a[0], (str, bytes, os.PathLike)):\n"
        "        ev.append([os.path.realpath(os.fsdecode(a[0])), a[2] if isinstance(a[2], int) else 0])\n"
        "sys.addaudithook(hook)\n"
        "sys.dont_write_bytecode = True\n"
        f"sys.path.insert(0, {str(src)!r})\n"
        "import statarb.schema\n"
        "print(json.dumps({'ev': ev, 'mods': sorted(m for m in sys.modules if m == 'statarb' or m.startswith('statarb.'))}))\n"
    )
    out = subprocess.run([str(py), "-I", "-B", "-c", code], capture_output=True, text=True, timeout=60, cwd=tempfile.gettempdir())
    assert out.returncode == 0, out.stderr[-500:]
    res = json.loads(out.stdout)
    root = os.path.realpath(src.parent)
    reg_dir = os.path.join(root, "regis" + "try")                     # 目录前缀从项目根推算
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
    assert any(p.startswith(os.path.realpath(src)) for p, _ in res["ev"]), "钩子应当记录到对校验器源码的读取"
    for p, flags in res["ev"]:
        assert not p.endswith(".jsonl"), p
        assert not (p == reg_dir or p.startswith(reg_dir + os.sep)), p
        if p == root or p.startswith(root + os.sep):
            assert not flags & write_flags, f"导入期间不应写校验器项目里的文件：{p}"
    assert set(res["mods"]) <= gate.ALLOWED_STATARB_MODULES


def _fake_script(body, timeout_s=3):
    d = Path(tempfile.mkdtemp(prefix="qb_gate_"))
    p = d / "fake_gate.py"
    p.write_text(body)
    return gate.Gate(sys.executable, d, script=p, timeout_s=timeout_s)


def test_gate_fails_closed():
    ok = '{"ok": true, "issues": [], "reject_codes": ["OUT_OF_RANGE"], "modules": ["statarb", "statarb.schema"], "denied": 0}'
    cases = {
        "garbage": "print('not json')",
        "bad_exit": "import sys; sys.exit(3)",
        "extra_module": f"import json; print(json.dumps({{**json.loads('{ok}'), 'modules': ['statarb', 'statarb.registry']}}))",
        "denied_open": f"import json; print(json.dumps({{**json.loads('{ok}'), 'denied': 1}}))",
        "not_ok": "print('{\"ok\": false}')",
        "bad_issue": f"import json; print(json.dumps({{**json.loads('{ok}'), 'issues': [{{'code': 1}}]}}))",
        "hang": "import time; time.sleep(30)",
    }
    for name, body in cases.items():
        try:
            # "hang" 只需要触发超时，0.5 秒足够（原来等满 3 秒）；其他用例仍用默认 3 秒，不受影响
            _fake_script(body, timeout_s=0.5 if name == "hang" else 3).validate(eq_cfg())
        except backend.TranslatorError as e:
            assert e.code == "TRANSLATOR_UNAVAILABLE", name
        else:
            raise AssertionError(f"{name} 应当 fail closed")
    good = _fake_script(f"print('{ok}')").validate(eq_cfg())          # 对照：正常输出能通过
    assert good == {"legal": True, "errors": [], "code": None}
    missing = gate.Gate(Path(tempfile.mkdtemp()) / "no_such_python", tempfile.mkdtemp())
    try:
        missing.validate(eq_cfg())
    except backend.TranslatorError as e:
        assert e.code == "TRANSLATOR_UNAVAILABLE"
    else:
        raise AssertionError("python 不存在应当 fail closed")


def test_gate_unserializable_and_from_config():
    g = _fake_script("print('x')")
    r = g.validate({"x": {1, 2}})                                      # 不可 JSON 化：直接判非法，不启动子进程
    assert not r["legal"] and r["code"] == "UNSUPPORTED_REQUEST"
    for cfg in ({"ai_layer_dir": None, "statarb_src_dir": "/x"}, {"ai_layer_dir": str(tempfile.mkdtemp()), "statarb_src_dir": None}):
        try:
            gate.Gate.from_config(cfg)
        except backend.TranslatorError as e:
            assert e.code == "TRANSLATOR_UNAVAILABLE"
        else:
            raise AssertionError(cfg)
    tmp = Path(tempfile.mkdtemp())
    g = gate.Gate.from_config({"ai_layer_dir": str(tmp), "statarb_src_dir": str(tmp)})
    assert g.python == tmp.resolve() / ".venv" / "bin" / "python"


# ---------------------------------------------------------------- 测试 12：预检
def test_precheck_p_date_positive():
    zh = ["帮我跑一下动量策略,评估到2025年3月", "回测等权基准,从2015年到2024-06-30", "岭回归策略,用到2026年底的数据",
          "测一个均线交叉,评估到2025/03", "回测动量,跑到最新数据", "测一下等权基准直到现在", "看看均线交叉至今的表现", "截至今天的结果"]
    en = ["Backtest momentum through mid-2024", "evaluate equal weight until March 2025", "run the ridge model through 2025-01-15",
          "test a moving-average crossover up to now", "use the latest data for the equal-weight baseline",
          "evaluate through the end of 2024", "backtest momentum to date", "evaluate in 2026"]
    for idea in zh + en:
        r = precheck.precheck(idea)
        assert r["blocked"] and r["rules"][0]["rule_id"] == "P-DATE", idea
        ref = r["refusal"]
        assert ref["code"] == "SEALED_PERIOD" and ref["source"] == "precheck", idea
        assert ref["message_zh"].startswith(plainview.REJECT_MSG["SEALED_PERIOD"][0]) and ref["message_en"].startswith(plainview.REJECT_MSG["SEALED_PERIOD"][1])
        assert len(ref["message_zh"]) > len(plainview.REJECT_MSG["SEALED_PERIOD"][0])         # 附带具体定位


def test_precheck_p_date_negative():
    for idea in ["训练窗口 2048 日的岭回归", "60日动量,每周调仓,评估到2021年底", "evaluate through the end of 2021", "training window of 2048 days",
                 "keep the model up to date: 20/50-day moving average", "评估到 2023-12-31", "from 2015 to 2023, equal weight",
                 "lookback 2024 days momentum", "成本 2025 bp 的等权基准", "单边成本5bp,评估到2023年底", "mid-2023 end"]:
        assert not precheck.precheck(idea)["blocked"], idea


def test_precheck_p_embargo():
    for idea in ["ridge model, 5-day target, embargo of 3 days", "用岭回归预测未来5日收益,embargo 只留3天",
                 "HGB with a 21-day target and embargo of 10 days", "岭回归,预测目标5日,不留 embargo", "ridge 5-day target, no embargo"]:
        r = precheck.precheck(idea)
        assert r["blocked"] and r["rules"][0]["rule_id"] == "P-EMBARGO" and r["refusal"]["code"] == "EMBARGO_VIOLATION", idea
    for idea in ["ridge, 5-day target, embargo of 6 days", "21-day target, embargo of 22 days", "embargo of 3 days",       # 没写 h：留给 C-EMBARGO
                 "岭回归,预测目标5日,embargo 只留10天", "ridge with the basic features"]:
        assert not precheck.precheck(idea)["blocked"], idea


# ---------------------------------------------------------------- 抽取器
def test_extract_patterns():
    f = facts_of("12-1 横截面动量(回看252日,跳过最近21日),每月调仓,单边成本10bp,评估到2021年底")
    assert f == {"signal.lookback": 252, "signal.skip": 21, "portfolio.rebalance_every": 21, "market.cost_bp": 10,
                 "signal.type": "momentum", "period.end": "2021-12-31"}, f
    f = facts_of("Use ridge regression with the extended 21 features to predict the 21-day forward return, 756-day training window, refit every 42 days, ridge alpha 50.0")
    assert f == {"signal.type": "ml", "signal.model": "ridge", "signal.features": "v2", "signal.h": 21, "signal.train_window": 756,
                 "signal.refit_every": 42, "signal.alpha": 50}, f
    assert facts_of("快线20日、慢线50日的双均线策略")["signal.fast"] == 20 and facts_of("20/50-day moving-average crossover")["signal.slow"] == 50
    assert facts_of("加迟滞带:前/后10%进场,掉出前/后30%才离场") == {"portfolio.band_entry": 0.1, "portfolio.band_exit": 0.3}      # 不会被当成 q
    assert facts_of("add a hysteresis band: enter the top/bottom 10%, exit only beyond 30%") == {"portfolio.band_entry": 0.1, "portfolio.band_exit": 0.3}
    assert facts_of("decile long/short (top/bottom 10%)") == {"portfolio.q": 0.1}
    assert facts_of("long/short the top and bottom 7.5%")["portfolio.q"] == 0.075
    assert facts_of("only trade the less liquid half of the stocks")["universe.subset"] == "illiquid_half"        # 不会被 liquid half 抢走
    assert facts_of("only trade the more liquid half")["universe.subset"] == "liquid_half"
    assert facts_of("只在成交额较低的一半股票里选")["universe.subset"] == "illiquid_half"
    assert facts_of("restrict to defensive sectors")["universe.subset"] == "defensive"
    assert facts_of("5-day reversal on the market-beta residual")["signal.type"] == "residual_reversal"
    assert facts_of("对市场β残差做5日均值回归")["signal.type"] == "residual_reversal"
    assert facts_of("plain 5-day reversal (no residualisation)") == {"reversal.N": 5}                            # 单独的反转不产出类型
    assert facts_of("训练窗口1,500日") == {"signal.train_window": 1500}                                          # 千分位要整体读，不能截成 1
    assert facts_of("回看5,10日") == {} and facts_of("动量,5日反转") == {"signal.type": "momentum", "reversal.N": 5}   # 不是千分位的逗号分隔不抽；逗号后的数字正常抽
    assert facts_of("成本 0.5 bp")["market.cost_bp"] == 0.5 and facts_of("每边交易成本20个基点")["market.cost_bp"] == 20
    assert facts_of("rebalance weekly") == {"portfolio.rebalance_every": 5} and facts_of("每周(每5日)调仓") == {"portfolio.rebalance_every": 5}
    assert facts_of("每日数据很重要") == {}                                                                       # "每日"不挨着"调仓"就不算
    assert facts_of("ＴＥＳＴ 单边成本５ｂｐ")["market.cost_bp"] == 5                                              # 全角会先被规范化


def test_extract_date_roles():
    ex = extract.extract("starting March 2015, evaluate through the end of 2021")
    assert [(d.role, d.iso) for d in ex.dates] == [("start", None), ("end", "2021-12-31")]
    assert ex.facts["period.end"].value == "2021-12-31" and not ex.late_dates
    ex = extract.extract("从2015年1月开始,评估到2021年6月底")
    assert ex.facts["period.end"].value == "2021-06-30"
    assert "period.end" not in extract.extract("评估到2021年").facts                  # 只有年份：不唯一，不产出
    assert extract.extract("评估到2025年3月").late_dates and extract.extract("mid-2024").late_dates[0].iso == "2024-06-30"
    assert not extract.extract("2048 日训练窗口").dates


# ---------------------------------------------------------------- 测试 13：一致性
def test_consistency_sources_and_flags():
    idea = "momentum with a 252-day lookback skipping 21 days, rebalance daily, 10 bp one-way cost"
    orc = AlwaysLegal()
    r = consistency.compare(idea, mom_cfg(), orc)
    s = r["sources"]
    assert s["signal.lookback"] == s["signal.skip"] == s["portfolio.rebalance_every"] == s["signal.type"] == "idea"
    assert s["market.cost_bp"] == "mismatch"
    assert s["universe.subset"] == s["portfolio.q"] == s["period.end"] == s["stats.seed"] == "default"
    assert [(f["rule_id"], f["level"], f["field"], f["idea_says"], f["config_has"]) for f in r["flags"]] == \
           [("C-NUM", "warn", "market.cost_bp", "10 bp", "5 bp")]
    assert r["refusal"] is None and len(orc.calls) == 1 and orc.calls[0]["market"]["cost_bp"] == 10      # 反事实：想法里的值写回副本
    assert consistency.compare(idea, mom_cfg(), orc)["sources"] and mom_cfg()["market"]["cost_bp"] == 5   # 原配置没被改


def test_consistency_c_end_c_type_c_conflict():
    orc = AlwaysLegal()
    r = consistency.compare("60日动量,评估到2021年底", mom_cfg(), orc)
    assert [(f["rule_id"], f["level"]) for f in r["flags"]] == [("C-END", "warn")] and r["sources"]["period.end"] == "mismatch"
    r = consistency.compare("用岭回归预测未来5日", mom_cfg(), orc)                       # 类型不同：C-TYPE，不再重复报"缺字段"
    assert [f["rule_id"] for f in r["flags"]] == ["C-TYPE"] and r["sources"]["signal.type"] == "mismatch"
    r = consistency.compare("rebalance weekly, rebalance monthly, momentum", mom_cfg(), orc)
    assert [f["rule_id"] for f in r["flags"]] == ["C-CONFLICT"] and r["flags"][0]["field"] == "portfolio.rebalance_every"
    assert r["sources"]["portfolio.rebalance_every"] == "unchecked"                         # 冲突时即使等于默认也不标 default
    r = consistency.compare("momentum, alpha 100", mom_cfg(), orc)                           # 想法提到了配置里不存在的字段
    assert [(f["rule_id"], f["message_key"]) for f in r["flags"]] == [("C-NUM", "ai.flag.missing")]
    r = consistency.compare("等权基准", eq_cfg(), orc)
    assert r["flags"] == [] and r["sources"]["signal.type"] == "idea"
    assert consistency.compare("x", {"schema_version": "1.0", "reject": {"code": "OUT_OF_RANGE", "message": "m"}}, orc) == {"flags": [], "sources": {}, "refusal": None}


def test_consistency_reversal_days_follow_config_type():
    orc = AlwaysLegal()
    r = consistency.compare("市场β残差的10日反转", resid_cfg(), orc)
    assert [(f["field"], f["idea_says"], f["config_has"]) for f in r["flags"]] == [("signal.L", "10", "5")]
    r = consistency.compare("原始收益的5日反转", cfg_of({"type": "reversal_raw", "lookback": 5}, every=1, q=0.1), orc)
    assert r["flags"] == [] and r["sources"]["signal.lookback"] == "idea"
    r = consistency.compare("5日反转", mom_cfg(), orc)                                        # 动量没有"反转天数"：不比较
    assert r["flags"] == []


def test_consistency_negation_is_unchecked_not_mismatch():
    orc = AlwaysLegal()
    r = consistency.compare("动量策略,不要前后20%", mom_cfg(), orc)
    assert r["flags"] == [] and r["sources"]["portfolio.q"] == "unchecked"                  # 默认值也不标 default：用户明确提到过
    r = consistency.compare("instead of ridge use momentum", mom_cfg(), orc)
    assert r["flags"] == [] and r["sources"]["signal.type"] == "idea"


# ---------------------------------------------------------------- 测试 14：反事实校验（用真实校验器）
def _block(r):
    return [f for f in r["flags"] if f["level"] == "block"]


@requires_external
def test_counterfactual_out_of_range_becomes_block():
    g = real_gate()
    r = consistency.compare("市场β残差的5日反转,每周调仓,多空各取前后20%", resid_cfg(), g)         # 模型把越界的 20% 悄悄改回了 10%
    b = _block(r)
    assert [(f["rule_id"], f["field"], f["code"]) for f in b] == [("C-NUM", "portfolio.q", "OUT_OF_RANGE")], r["flags"]
    assert r["refusal"]["code"] == "OUT_OF_RANGE" and r["refusal"]["source"] == "counterfactual"
    assert r["refusal"]["message_zh"].startswith(plainview.REJECT_MSG["OUT_OF_RANGE"][0])
    assert r["sources"]["portfolio.q"] == "mismatch"
    r = consistency.compare("等权基准,单边成本7bp", eq_cfg(), g)
    assert [(f["field"], f["code"]) for f in _block(r)] == [("market.cost_bp", "OUT_OF_RANGE")]


@requires_external
def test_counterfactual_legal_value_is_only_warn():
    g = real_gate()
    r = consistency.compare("等权基准,单边成本10bp", eq_cfg(), g)                              # 10 是合法取值，只是被改成了 5
    assert _block(r) == [] and r["refusal"] is None
    assert [(f["rule_id"], f["level"], f["field"]) for f in r["flags"]] == [("C-NUM", "warn", "market.cost_bp")]
    r = consistency.compare("evaluate through the end of 2021, equal-weight", eq_cfg(), g)
    assert [(f["rule_id"], f["level"]) for f in r["flags"]] == [("C-END", "warn")]
    r = consistency.compare("市场β残差5日反转,加迟滞带:前/后10%进场,掉出前/后30%才离场", resid_cfg(), g)   # 这个组合跑过：合法
    assert _block(r) == [] and {f["field"] for f in r["flags"]} == {"portfolio.band_entry", "portfolio.band_exit"}


@requires_external
def test_counterfactual_only_blocks_when_the_field_itself_is_illegal():
    g = real_gate()
    r = consistency.compare("momentum, add a hysteresis band: enter the top/bottom 10%, exit only beyond 30%", mom_cfg(), g)
    assert [(f["field"], f["code"]) for f in _block(r)] == [("portfolio.band_entry", "UNSUPPORTED_REQUEST")]     # 动量从没配过迟滞带
    r = consistency.compare("ridge with the basic 8 features, 21-day target", ml_cfg(h=5), g)
    assert _block(r) == [] and [(f["rule_id"], f["field"]) for f in r["flags"]] == [("C-NUM", "signal.h")]      # 报错落在别的字段上：只 warn


def test_c_embargo_blocks_using_config_h_when_idea_has_no_h():
    r = consistency.compare("ridge, embargo of 3 days", ml_cfg(h=5), AlwaysLegal())
    assert [(f["rule_id"], f["code"]) for f in _block(r)] == [("C-EMBARGO", "EMBARGO_VIOLATION")]
    assert r["refusal"]["source"] == "consistency" and "6" in r["refusal"]["message_zh"]
    assert len([f for f in r["flags"] if f["field"] == "signal.embargo"]) == 1                                  # 不重复
    r = consistency.compare("ridge, embargo of 6 days", ml_cfg(h=5), AlwaysLegal())
    assert r["flags"] == [] and r["sources"]["signal.embargo"] == "idea"


# ---------------------------------------------------------------- S14：想法里明写的数值必须和配置一致
class RangeOracle:
    """假 oracle：只认 ma_cross 快线 20 与 q=0.1（或 null），越界就在该字段上报 OUT_OF_RANGE，模拟真实校验器的行为。"""

    def validate(self, doc):
        errors = []
        if doc["signal"].get("fast") not in (None, 20):
            errors.append({"code": "OUT_OF_RANGE", "path": "signal.fast", "message": "fast"})
        if doc["portfolio"]["q"] not in (None, 0.1):
            errors.append({"code": "OUT_OF_RANGE", "path": "portfolio.q", "message": "q"})
        return {"legal": not errors, "errors": errors, "code": errors[0]["code"] if errors else None}


def ma_cfg(**kw):
    return cfg_of({"type": "ma_cross", "fast": 20, "slow": 50}, **kw)


def rev_cfg(**kw):
    return cfg_of({"type": "reversal_raw", "lookback": 5}, every=1, q=0.1, **kw)


def test_s14_extract_new_patterns():
    assert facts_of("美股快10日、慢50日均线交叉") == {"signal.fast": 10, "signal.slow": 50, "signal.type": "ma_cross"}
    assert facts_of("快20日慢50日") == {"signal.fast": 20, "signal.slow": 50}
    assert facts_of("快线20日、慢线50日")["signal.fast"] == 20                                          # 旧写法给同一个值：不冲突
    assert facts_of("moving-average crossover with fast 20 and slow 50 trading days") == \
        {"signal.fast": 20, "signal.slow": 50, "signal.type": "ma_cross"}
    assert facts_of("持有前20%")["portfolio.q"] == 0.2 and facts_of("holdings restricted to the top 20%")["portfolio.q"] == 0.2
    assert facts_of("持有前后10%")["portfolio.q"] == 0.1 and facts_of("top/bottom 10%")["portfolio.q"] == 0.1      # 老写法不受影响
    assert "portfolio.q" not in facts_of("之前10%的时间") and "portfolio.q" not in facts_of("加迟滞带:前/后10%进场")
    assert facts_of("slow 5% of the time") == {}                                                       # 百分号不是天数
    assert facts_of("生成美股机器学习信号配置，模型ridge、特征v1、预测期h为5日、embargo为5日")["signal.h"] == 5
    assert facts_of("prediction horizon of 21 trading days")["signal.h"] == 21


def test_s14_precheck_blocks_embargo_with_chinese_horizon():
    idea = "生成美股机器学习信号配置，模型ridge、特征v1、预测期h为5日、embargo为5日，评估2018-08-01至2019-07-31。"
    r = precheck.precheck(idea)
    assert r["blocked"] and r["rules"][0]["rule_id"] == "P-EMBARGO" and r["refusal"]["code"] == "EMBARGO_VIOLATION"
    ok = precheck.precheck(idea.replace("embargo为5日", "embargo为6日"))
    assert not ok["blocked"]


def test_s14_silent_fast_change_is_blocked():
    idea = "我想要美股快10日、慢50日均线交叉的配置，评估2014-04-01到2015-03-31。"
    r = consistency.compare(idea, ma_cfg(), RangeOracle())                          # 模型悄悄输出了 fast=20
    b = _block(r)
    assert [(f["field"], f["code"]) for f in b] == [("signal.fast", "OUT_OF_RANGE")], r["flags"]
    assert r["refusal"]["code"] == "OUT_OF_RANGE" and r["sources"]["signal.fast"] == "mismatch"
    ok = consistency.compare("美股快20日、慢50日均线交叉的配置", ma_cfg(), RangeOracle())
    assert ok["flags"] == [] and ok["refusal"] is None and ok["sources"]["signal.fast"] == "idea"


def test_s14_silent_q_change_is_blocked():
    idea = "For 2017-07-03 to 2018-06-29, configure US equity raw reversal with a 5-day lookback, daily rebalancing, and holdings restricted to the top 20%."
    r = consistency.compare(idea, rev_cfg(), RangeOracle())
    assert [(f["field"], f["code"]) for f in _block(r)] == [("portfolio.q", "OUT_OF_RANGE")], r["flags"]
    assert r["refusal"]["code"] == "OUT_OF_RANGE"
    ok = consistency.compare("raw reversal with a 5-day lookback, daily rebalancing, top 10% holdings", rev_cfg(), RangeOracle())
    assert ok["flags"] == [] and ok["refusal"] is None and ok["sources"]["portfolio.q"] == "idea"


def test_s14_embargo_via_consistency_when_horizon_is_in_config():
    r = consistency.compare("ridge v1, embargo为5日", ml_cfg(h=5, embargo=6), RangeOracle())      # 模型悄悄把 5 改成了 6
    assert [(f["rule_id"], f["code"]) for f in _block(r)] == [("C-EMBARGO", "EMBARGO_VIOLATION")]


def test_s14_pipeline_with_fake_backend():
    """走完整 API 流程（假后端，不联网）：想法里明写的越界数值，不管模型怎么输出都不能被接受。"""
    ma_idea = "我想要美股快10日、慢50日均线交叉的配置，评估2014-04-01到2015-03-31。"
    rev_idea = "raw reversal with a 5-day lookback, daily rebalancing, holdings restricted to the top 20%"
    ml_idea = "生成美股机器学习信号配置，模型ridge、特征v1、预测期h为5日、embargo为5日"
    ok_idea = "美股快20日、慢50日均线交叉的配置"
    service, fake, _, _ = _api_fixture([ma_cfg(), rev_cfg(), ma_cfg()], oracle=RangeOracle())
    a = _translated(service, ma_idea)
    assert a["status"] != "ok" and a["config"] is None and not a["can_confirm"] and a["refusal"]["code"] == "OUT_OF_RANGE"
    b = _translated(service, rev_idea)
    assert b["config"] is None and not b["can_confirm"] and b["refusal"]["code"] == "OUT_OF_RANGE"
    calls = len(fake.calls)
    c = _translated(service, ml_idea)
    assert len(fake.calls) == calls and c["refusal"]["source"] == "precheck" and c["refusal"]["code"] == "EMBARGO_VIOLATION"
    d = _translated(service, ok_idea)
    assert d["status"] == "ok" and d["config"] == ma_cfg() and d["can_confirm"] and not d["flags"]


@requires_external
def test_s14_real_gate_pipeline_blocks_silent_changes():
    """同上，但用真实校验器当 oracle（不再用自写的假范围）：证明生产链路里校验器报错的路径确实落在被替换的字段上。"""
    gate_ = real_gate()
    ma_idea = "我想要美股快10日、慢50日均线交叉的配置，评估2014-04-01到2015-03-31。"
    rev_idea = "raw reversal with a 5-day lookback, daily rebalancing, holdings restricted to the top 20%"
    ok_idea = "美股快20日、慢50日均线交叉的配置"
    service, fake, _, _ = _api_fixture([ma_cfg(), rev_cfg(), ma_cfg()], oracle=gate_)
    a = _translated(service, ma_idea)                                         # 模型悄悄输出 fast=20
    assert a["status"] == "rejected" and a["config"] is None and not a["can_confirm"], a
    assert a["refusal"]["code"] == "OUT_OF_RANGE" and a["refusal"]["source"] == "counterfactual"
    b = _translated(service, rev_idea)                                        # 模型悄悄输出 q=0.1
    assert b["status"] == "rejected" and b["config"] is None and not b["can_confirm"], b
    assert b["refusal"]["code"] == "OUT_OF_RANGE"
    d = _translated(service, ok_idea)                                         # 想法与配置一致：照常通过
    assert d["status"] == "ok" and d["config"] == ma_cfg() and d["can_confirm"] and not d["flags"], d
    assert len(fake.calls) == 3


# ---------------------------------------------------------------- 测试 15：规则的局限（诚实测试）
def test_known_limits_are_unchecked_not_covered():
    orc = AlwaysLegal()
    cfg = resid_cfg(cost=10)
    for idea in ["市场β残差的二十日反转,单边成本十个基点,前后百分之二十", "residual reversal of twenty days with ten basis points",
                 "回测两年后的表现", "测一下去年的数据", "疫情之后的表现,动量", "最近十年的等权基准", "明年初结束评估"]:
        assert not precheck.precheck(idea)["blocked"], idea                                  # 相对时间、中文数字：预检抓不到，也不误拦
        r = consistency.compare(idea, cfg, orc)
        assert _block(r) == [] and r["refusal"] is None, idea
        assert r["sources"]["market.cost_bp"] == "unchecked" and not [f for f in r["flags"] if f["field"] == "market.cost_bp"], idea
    ex = extract.extract("市场β残差的二十日反转,前后百分之二十")
    assert "portfolio.q" not in ex.facts and "reversal.N" not in ex.facts
    ex = extract.extract("不要前后20%,不要前/后10%进场")
    assert ex.facts == {} and {"portfolio.q", "portfolio.band_entry"} <= ex.negated
    assert facts_of("reversal") == {} and facts_of("5日反转") == {"reversal.N": 5}


def test_known_limit_default_label_means_nothing_extracted():
    """如实固化一个缺陷：配置值恰好等于默认值、而想法用了抽不出来的写法时，来源会标 default（"你没提"），其实用户提过。"""
    r = consistency.compare("市场β残差的5日反转,前后百分之二十", resid_cfg(), AlwaysLegal())
    assert r["sources"]["portfolio.q"] == "default"


# ---------------------------------------------------------------- plainview
@requires_external
def test_plainview_labels_cover_every_schema_field():
    _, py, src = _external()
    code = (
        "import sys, json\n"
        f"sys.dont_write_bytecode = True\nsys.path.insert(0, {str(src)!r})\n"
        "from statarb import schema\n"
        "paths = set()\n"
        "for c in schema.enumerate_valid_configs(costs=(0.5, 5), ends=('2023-12-31',), starts=('auto', '2015-01-01')):\n"
        "    paths.add('schema_version')\n"
        "    for sec, v in c.items():\n"
        "        if isinstance(v, dict):\n"
        "            paths.update(sec + '.' + k for k in v)\n"
        "print(json.dumps(sorted(paths)))\n"
    )
    out = subprocess.run([str(py), "-I", "-B", "-c", code], capture_output=True, text=True, timeout=120, cwd=tempfile.gettempdir())
    assert out.returncode == 0, out.stderr[-500:]
    schema_paths = set(json.loads(out.stdout))
    assert len(schema_paths) >= 25 and schema_paths <= set(plainview.LABELS), sorted(schema_paths - set(plainview.LABELS))
    for path, (zh, en) in plainview.LABELS.items():
        assert zh.strip() and en.strip() and zh != en, path


@requires_external
def test_plainview_reject_msg_matches_original():
    ai, py, _ = _external()
    code = (
        "import sys, json\n"
        f"sys.dont_write_bytecode = True\nsys.path.insert(0, {str(ai)!r})\n"
        "import ai_lib\n"
        "print(json.dumps(ai_lib.REJECT_MSG, ensure_ascii=False))\n"
    )
    out = subprocess.run([str(py), "-I", "-B", "-c", code], capture_output=True, text=True, timeout=120, cwd=tempfile.gettempdir())
    assert out.returncode == 0, out.stderr[-500:]
    orig = {k: tuple(v) for k, v in json.loads(out.stdout).items()}
    assert orig == plainview.REJECT_MSG, "网站里的拒绝文案和 ai_lib.REJECT_MSG 不一致"


def test_plainview_view_and_texts():
    cfg = ml_cfg()
    r = consistency.compare("ridge, 5-day target, embargo of 6 days, 10 bp one-way cost", cfg, AlwaysLegal())
    view = plainview.build_view(cfg, r["sources"])
    assert [v["field"] for v in view][:3] == ["schema_version", "universe.subset", "signal.type"]
    by = {v["field"]: v for v in view}
    assert by["market.cost_bp"]["source"] == "mismatch" and by["market.cost_bp"]["value_text"] == "5 bp" and by["market.cost_bp"]["important"]
    assert by["portfolio.q"]["value_text"] == "10%" and by["signal.alpha"]["value_text"] == "100"
    assert by["signal.embargo"]["source"] == "idea" and by["signal.embargo"]["important"] and not by["stats.seed"]["important"]
    assert "signal.fast" not in by and set(by["signal.h"]) == {"field", "label_zh", "label_en", "value_text", "source", "important"}
    flag = r["flags"][0]
    assert plainview.flag_message(flag, "zh") == "你写的是「10 bp」,配置里是「5 bp」"
    assert plainview.flag_message(flag, "en") == 'You wrote "10 bp", the config has "5 bp"'
    assert plainview.value_text("x", None) == "null" and plainview.value_text("portfolio.q", None) == "null"
    ref = plainview.make_refusal("NOT_A_CODE", "model", "a", "b")
    assert ref["code"] == "UNSUPPORTED_REQUEST" and ref["source"] == "model" and ref["message_zh"].endswith(" a") and ref["message_en"].endswith(" b")


def test_extract_and_rules_are_pure_functions():
    """规则是纯函数：不碰磁盘、不启动进程、不联网。"""
    with mock.patch("subprocess.Popen", side_effect=AssertionError), mock.patch("builtins.open", side_effect=AssertionError), \
            mock.patch("socket.socket", side_effect=AssertionError):
        ex = extract.extract("momentum, 252-day lookback, through the end of 2021")
        precheck.precheck("评估到2025年3月", extract.extract("评估到2025年3月"))
        consistency.compare("momentum", mom_cfg(), AlwaysLegal())
        plainview.build_view(mom_cfg(), {})
    assert ex.facts["signal.lookback"].value == 252


# ================================================================ S3：假 worker 与进程监督（19–28）
FAKE_SCRIPT = REPO / "tests" / "fake_translator_worker.py"
_fake_pids = set()


def _assert_pid_gone(pid, timeout=3):
    deadline = time.monotonic() + timeout
    while True:
        # Linux 的子收割者测试可能把孤儿领养为本进程的子进程。
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        if time.monotonic() >= deadline:
            raise AssertionError(f"假 worker 或孙进程仍存在：pid={pid}")
        time.sleep(0.02)


def _wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "等待状态变化超时"
        time.sleep(0.01)


def _fake_cfg(root, **timeouts):
    return {"enabled": True, "backend": "local_lora", "ai_layer_dir": str(root),
            "timeouts": {"load_s": 2, "request_s": 2, "idle_s": 120, **timeouts}}


def _model():
    return {"version": "fake-v1", "base_model": "models/fake", "adapter_path": "adapters/fake"}


def _command(pid_log, mode="normal", *extra):
    return [sys.executable, "-I", "-B", str(FAKE_SCRIPT), "--pid-log", str(pid_log), "--mode", mode, *extra]


@contextlib.contextmanager
def _fake(mode="normal", *, timeouts=None, cfg_extra=None, provider=None, audit=None, extra=()):
    with tempfile.TemporaryDirectory(prefix="qb_ai_supervisor_") as tmp:
        root = Path(tmp).resolve()
        pid_log = root / "fake-pids"
        cfg = {**_fake_cfg(root, **(timeouts or {})), **(cfg_extra or {})}
        owner = local_lora.Supervisor(cfg, root=root, model_provider=provider or _model, audit=audit,
                                      test_command=_command(pid_log, mode, *extra),
                                      shutdown_grace_s=0.15, term_grace_s=0.15)
        try:
            yield owner, root, pid_log
        finally:
            owner.close()
            if pid_log.exists():
                pids = {int(p) for p in pid_log.read_text().splitlines()}
                _fake_pids.update(pids)
                for pid in pids:
                    _assert_pid_gone(pid)
            assert not paths.pid_path(root).exists()
            assert owner._lock_file is None
            assert owner._watcher is None or not owner._watcher.is_alive()


def _failure(result, code):
    assert result["config"] is None and result["refusal"] == {"code": code}, result
    return backend.ERROR_HTTP[code]


def test_supervisor_19_normal_lazy_and_protocol():
    with _fake() as (s, root, pid_log):
        assert s.status()["state"] == "idle" and s.starts == 0 and not pid_log.exists()
        idea = '中文 idea; $(echo unsafe) `echo unsafe` "\n第二行'
        out = s.translate(idea)
        doc = out["config"]
        assert out["refusal"] is None and out["parse_mode"] == "strict" and out["latency_ms"] >= 0, {
            "result": out,
            # 假 worker 的启动异常须可见，便于区分沙箱限制和监督者协议缺陷。
            "worker_stderr": (paths.state_dir(root) / "worker.err").read_text(encoding="utf-8"),
        }
        assert out["backend_id"] == "local_lora:fake-v1" and out["model_version"] == "fake-v1"
        assert doc["idea"] == idea and doc["pgid"] == doc["pid"] and doc["nice"] >= 10
        assert doc["request_keys"] == ["id", "idea", "op"]
        assert not {"mlx", "mlx_lm", "ai_lib", "statarb"} & set(doc["imports"])
        assert idea not in " ".join(s._proc.args) and s.status()["state"] == "ready"
        assert s.translate("another idea")["config"]["pid"] == doc["pid"] and s.starts == 1
        assert json.loads(paths.pid_path(root).read_text())["pid"] == doc["pid"]
        for file in (paths.pid_path(root), paths.lock_path(root), paths.state_dir(root) / "worker.err"):
            assert stat.S_IMODE(file.stat().st_mode) == 0o600
        assert stat.S_IMODE(paths.state_dir(root).stat().st_mode) == 0o700


def test_supervisor_20_timeouts_reap_and_never_reuse():
    for mode in ("hang", "stubborn", "load_hang", "partial"):
        with _fake(mode, timeouts={"load_s": 0.25, "request_s": 0.2}) as (s, root, pid_log):
            for _ in range(2):
                out = s.translate("timeout example")
                assert _failure(out, "TRANSLATOR_TIMEOUT") == 504
                assert s._proc is None and s._lock_file is None and not paths.pid_path(root).exists()
                for pid in map(int, pid_log.read_text().splitlines()):
                    _assert_pid_gone(pid)
            assert s.starts == 2


def test_supervisor_21_process_group_cleans_child():
    with _fake("spawn_child") as (s, _, _):
        doc = s.translate("spawn a child")["config"]
        assert type(doc["child_pid"]) is int and os.getpgid(doc["child_pid"]) == doc["pid"]
        s._kill()
        _assert_pid_gone(doc["pid"])
        _assert_pid_gone(doc["child_pid"])
        assert s.status()["state"] == "idle"
        s._kill()  # 重复回收也安全

    # 顽固 worker 不响应 shutdown、stdin EOF 或 TERM，且从不主动杀孙进程。
    # Linux 用子收割者接管孤儿并 wait，避免容器 PID 1 不及时收尸造成假失败。
    linux = sys.platform.startswith("linux")
    previous_subreaper = ctypes.c_int()
    if linux:
        libc = ctypes.CDLL(None)
        assert libc.prctl(37, ctypes.byref(previous_subreaper), 0, 0, 0) == 0
        assert libc.prctl(36, 1, 0, 0, 0) == 0
    try:
        with _fake("spawn_child_stubborn") as (s, _, _):
            doc = s.translate("spawn a stubborn child")["config"]
            assert doc is not None
            pgid, child_pid = doc["pid"], doc["child_pid"]
            # 保存真函数供失败清理使用；即使误把 killpg 改成只杀组长也不会遗留孙进程。
            kill_group = os.killpg
            cleaned = False
            try:
                assert type(child_pid) is int and os.getpgid(child_pid) == pgid
                os.kill(child_pid, 0)
                with mock.patch.object(local_lora.os, "killpg", wraps=kill_group) as group_signals:
                    s._kill()
                group_signals.assert_any_call(pgid, signal.SIGTERM)
                group_signals.assert_any_call(pgid, signal.SIGKILL)
                _assert_pid_gone(pgid)
                _assert_pid_gone(child_pid)
                cleaned = True
                assert s.status()["state"] == "idle"
                s._kill()
            finally:
                if not cleaned:
                    try:
                        kill_group(pgid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    _assert_pid_gone(child_pid)
    finally:
        if linux:
            assert libc.prctl(36, previous_subreaper.value, 0, 0, 0) == 0


def test_supervisor_22_idle_reap_then_restart():
    with _fake(timeouts={"idle_s": 1}) as (s, _, _):
        first = s.translate("first")["config"]["pid"]
        _wait_until(lambda: s._proc is None, timeout=3)
        assert s.status()["state"] == "idle"
        _assert_pid_gone(first)
        second = s.translate("second")["config"]["pid"]
        assert second != first and s.starts == 2


def test_supervisor_23_single_flight_does_not_queue():
    with _fake("slow") as (s, _, pid_log):
        replies = []
        t = threading.Thread(target=lambda: replies.append(s.translate("first thread")))
        t.start()
        try:
            _wait_until(lambda: s._state == "busy")
            before = time.monotonic()
            assert _failure(s.translate("second thread"), "TRANSLATOR_BUSY") == 429
            assert time.monotonic() - before < 0.2 and s.starts == 1
            assert len(pid_log.read_text().splitlines()) == 1
        finally:
            t.join(timeout=4)
        assert not t.is_alive() and len(replies) == 1 and replies[0]["config"] is not None


def test_supervisor_24_crashes_open_breaker_and_recover():
    audit = Rec()
    with _fake("crash", audit=audit) as (s, _, _):
        for _ in range(3):
            assert _failure(s.translate("crash example"), "TRANSLATOR_BACKEND_FAILED") == 502
        assert s.starts == 3 and s.status()["state"] == "cooldown"
        assert _failure(s.translate("fourth"), "TRANSLATOR_UNAVAILABLE") == 503
        assert s.starts == 3 and audit.actions() == ["ai_translator_breaker_open"]
        assert audit.calls[0][1] == {"failures": 3}
        s._cooldown_until = time.monotonic() - 1
        s._failures = [time.monotonic() - 601]  # 时间窗口过后可以重新尝试
        assert s.status()["state"] == "idle"
        _failure(s.translate("after cooldown"), "TRANSLATOR_BACKEND_FAILED")
        assert s.starts == 4 and len(s._failures) == 1


def test_supervisor_25_invalid_protocol_is_fatal():
    for mode in ("non_json", "long_line", "wrong_id", "bad_ready", "invalid_utf8", "bad_fields",
                 "bad_object", "lenient", "duplicate_key", "gen_failed"):
        with _fake(mode) as (s, _, pid_log):
            assert _failure(s.translate("protocol example"), "TRANSLATOR_BACKEND_FAILED") == 502
            assert s._proc is None
            for pid in map(int, pid_log.read_text().splitlines()):
                _assert_pid_gone(pid)


def _supervisor_process(root, mode="normal", prefix="", suffix="while True: time.sleep(60)"):
    pid_log = root / "fake-pids"
    script = (
        "import sys, json, time, signal\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        + prefix + "\n"
        "from src.ai_translator.local_lora import Supervisor\n"
        f"s = Supervisor({_fake_cfg(root)!r}, root={str(root)!r}, model_provider=lambda: {_model()!r}, "
        f"test_command={_command(pid_log, mode)!r}, shutdown_grace_s=0.15, term_grace_s=0.15)\n"
        "print(json.dumps(s.translate('parent lifecycle')), flush=True)\n"
        + suffix + "\n"
    )
    return subprocess.Popen([sys.executable, "-I", "-B", "-c", script], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env={**os.environ, "QB_TESTING": "1"}, text=True)


def test_supervisor_26_parent_sigkill_worker_exits_itself():
    # 容器内 Linux 的 PID 1 未必及时收割；由测试进程领养并 wait，不以僵尸假装存活。
    linux = sys.platform.startswith("linux")
    if linux:
        assert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0
    try:
        with tempfile.TemporaryDirectory(prefix="qb_ai_parent_") as tmp:
            root = Path(tmp)
            parent = _supervisor_process(root)
            worker_pid = None
            try:
                reply = json.loads(parent.stdout.readline())
                worker_pid = reply["config"]["pid"]
                _fake_pids.add(worker_pid)
                parent.kill()
                parent.wait(timeout=3)
                _assert_pid_gone(worker_pid, timeout=3)
            finally:
                if parent.poll() is None:
                    parent.kill()
                parent.communicate(timeout=5)
                if worker_pid is not None:
                    try:
                        os.killpg(worker_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
    finally:
        if linux:
            ctypes.CDLL(None).prctl(36, 0, 0, 0, 0)


def test_supervisor_27_shared_file_lock_blocks_second_supervisor():
    with _fake() as (first, root, pid_log):
        pid = first.translate("first owner")["config"]["pid"]
        second = local_lora.Supervisor(first.cfg, root=root, model_provider=_model,
                                       test_command=_command(pid_log), shutdown_grace_s=0.15, term_grace_s=0.15)
        try:
            assert _failure(second.translate("second owner"), "TRANSLATOR_UNAVAILABLE") == 503
            assert second.starts == 0 and len(pid_log.read_text().splitlines()) == 1
            assert first.translate("still owns lock")["config"]["pid"] == pid
            first.close()
            assert second.translate("lock released")["config"] is not None
            assert second.starts == 1
        finally:
            second.close()


def test_supervisor_28_environment_is_minimal_and_offline():
    with mock.patch.dict(os.environ, {"AI_TRANSLATOR_API_KEY": "fake-key-do-not-pass",
                                      "QB_FAKE_SECRET": "fake-secret-do-not-pass", "PYTHONPATH": "/invalid/path"}):
        with _fake() as (s, _, _):
            doc = s.translate("environment example")["config"]
            env = doc["env"]
            assert "AI_TRANSLATOR_API_KEY" not in env and "QB_FAKE_SECRET" not in env
            assert "PYTHONPATH" not in env and "QB_TESTING" not in env
            assert "fake-key-do-not-pass" not in json.dumps(env) and "fake-secret-do-not-pass" not in json.dumps(env)
            assert set(env) <= set(local_lora.clean_environment()) | {"LC_CTYPE"}  # Python 自身可能做 locale coercion
            assert env["HF_HUB_OFFLINE"] == "1" and env["TRANSFORMERS_OFFLINE"] == "1"
            assert env["TOKENIZERS_PARALLELISM"] == "false"


def test_supervisor_default_disabled_and_invalid_input_no_process():
    with _fake(cfg_extra={"enabled": False}) as (s, _, pid_log):
        with mock.patch("src.ai_translator.local_lora.subprocess.Popen", side_effect=AssertionError):
            assert s.status()["state"] == "disabled"
            _failure(s.translate("disabled"), "TRANSLATOR_DISABLED")
            assert s.starts == 0 and not pid_log.exists()
    with _fake() as (s, _, _):
        for idea in (None, "", "   ", 1):
            _failure(s.translate(idea), "TRANSLATOR_INPUT_INVALID")
        _failure(s.translate("a" * 501), "TRANSLATOR_INPUT_TOO_LONG")
        _failure(s.translate("x", task="unknown"), "TRANSLATOR_INPUT_INVALID")
        assert s.starts == 0
    with mock.patch.dict(os.environ, {"QB_TESTING": "0"}):
        try:
            local_lora.Supervisor(test_command=[sys.executable])
        except ValueError:
            pass
        else:
            raise AssertionError("生产环境不应接受测试命令")


def test_supervisor_bad_output_is_not_retried_and_hash_warning():
    with _fake("bad_output") as (s, _, _):
        result = s.translate("unparseable")
        _failure(result, "TRANSLATOR_BAD_OUTPUT")
        assert result["raw_output"] == "no object" and s.starts == 1 and s.status()["state"] == "ready"
    with _fake(extra=("--schema-sha256", "0" * 64)) as (s, _, _):
        assert s.translate("changed schema")["config"] is not None
        assert s.status()["warning"] == "SCHEMA_CHANGED"


def test_supervisor_disable_and_model_switch_after_request():
    model = _model()
    with _fake("slow", provider=lambda: dict(model)) as (s, _, _):
        replies = []
        t = threading.Thread(target=lambda: replies.append(s.translate("switch during generation")))
        t.start()
        try:
            _wait_until(lambda: s._state == "busy")
            old_pid = s._proc.pid
            model["version"] = "fake-v2"
        finally:
            t.join(timeout=4)
        assert not t.is_alive() and replies[0]["model_version"] == "fake-v1"
        assert replies[0]["config"] is not None and s._proc is None
        _assert_pid_gone(old_pid)
        assert s.translate("next version")["model_version"] == "fake-v2"
        pid = s._proc.pid
        s.update_config({**s.cfg, "enabled": False})
        _assert_pid_gone(pid)
        assert s.status()["state"] == "disabled"
    with _fake("hang", timeouts={"request_s": 2}) as (s, _, _):
        replies = []
        t = threading.Thread(target=lambda: replies.append(s.translate("disable during generation")))
        t.start()
        try:
            _wait_until(lambda: s._state == "busy")
            pid = s._proc.pid
            s.update_config({**s.cfg, "enabled": False})
        finally:
            t.join(timeout=4)
        assert not t.is_alive() and replies[0]["config"] is None
        _assert_pid_gone(pid)


def test_supervisor_daily_limit_resets_by_local_date():
    with _fake(cfg_extra={"limits": {"daily_limit": 1}}) as (s, _, _):
        assert s.translate("first")["config"] is not None
        assert s.status()["remaining_today"] == 0
        assert _failure(s.translate("second"), "TRANSLATOR_DAILY_LIMIT") == 429
        s._day = None
        assert s.translate("new day")["config"] is not None and s.starts == 1


def test_supervisor_exit_and_signals_chain_existing_handler():
    for signum in (None, signal.SIGTERM, signal.SIGINT):
        with tempfile.TemporaryDirectory(prefix="qb_ai_exit_") as tmp:
            root = Path(tmp)
            if signum is None:
                parent = _supervisor_process(root, suffix="pass")
            else:
                marker = root / "previous-handler"
                prefix = (
                    "def previous(sig, frame):\n"
                    f"    open({str(marker)!r}, 'w').write(str(sig))\n"
                    "    raise SystemExit(0)\n"
                    f"signal.signal({int(signum)}, previous)\n"
                )
                parent = _supervisor_process(root, prefix=prefix)
            try:
                reply = json.loads(parent.stdout.readline())
                pid = reply["config"]["pid"]
                _fake_pids.add(pid)
                if signum is not None:
                    parent.send_signal(signum)
                _, err = parent.communicate(timeout=5)
                assert parent.returncode == 0, err
                _assert_pid_gone(pid)
                assert not paths.pid_path(root).exists()
                if signum is not None:
                    assert marker.read_text() == str(int(signum))
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.communicate(timeout=5)


def test_supervisor_stale_pid_recovery_verifies_identity():
    with tempfile.TemporaryDirectory(prefix="qb_ai_stale_") as tmp:
        root = Path(tmp)
        paths.state_dir(root).mkdir(parents=True)
        p = subprocess.Popen(_command(root / "fake-pids"), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, cwd=root, start_new_session=True)
        try:
            assert json.loads(p.stdout.readline())["op"] == "ready"
            _fake_pids.add(p.pid)
            # 另一线程模拟系统 reaper，回收不属于新 Supervisor 的直接子进程。
            reaper = threading.Thread(target=p.wait)
            reaper.start()
            write(paths.pid_path(root), {"pid": p.pid})
            # 沙箱不做命令行查询；仅身份读取在这里注入，信号、锁与实际退出均用真进程验证。
            with mock.patch("src.ai_translator.local_lora._command_args", return_value=[os.fsencode(str(FAKE_SCRIPT))]):
                s = local_lora.Supervisor(_fake_cfg(root), root=root, model_provider=_model,
                                          test_command=_command(root / "fake-pids"), term_grace_s=0.2)
            try:
                reaper.join(timeout=3)
                _assert_pid_gone(p.pid)
                assert s.starts == 0 and not paths.pid_path(root).exists()
            finally:
                s.close()
        finally:
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGKILL)
            p.communicate(timeout=5)
    with tempfile.TemporaryDirectory(prefix="qb_ai_unrelated_") as tmp:
        root = Path(tmp)
        s = local_lora.Supervisor(_fake_cfg(root), root=root, model_provider=_model,
                                  test_command=_command(root / "fake-pids"))
        write(paths.pid_path(root), {"pid": os.getpid()})
        try:
            _failure(s.translate("must not kill unrelated process"), "TRANSLATOR_UNAVAILABLE")
            assert s.starts == 0 and paths.pid_path(root).exists()
        finally:
            s.close()


def test_supervisor_stderr_truncates_on_start():
    with _fake() as (s, root, _):
        err = paths.state_dir(root) / "worker.err"
        err.write_bytes(b"x" * (1024 * 1024 + 1))
        assert s.translate("stderr cap")["config"] is not None
        assert err.stat().st_size == 0


def test_supervisor_production_command_is_fixed_without_starting_model():
    with _fake() as (s, root, _):
        s._test_command = None
        with mock.patch("src.ai_translator.local_lora.subprocess.Popen", side_effect=OSError("not started")) as popen:
            _failure(s.translate("must only go to stdin"), "TRANSLATOR_UNAVAILABLE")
        cmd = popen.call_args.args[0]
        kw = popen.call_args.kwargs
        assert cmd == [str(root / ".venv" / "bin" / "python"), "-I", str(local_lora.SCRIPT),
                       "--ai-layer-dir", str(root), "--model", str(root / "models" / "fake"),
                       "--adapter", str(root / "adapters" / "fake")]
        assert kw["start_new_session"] is True and kw["close_fds"] is True
        assert not kw.get("shell", False) and kw["env"] == local_lora.clean_environment()
        assert kw["cwd"] == root and s._lock_file is None and s.starts == 0


# ================================================================ S4：API 与日志
_ACCESS = {"is_local": True, "authenticated": True, "requested_with": True, "origin_ok": True}


def _api_fixture(outcomes=(), *, cfg=None, oracle=None, **kwargs):
    loader, _, _, root = make({"enabled": True, "backend": "fake", **(cfg or {})})
    fake = api.FakeBackend(outcomes)
    factory = mock.Mock(return_value=fake)
    service = api.API(root, loader=loader, backend_factory=factory, oracle=oracle or AlwaysLegal(), **kwargs)
    return service, fake, factory, root


def _call(service, action, body=None, **access):
    return api.handle("GET" if action == "status" else "POST", "/api/ai/" + action,
                      json.dumps(body if body is not None else {}).encode(), service=service,
                      **{**_ACCESS, **access})


def _translated(service, idea="equal-weight portfolio", **extra):
    code, result = _call(service, "translate", {"idea": idea, **extra})
    assert code == 200, (code, result)
    return result


def _rows(root):
    return [json.loads(line) for p in sorted((root / "data" / "ai_translator").glob("log_*.jsonl"))
            for line in p.read_text().splitlines()]


def test_api_01_external_guard_and_missing_markers():
    root = Path(tempfile.mkdtemp(prefix="qb_ai_guard_"))
    ai, research = root / "ai", root / "research"
    adapter = ai / "adapters" / "run2_best"
    adapter.mkdir(parents=True)
    (research / "registry").mkdir(parents=True)
    missing = external_fingerprint(ai, research)
    assert len(missing) == 3 and all(value is None for value in missing.values())
    for p in (ai / "runs.jsonl", ai / "MODEL_REGISTRY.json", research / "registry" / "trials.jsonl", adapter / "weights"):
        before = external_fingerprint(ai, research)
        p.write_bytes(b"first")
        created = external_fingerprint(ai, research)
        assert created != before
        p.write_bytes(b"second")
        assert external_fingerprint(ai, research) != created
        assert str(p) in created
    assert external_fingerprint() == EXTERNAL_BEFORE


def test_api_08_remote_denied_before_config_or_auth():
    # Host/代理头的解析在 S5；S4 覆盖该检查传入 False 后四条业务路径均被拒。
    from src import admin
    blocked = mock.Mock()
    service, fake, factory, root = _api_fixture(audit_blocked=blocked)
    allowed = {"localhost:8765", "127.0.0.1:8765"}
    cases = [{"Host": "x.trycloudflare.com"}, {"Host": "evil.invalid"}]
    cases += [{"Host": "127.0.0.1:8765", name: "fake-proxy"} for name in sorted(admin.PROXY_HEADERS)]
    with mock.patch.object(service.loader, "load", side_effect=AssertionError("must not load")):
        for incoming in cases:
            local = admin.is_local_request(incoming, allowed)
            assert local is False
            for action in ("status", "translate", "confirm", "feedback"):
                assert _call(service, action, is_local=local, authenticated=False)[0] == 404
    assert blocked.call_count == len(cases) * 4
    assert fake.calls == [] and not factory.called and not (root / "data" / "ai_translator").exists()
    blocked.side_effect = OSError("audit unavailable")
    assert _call(service, "status", is_local=False)[0] == 404


def test_api_10_authentication_and_origin_precede_config():
    service, fake, factory, _ = _api_fixture()
    with mock.patch.object(service.loader, "load", side_effect=AssertionError("must not load")):
        for action in ("status", "translate", "confirm", "feedback"):
            assert _call(service, action, authenticated=False)[0] == 401
        for action in ("translate", "confirm", "feedback"):
            assert _call(service, action, requested_with=False)[0] == 401
            code, result = _call(service, action, origin_ok=False)
            assert code == 403 and result["error"] == "TRANSLATOR_BAD_ORIGIN"
    assert not factory.called and not fake.calls
    assert service.handle("POST", "/api/ai/translate")[0] == 404


def test_api_11_input_validation_never_constructs_backend():
    service, fake, factory, _ = _api_fixture()
    cases = ["", "   ", None, 2, [], {}, "\x00\x01", "x" * 501]
    with mock.patch("subprocess.Popen", side_effect=AssertionError("must not start")):
        for idea in cases:
            code, result = _call(service, "translate", {"idea": idea})
            assert code == 400, (idea, result)
            assert result["error"] == ("TRANSLATOR_INPUT_TOO_LONG" if isinstance(idea, str) and len(idea) > 500 else "TRANSLATOR_INPUT_INVALID")
        for raw in (b"", b"{", b"[]", b"\xff", b"{}" * 40000):
            assert service.handle("POST", "/api/ai/translate", raw, **_ACCESS)[0] == 400
    assert not factory.called and not fake.calls


def test_api_disabled_missing_and_invalid_all_posts():
    for content in (None, {"enabled": False}, "{broken"):
        loader, _, _, root = make(content)
        factory = mock.Mock(side_effect=AssertionError("must not create"))
        service = api.API(root, loader=loader, backend_factory=factory)
        with mock.patch("subprocess.Popen", side_effect=AssertionError("must not start")):
            code, status = _call(service, "status")
            assert code == 200 and status["state"] == "disabled" and not status["enabled"]
            for action in ("translate", "confirm", "feedback"):
                assert _call(service, action)[1]["error"] == "TRANSLATOR_DISABLED"
        assert not factory.called
    service, fake, _, _ = _api_fixture([eq_cfg()])
    _translated(service)
    service.loader.save({"enabled": False})
    assert _call(service, "status")[1]["state"] == "disabled" and fake.closed


def test_api_16_17_logs_complete_private_and_no_transport_secrets():
    service, fake, _, root = _api_fixture([eq_cfg()])
    result = _translated(service, "equal-weight\x00 portfolio", token="fake-token-never-log", Cookie="fake-cookie-never-log",
                         Authorization="fake-auth-never-log", ip="192.0.2.77", headers={"X-Test": "private-header"})
    assert fake.calls == [("equal-weight portfolio", "idea_to_config")]
    rid = result["request_id"]
    assert _call(service, "confirm", {"request_id": rid})[0] == 200
    assert _call(service, "feedback", {"request_id": rid, "verdict": "wrong", "note": "please review",
                                       "Cookie": "fake-cookie-never-log"})[0] == 200
    rows = _rows(root)
    assert [r["event"] for r in rows] == ["translate", "confirm"]
    row = rows[0]
    assert set(row) == {"event", "ts", "request_id", "backend_id", "model", "idea", "precheck", "raw_output",
                        "parse_mode", "config", "gate", "flags", "status", "latency_ms"}
    assert row["model"] == fake.model and row["config"] == eq_cfg() and row["raw_output"]
    assert row["idea"] == "equal-weight\x00 portfolio"  # 日志保留输入原文；交给后端前清除控制字符
    assert row["gate"] == {"legal": True, "errors": [], "code": None}
    assert datetime.datetime.fromisoformat(row["ts"]).tzinfo is not None
    assert rows[1]["request_id"] == rid and rows[1]["ack_flags"] is False
    directory = root / "data" / "ai_translator"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    text = "".join(p.read_text() for p in directory.glob("*.jsonl"))
    for value in ("fake-token-never-log", "fake-cookie-never-log", "fake-auth-never-log", "192.0.2.77", "private-header", "Cookie", "Authorization"):
        assert value not in text, value
    assert not re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text)
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in directory.glob("*.jsonl"))


def test_api_18_confirm_warn_server_copy_duplicate_expiry_and_lru():
    clock = [100.0]
    service, fake, _, root = _api_fixture([eq_cfg(cost=5)] * 25, clock=lambda: clock[0], rate_hit=lambda *args: False)
    result = _translated(service, "equal-weight with 10 basis points")
    assert result["status"] == "ok" and result["needs_ack"] and result["can_confirm"]
    rid = result["request_id"]
    result["config"]["market"]["cost_bp"] = 999
    assert _call(service, "confirm", {"request_id": rid})[1]["error"] == "TRANSLATOR_ACK_REQUIRED"
    assert _call(service, "confirm", {"request_id": rid, "ack_flags": 1})[0] == 409
    code, confirmed = _call(service, "confirm", {"request_id": rid, "ack_flags": True, "config": {"evil": True}})
    assert code == 200 and confirmed["config"] == eq_cfg(cost=5)
    assert _call(service, "confirm", {"request_id": rid, "ack_flags": True})[1]["error"] == "TRANSLATOR_ALREADY_CONFIRMED"
    expired = _translated(service)["request_id"]
    clock[0] += service.TTL
    assert _call(service, "confirm", {"request_id": expired})[0] == 410
    first = _translated(service)["request_id"]
    second = _translated(service)["request_id"]
    for _ in range(18):
        _translated(service)
    _call(service, "feedback", {"request_id": first, "verdict": "wrong"})  # 访问 first，使 second 成为最旧的一条
    _translated(service)
    assert len(service._cache) == 20
    assert _call(service, "confirm", {"request_id": second})[0] == 410
    assert _call(service, "confirm", {"request_id": first})[0] == 200
    assert len([r for r in _rows(root) if r["event"] == "confirm"]) == 2


def test_api_18_concurrent_confirmation_appends_once():
    service, _, _, root = _api_fixture([eq_cfg()])
    rid = _translated(service)["request_id"]
    barrier = threading.Barrier(3)
    results = []
    def confirm():
        barrier.wait()
        results.append(_call(service, "confirm", {"request_id": rid})[0])
    threads = [threading.Thread(target=confirm) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()
    assert sorted(results) == [200, 409]
    assert [r["event"] for r in _rows(root)] == ["translate", "confirm"]


@requires_external
def test_api_29_full_pipeline_real_gate_fake_backend():
    oracle = real_gate()
    rejected = {"schema_version": "1.0", "reject": {"code": "MISSING_REQUIRED", "message": "Please specify a signal"}}
    service, fake, _, root = _api_fixture([eq_cfg(), rejected, eq_cfg(cost=7), eq_cfg(cost=5), mom_cfg()], oracle=oracle)
    before = oracle.calls
    result = _translated(service)
    assert result["status"] == "ok" and result["config"] == eq_cfg() and result["view"] and result["can_confirm"]
    assert _call(service, "confirm", {"request_id": result["request_id"]})[0] == 200
    result = _translated(service, "unspecified strategy")
    assert result["status"] == "rejected" and result["refusal"]["source"] == "model"
    assert not result["can_confirm"] and result["config"] is None
    calls = len(fake.calls)
    blocked = _translated(service, "evaluate equal weight through July 2026")
    assert len(fake.calls) == calls and blocked["refusal"]["source"] == "precheck"
    assert _call(service, "confirm", {"request_id": blocked["request_id"]})[1]["error"] == "TRANSLATOR_NOT_CONFIRMABLE"
    bad = _translated(service)
    assert bad["refusal"]["source"] == "gate" and bad["refusal"]["code"] == "OUT_OF_RANGE"
    assert bad["config"] is None and not bad["view"] and not bad["can_confirm"]
    warn = _translated(service, "equal-weight with 10 basis points")
    assert warn["needs_ack"] and warn["flags"][0]["level"] == "warn"
    assert _call(service, "confirm", {"request_id": warn["request_id"]})[0] == 409
    assert _call(service, "confirm", {"request_id": warn["request_id"], "ack_flags": True})[0] == 200
    counterfactual = _translated(service, "momentum top/bottom 27%")
    assert counterfactual["refusal"]["source"] == "counterfactual" and counterfactual["config"] is None
    assert any(f["level"] == "block" for f in counterfactual["flags"])
    assert _call(service, "confirm", {"request_id": counterfactual["request_id"]})[0] == 409
    assert oracle.calls > before + 5 and len(fake.calls) == 5
    assert len([r for r in _rows(root) if r["event"] == "translate"]) == 6
    assert not any(k == "mlx" or k.startswith("mlx.") for k in sys.modules)


def test_api_29_fake_contract_timeout_errors_and_gate_unavailable():
    with mock.patch.dict(os.environ, {"QB_TESTING": "0"}):
        try:
            api.FakeBackend()
            assert False
        except ValueError:
            pass
    for failure in ("TRANSLATOR_TIMEOUT", "TRANSLATOR_BACKEND_FAILED", "TRANSLATOR_BAD_OUTPUT", "TRANSLATOR_UNAVAILABLE"):
        service, fake, _, root = _api_fixture([failure])
        code, result = _call(service, "translate", {"idea": "equal-weight"})
        assert code == backend.ERROR_HTTP[failure] and result == {"error": failure, "message": failure}
        assert _rows(root)[0]["status"] == "error" and not service._cache
    oracle = mock.Mock()
    oracle.validate.side_effect = backend.TranslatorError("TRANSLATOR_UNAVAILABLE")
    service, _, _, root = _api_fixture([eq_cfg()], oracle=oracle)
    assert _call(service, "translate", {"idea": "equal-weight"})[1]["error"] == "TRANSLATOR_UNAVAILABLE"
    assert _rows(root)[0]["status"] == "error" and not service._cache


def test_api_34_feedback_validation_cache_log_lookup_and_note_limit():
    service, _, _, root = _api_fixture([eq_cfg()])
    rid = _translated(service)["request_id"]
    for body in ({"request_id": rid, "verdict": "right"}, {"request_id": rid},
                 {"request_id": rid, "verdict": "wrong", "note": 99}):
        assert _call(service, "feedback", body)[0] == 400
    for missing in ("0" * 16, "../bad", None, []):
        assert _call(service, "feedback", {"request_id": missing, "verdict": "wrong"})[0] == 410
    assert _call(service, "feedback", {"request_id": rid, "verdict": "wrong", "note": "错" * 250}) == (200, {"ok": True})
    # 新实例缓存为空，仍可从日志找到旧结果；确认不能从日志恢复。
    restart = api.API(root, loader=service.loader)
    assert _call(restart, "feedback", {"request_id": rid, "verdict": "wrong"})[0] == 200
    assert _call(restart, "confirm", {"request_id": rid})[0] == 410
    p = root / "data" / "ai_translator" / "feedback.jsonl"
    rows = [json.loads(line) for line in p.read_text().splitlines()]
    assert len(rows) == 2 and rows[0]["note"] == "错" * 200 and rows[1]["note"] == ""
    assert all(row["status"] == "pending_review" and row["verdict"] == "wrong" for row in rows)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_api_limits_fixed_local_key_and_daily_reset():
    day = [datetime.date(2026, 10, 1)]
    hit = mock.Mock(return_value=False)
    service, fake, _, _ = _api_fixture([eq_cfg(), eq_cfg()], cfg={"limits": {"daily_limit": 1}}, today=lambda: day[0], rate_hit=hit)
    assert _call(service, "status")[1]["limits"]["remaining_today"] == 1
    _translated(service)
    assert _call(service, "status")[1]["limits"]["remaining_today"] == 0
    assert _call(service, "translate", {"idea": "equal-weight"})[1]["error"] == "TRANSLATOR_DAILY_LIMIT"
    hit.assert_called_with("ai_translate", "local", 10, 60)
    day[0] += datetime.timedelta(days=1)
    assert _call(service, "status")[1]["limits"]["remaining_today"] == 1
    _translated(service)
    hit.return_value = True
    assert _call(service, "translate", {"idea": "equal-weight"})[1]["error"] == "TRANSLATOR_RATE_LIMITED"
    clock = [0.0]
    service, fake, _, _ = _api_fixture([eq_cfg()] * 11, clock=lambda: clock[0])
    for _ in range(10):
        _translated(service)
    assert _call(service, "translate", {"idea": "equal-weight"})[0] == 429
    assert len(fake.calls) == 10
    clock[0] = 60.0
    _translated(service)


def test_api_single_flight_returns_busy():
    service, fake, _, _ = _api_fixture([eq_cfg()])
    entered, release = threading.Event(), threading.Event()
    original = fake.translate
    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=3)
        return original(*args, **kwargs)
    fake.translate = slow
    replies = []
    thread = threading.Thread(target=lambda: replies.append(_call(service, "translate", {"idea": "equal-weight"})))
    thread.start()
    try:
        assert entered.wait(timeout=3)
        assert _call(service, "translate", {"idea": "equal-weight"})[1]["error"] == "TRANSLATOR_BUSY"
    finally:
        release.set()
        thread.join(timeout=3)
    assert not thread.is_alive() and replies[0][0] == 200 and len(fake.calls) == 1


def test_api_logging_failure_never_confirms_or_caches():
    service, _, _, _ = _api_fixture([eq_cfg()])
    with mock.patch.object(service.logs, "translate", side_effect=OSError("private path")):
        code, result = _call(service, "translate", {"idea": "equal-weight"})
        assert code == 502 and "private path" not in json.dumps(result) and not service._cache
    service, _, _, _ = _api_fixture([eq_cfg()])
    rid = _translated(service)["request_id"]
    with mock.patch.object(service.logs, "confirm", side_effect=OSError("private path")):
        assert _call(service, "confirm", {"request_id": rid})[0] == 502
    assert _call(service, "confirm", {"request_id": rid})[0] == 200


def test_api_disable_during_precheck_prevents_backend_creation():
    service, fake, factory, _ = _api_fixture([eq_cfg()])
    entered, release = threading.Event(), threading.Event()
    original = precheck.precheck
    def paused(*args):
        entered.set()
        assert release.wait(timeout=3)
        return original(*args)
    replies = []
    with mock.patch.object(precheck, "precheck", side_effect=paused):
        thread = threading.Thread(target=lambda: replies.append(_call(service, "translate", {"idea": "equal-weight"})))
        thread.start()
        try:
            assert entered.wait(timeout=3)
            service.loader.save({"enabled": False})
            assert _call(service, "status")[1]["state"] == "disabled"
        finally:
            release.set()
            thread.join(timeout=3)
    assert not thread.is_alive() and replies[0][1]["error"] == "TRANSLATOR_DISABLED"
    assert not factory.called and not fake.calls


def test_api_model_metadata_uses_request_snapshot():
    selected = {"version": "fixture-v1", "adapter_path": "adapters/first", "adapter_sha256": "d" * 64,
                "base_model_sha256": "e" * 64, "schema_sha256": "f" * 64}
    original_model = dict(selected)
    service, fake, _, root = _api_fixture([eq_cfg()], model_provider=lambda: dict(selected))
    original_translate = fake.translate
    def switch_during_request(*args, **kwargs):
        result = original_translate(*args, **kwargs)
        selected.update(version="fixture-v2", adapter_path="adapters/second", adapter_sha256="9" * 64)
        return result
    fake.translate = switch_during_request
    result = _translated(service)
    assert result["model_version"] == "fixture-v1" and _rows(root)[0]["model"] == original_model


def test_logstore_redacts_free_text_limits_raw_and_repairs_permissions():
    root = Path(tempfile.mkdtemp(prefix="qb_ai_logs_"))
    store = logstore.LogStore(root)
    now = datetime.datetime(2026, 1, 31, 23, 59, tzinfo=datetime.timezone.utc)
    store._now = lambda: now
    row = dict(request_id="a" * 16, backend_id="fake:v1", model={}, idea="equal-weight 192.0.2.88 ::1",
               precheck={}, raw_output="Cookie: fake-cookie; Authorization: fake-auth; token=fake-token; " + "x" * 5000,
               parse_mode="none", config={"password": "fake-password", "ip": "2001:db8::9"}, gate=None,
               flags=[], status="error", latency_ms=0)
    store.translate(**row)
    path = root / "data" / "ai_translator" / "log_202601.jsonl"
    assert len(json.loads(path.read_text())["raw_output"]) <= 4000
    text = path.read_text()
    for value in ("Cookie", "Authorization", "fake-cookie", "fake-auth", "fake-token", "fake-password", "192.0.2.88", "::1", "2001:db8::9"):
        assert value not in text, value
    before = path.read_bytes()
    os.chmod(path, 0o666)
    os.chmod(store.directory, 0o777)
    store.confirm(request_id="a" * 16, ack_flags=True)
    assert path.read_bytes().startswith(before)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(store.directory.stat().st_mode) == 0o700
    now += datetime.timedelta(minutes=1)
    store.confirm(request_id="a" * 16, ack_flags=False)
    assert (store.directory / "log_202602.jsonl").exists() and store.contains("a" * 16)
    assert not store.contains("b" * 16)
    store.feedback(request_id="a" * 16, note="::1 " * 100)
    note = json.loads((store.directory / "feedback.jsonl").read_text())["note"]
    assert len(note) <= 200 and "::1" not in note
    try:
        store.confirm(request_id="a" * 16, ack_flags=True, headers={})
        assert False
    except TypeError:
        pass


def test_logstore_concurrent_append_is_complete():
    root = Path(tempfile.mkdtemp(prefix="qb_ai_append_"))
    scripts = [
        "from src.ai_translator.logstore import LogStore\n"
        f"s = LogStore({str(root)!r})\n"
        f"for i in range(15): s.confirm(request_id={str(i)!r} + str(i), ack_flags=True)\n"
        for i in range(4)]
    workers = [subprocess.Popen([sys.executable, "-c", script], cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for script in scripts]
    for worker in workers:
        out, err = worker.communicate(timeout=10)
        assert worker.returncode == 0, (out, err)
    rows = _rows(root)
    assert len(rows) == 60 and all(row["event"] == "confirm" for row in rows)


def test_z_external_files_unchanged():
    assert external_fingerprint() == EXTERNAL_BEFORE


# ================================================================ S5：HTTP 接线（只提取方法，绝不导入 app）
def _http_fixture(method="POST", path="/api/ai/translate", *, headers=None, authenticated=True,
                  limited=False, body=b'{}'):
    from email.message import Message
    from io import BytesIO
    from types import SimpleNamespace
    from urllib.parse import urlparse
    from src import admin

    tree = ast.parse((REPO / "app.py").read_text(encoding="utf-8"))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_ai")
    # app.py 新增的主人通道方法也要摘进来；测试环境里 owner_access=None，_owner() 恒为 False
    owner_nodes = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                   and n.name in ("_owner_cfg", "_owner", "_local_or_owner", "_origin_ok")]
    events = []
    def record(name, result):
        def call(*args):
            events.append(name)
            return result(*args) if callable(result) else result
        return call
    audit = mock.Mock(side_effect=record("audit", None))
    origin = mock.Mock(side_effect=record("origin", admin.origin_ok))
    hit = mock.Mock(side_effect=record("rate", limited))
    namespace = {"admin": SimpleNamespace(is_local_request=admin.is_local_request,
                                         audit_blocked=audit, origin_ok=origin),
                 "ALLOWED_HOSTS": {"127.0.0.1:8765", "localhost:8765"},
                 "members": SimpleNamespace(hit=hit), "owner_access": None, "ROOT": None, "urlparse": urlparse}
    exec(compile(ast.Module(body=[node] + owner_nodes, type_ignores=[]), "app.py:_ai", "exec"), namespace)
    message = Message()
    for key, value in (headers if headers is not None else {
            "Host": "127.0.0.1:8765", "X-Requested-With": "dashboard",
            "Origin": "http://127.0.0.1:8765", "Content-Length": str(len(body))}).items():
        message[key] = value
    handler = SimpleNamespace(headers=message, rfile=mock.Mock(wraps=BytesIO(body)), close_connection=False,
                              _authed=mock.Mock(side_effect=record("auth", authenticated)),
                              _send=lambda code, obj, mime: (code, obj, mime),
                              _json=lambda obj, code=200: (code, obj, "application/json"))
    for n in owner_nodes:
        setattr(handler, n.name, __import__("types").MethodType(namespace[n.name], handler))

    def run():
        return namespace["_ai"](handler, method, urlparse(path))
    return SimpleNamespace(run=run, handler=handler, audit=audit, origin=origin, hit=hit, events=events)


def test_http_08_remote_matrix_audits_before_authentication():
    from src import admin
    from src.ai_translator import httpgate

    cases = [{"Host": "x.trycloudflare.com"}, {"Host": "outside.invalid:8765"}, {}]
    for header in sorted(admin.PROXY_HEADERS):
        for spelling in (header, header.title()):
            cases.append({"Host": "127.0.0.1:8765", spelling: "fixture"})
    for action in ("status", "translate", "confirm", "feedback"):
        method = "GET" if action == "status" else "POST"
        path = "/api/ai/" + action
        for headers in cases:
            decision = httpgate.check_access(method, is_local=admin.is_local_request(
                headers, {"127.0.0.1:8765", "localhost:8765"}))
            assert decision == (404, b"not found"), (path, headers)
            fixture = _http_fixture(method, path, headers=headers, authenticated=False, limited=True)
            with mock.patch.object(api, "handle") as handle:
                assert fixture.run() == (*decision, "text/plain"), (path, headers)
            fixture.audit.assert_called_once_with(path)
            fixture.handler._authed.assert_not_called()
            fixture.origin.assert_not_called()
            fixture.hit.assert_not_called()
            fixture.handler.rfile.read.assert_not_called()
            handle.assert_not_called()
            assert fixture.events == ["audit"]


def test_http_09_route_order_is_guard_admin_ai_share():
    source = (REPO / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for method in ("GET", "POST"):
        node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "do_" + method)
        text = ast.get_source_segment(source, node)
        assert text.index("self._guard()") < text.index("self._admin_" + method.lower()) < text.index('"/api/ai/"')
        assert text.index('"/api/ai/"') < text.index("self._share_gate(")
        if method == "POST":
            assert text.index('"/api/ai/"') < text.index("self._share_login(")
        branch = next(n for n in node.body if isinstance(n, ast.If) and '"/api/ai/"' in ast.get_source_segment(source, n.test))
        call = branch.body[0].value
        assert isinstance(branch.body[0], ast.Return) and call.func.attr == "_ai"
        assert call.args[0].value == method


def test_http_access_order_and_failure_responses():
    from src.ai_translator import httpgate

    for method, authenticated, marker, origin, limited, code, events in [
            ("GET", False, None, None, True, 401, ["auth"]),
            ("POST", False, None, "https://outside.invalid", True, 401, ["auth"]),
            ("POST", True, None, "https://outside.invalid", True, 401, ["auth"]),
            ("POST", True, "other", None, True, 401, ["auth"]),
            ("POST", True, "dashboard", "https://outside.invalid", True, 403, ["auth", "origin"]),
            ("POST", True, "dashboard", "http://127.0.0.1:8765", True, 429, ["auth", "origin", "rate"]),
            ("GET", True, None, None, True, 429, ["auth", "rate"])]:
        headers = {"Host": "127.0.0.1:8765", "Content-Length": "2"}
        if marker is not None:
            headers["X-Requested-With"] = marker
        if origin is not None:
            headers["Origin"] = origin
        path = "/api/ai/status" if method == "GET" else "/api/ai/translate"
        fixture = _http_fixture(method, path, headers=headers, authenticated=authenticated, limited=limited)
        with mock.patch.object(api, "handle") as handle:
            result = fixture.run()
        decision = httpgate.check_access(method, is_local=True, authenticated=authenticated,
                                         requested_with=marker == "dashboard",
                                         origin_ok=origin == "http://127.0.0.1:8765", rate_limited=limited)
        assert result[:2] == decision and result[0] == code, (method, headers, result)
        assert result[2] == ("text/plain" if code == 401 else "application/json")
        assert fixture.events == events
        fixture.audit.assert_not_called()
        fixture.handler.rfile.read.assert_not_called()
        handle.assert_not_called()
        if "rate" in events:
            fixture.hit.assert_called_once_with("ai_translate", "local", 10, 60)
        else:
            fixture.hit.assert_not_called()


def test_http_local_requests_forward_raw_body_and_handle_response():
    for action in ("status", "translate", "confirm", "feedback"):
        method = "GET" if action == "status" else "POST"
        path = "/api/ai/" + action
        body = '{"idea":"纯 HTTP 接线样例"}'.encode()
        fixture = _http_fixture(method, path, body=body)
        with mock.patch.object(api, "handle", return_value=(202, {"fixture": action})) as handle:
            assert fixture.run() == (202, {"fixture": action}, "application/json")
        handle.assert_called_once_with(method, path, body if method == "POST" else b"",
                                       is_local=True, authenticated=True, requested_with=True, origin_ok=True)
        fixture.hit.assert_called_once_with("ai_translate", "local", 10, 60)
        fixture.audit.assert_not_called()
        assert fixture.events == (["auth", "origin", "rate"] if method == "POST" else ["auth", "rate"])
        if method == "POST":
            fixture.handler.rfile.read.assert_called_once_with(len(body))
        else:
            fixture.handler.rfile.read.assert_not_called()


def test_http_disabled_status_uses_real_handle_with_temporary_root():
    service, _, factory, _ = _api_fixture(cfg={"enabled": False})
    fixture = _http_fixture("GET", "/api/ai/status", headers={"Host": "localhost:8765"})
    original = api.handle
    def temporary_handle(*args, **kwargs):
        return original(*args, service=service, **kwargs)
    with mock.patch.object(api, "handle", side_effect=temporary_handle) as handle:
        code, obj, _ = fixture.run()
    assert code == 200 and obj["state"] == "disabled" and obj["enabled"] is False
    handle.assert_called_once()
    factory.assert_not_called()
    fixture.origin.assert_not_called()


def test_http_invalid_content_length_never_reads_body_or_calls_handle():
    for length in ("bad", "-1", str(64 * 1024 + 1)):
        fixture = _http_fixture(headers={"Host": "127.0.0.1:8765", "X-Requested-With": "dashboard",
                                         "Content-Length": length})
        with mock.patch.object(api, "handle") as handle:
            code, obj, _ = fixture.run()
        assert code == 400 and obj["error"] == "TRANSLATOR_INPUT_INVALID"
        assert fixture.handler.close_connection is True
        fixture.handler.rfile.read.assert_not_called()
        handle.assert_not_called()


# ================================================================ S6：前端静态约束（不导入或启动 app.py）
def _ai_frontend_dictionary():
    source = (REPO / "dashboard" / "i18n.js").read_text(encoding="utf-8")
    start = source.index("const aiTranslations = ") + len("const aiTranslations = ")
    return json.JSONDecoder().raw_decode(source[start:])[0]


def test_frontend_safe_text_and_single_status_request():
    source = (REPO / "dashboard" / "ai_translator.js").read_text(encoding="utf-8")
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval", "localStorage"):
        assert banned not in source, banned
    assert "document.createElement" in source and ".textContent" in source
    assert source.count('request("status")') == 1
    assert 'response.status !== 200' in source
    assert 'nav.hidden = true' in source and 'nav.hidden = status.enabled !== true' in source
    assert "setInterval" not in source and "setTimeout" not in source
    assert '"X-Requested-With": "dashboard"' in source
    assert '"Content-Type": "application/json"' in source
    assert 'input.maxLength = state.status.limits.max_input_chars' in source
    assert 'note.maxLength = 200' in source
    confirm = source.split('async function confirm() {', 1)[1].split('async function feedback()', 1)[0]
    assert 'const body = { request_id: result.request_id };' in confirm
    assert 'body.ack_flags = state.ack' in confirm
    assert 'request("confirm", body)' in confirm
    assert 'body.config' not in confirm and 'config:' not in confirm


def test_frontend_static_whitelist_has_only_one_added_item():
    source = (REPO / "app.py").read_text(encoding="utf-8")
    original = ("/app.js", "/engines.js", "/i18n.js", "/analysis.js", "/skin.js")
    matches = [tuple(ast.literal_eval(node)) for node in ast.walk(ast.parse(source))
               if isinstance(node, ast.Tuple)
               and all(isinstance(value, ast.Constant) and isinstance(value.value, str) for value in node.elts)
               and "/app.js" in [value.value for value in node.elts]]
    assert matches == [original + ("/ai_translator.js",)], matches
    assert source.count('"/ai_translator.js"') == 1


def test_frontend_script_is_external_and_route_is_registered():
    from html.parser import HTMLParser

    class Scripts(HTMLParser):
        def __init__(self):
            super().__init__()
            self.scripts, self.current = [], None

        def handle_starttag(self, tag, attrs):
            if tag == "script":
                assert self.current is None
                self.current = [dict(attrs), ""]

        def handle_data(self, data):
            if self.current is not None:
                self.current[1] += data

        def handle_endtag(self, tag):
            if tag == "script":
                assert self.current is not None
                self.scripts.append(self.current)
                self.current = None

    parser = Scripts()
    parser.feed((REPO / "dashboard" / "index.html").read_text(encoding="utf-8"))
    assert parser.current is None
    assert [attrs.get("src", "").split("?")[0] for attrs, _ in parser.scripts] == [
        "/skin.js", "/app.js", "/analysis.js", "/engines.js", "/i18n.js", "/ai_translator.js"]
    assert all(attrs.get("src") and not body.strip() for attrs, body in parser.scripts)
    source = (REPO / "dashboard" / "app.js").read_text(encoding="utf-8")
    pages = re.search(r"const PAGES = (\[[^;]+\]);", source).group(1)
    assert "translator" in json.loads(pages)
    assert "translator: async () => window.AI_TRANSLATOR?.load()" in source
    js = (REPO / "dashboard" / "ai_translator.js").read_text(encoding="utf-8")
    assert 'nav.href = "#/translator"' in js and 'page.id = "p-translator"' in js


def test_frontend_all_design_keys_have_both_languages():
    translations = _ai_frontend_dictionary()
    required = {
        "ai.title", "ai.notice", "ai.badge.exp", "ai.input.ph", "ai.btn.translate", "ai.busy",
        "ai.btn.confirm", "ai.btn.cancel", "ai.btn.wrong", "ai.btn.copy", "ai.ack", "ai.confirmed.note",
        "ai.col.field", "ai.col.value", "ai.col.source", "ai.src.idea", "ai.src.default", "ai.src.mismatch",
        "ai.src.unchecked", "ai.flag.mismatch", "ai.flag.block", "ai.note.limits", "ai.off", "ai.cfg.bad",
        "ai.err.TRANSLATOR_BUSY", "ai.err.TRANSLATOR_TIMEOUT", "ai.err.TRANSLATOR_UNAVAILABLE",
        "ai.err.TRANSLATOR_DAILY_LIMIT", "ai.err.TRANSLATOR_BAD_OUTPUT"
    } | {"ai.refusal." + code for code in plainview.REJECT_CODES}
    assert required <= translations.keys(), required - translations.keys()
    for key, pair in translations.items():
        assert key.startswith("ai.") and isinstance(pair, list) and len(pair) == 2, key
        assert all(isinstance(value, str) and value.strip() for value in pair), key
    assert translations["ai.off"] == [
        "AI 翻译未开启。可在 data/ai_translator.json 中打开(仅本机)。",
        "AI translation is off. It can be turned on in data/ai_translator.json (local machine only)."]
    for code, pair in plainview.REJECT_MSG.items():
        assert translations["ai.refusal." + code] == list(pair)


def test_frontend_every_used_translation_key_exists():
    translations = _ai_frontend_dictionary()
    source = (REPO / "dashboard" / "ai_translator.js").read_text(encoding="utf-8")
    literal_keys = set(re.findall(r'[\"\'`](ai\.[A-Za-z0-9_.]+)[\"\'`]', source))
    assert literal_keys and literal_keys <= translations.keys(), literal_keys - translations.keys()
    # 两种动态前缀的实际取值分别来自后端错误码与拒绝码，不能只检查字面量。
    dynamic_keys = {"ai.err." + code for code in backend.ERROR_HTTP}
    dynamic_keys |= {"ai.refusal." + code for code in plainview.REJECT_CODES}
    assert dynamic_keys <= translations.keys(), dynamic_keys - translations.keys()
    for key, pair in plainview.FLAG_TEXT.items():
        assert translations[key] == list(pair)
    i18n_source = (REPO / "dashboard" / "i18n.js").read_text(encoding="utf-8")
    assert 'window.AI_I18N = ' in i18n_source
    assert '.ai-translator, .ai-translator-nav' in i18n_source
    assert 'new Event("dashboard-language-change")' in i18n_source
    assert '"dashboard-language-change", render' in source


# ================================================================ S7a：登记、切换与真实 worker 的假模型注入
def _script(name, *args):
    return subprocess.run([sys.executable, "-B", str(REPO / "scripts" / name), *map(str, args)],
                          capture_output=True, text=True, timeout=30, env={**os.environ, "QB_TESTING": "1"})


def _registry_fixture(root, versions=("fake-v1", "fake-v2")):
    base = (root / "ai").resolve()
    (base / ".venv" / "bin").mkdir(parents=True)
    (base / ".venv" / "bin" / "python").symlink_to(sys.executable)
    (base / "models" / "fake").mkdir(parents=True)
    (base / "adapters" / "fake" / "nested").mkdir(parents=True)
    (base / "models" / "fake" / "model.safetensors").write_bytes(b"dummy weights")
    (base / "adapters" / "fake" / "adapter.safetensors").write_bytes(b"dummy adapter")
    (base / "adapters" / "fake" / "nested" / "config.json").write_text('{}')
    evaluation = b'{"run_id":"fixture-run","status":"fixture-evaluation","private_metrics":{"x":1}}\n'
    (base / "evaluation.jsonl").write_bytes(evaluation)
    target = base / "MODEL_REGISTRY.json"
    for version in versions:
        out = _script("register_translator_model.py", version, "adapters/fake", "fixture-run", "fixture-evaluation",
                      "--ai-layer-dir", base, "--base-model", "models/fake", "--eval-file", "evaluation.jsonl")
        assert out.returncode == 0, (out.stdout, out.stderr)
    assert (base / "evaluation.jsonl").read_bytes() == evaluation
    return base, target


def _activate(target, version):
    obj = registry.read_registry(target)
    obj["active_version"] = version
    registry.atomic_write(target, obj)


def _switch_args(root, base, *, mode="normal"):
    document = root / "output.json"
    document.write_text(json.dumps(eq_cfg()))
    gate_script = root / "gate.py"
    gate_script.write_text("import json,sys\njson.load(sys.stdin)\nprint(json.dumps({"
                           "'ok': True, 'issues': [], 'reject_codes': [], 'modules': [], 'denied': 0}))\n")
    command = _command(root / "pids", mode, "--document", str(document),
                       "--model-dir", str(base / "models" / "fake"),
                       "--adapter-dir", str(base / "adapters" / "fake"))
    return ["--root", root, "--ai-layer-dir", base, "--statarb-src-dir", root,
            "--test-command", json.dumps(command), "--test-gate-script", gate_script]


def _check_fixture_pids(root):
    if (root / "pids").exists():
        for pid in map(int, (root / "pids").read_text().splitlines()):
            _fake_pids.add(pid)
            _assert_pid_gone(pid)


def test_registry_30_active_hashes_expect_and_weight_cache():
    with tempfile.TemporaryDirectory(prefix="qb_registry_") as tmp:
        root = Path(tmp)
        base, target = _registry_fixture(root)
        reader = registry.Registry(base)
        assert reader() is None and reader.problem == "MODEL_UNAVAILABLE"
        _activate(target, "fake-v1")
        record = reader()
        assert record["version"] == "fake-v1" and not record["meets_launch_gate"]
        assert record["status"] == "experimental" and record["criteria_met"] == {}
        assert "private_metrics" not in repr(record)
        assert record["eval_ref"]["line_sha256"] == hashlib.sha256((base / "evaluation.jsonl").read_bytes()).hexdigest()
        wrong = registry.Registry(base, "fake-v2")
        assert wrong() is None and wrong.problem == "MODEL_VERSION_MISMATCH"
        obj = registry.read_registry(target)
        obj["versions"][1]["adapter_sha256"] = "0" * 64
        registry.atomic_write(target, obj)
        assert reader()["version"] == "fake-v1"  # 无效非 active 版本不会被选中。
        adapter = base / "adapters" / "fake"
        expected = hashlib.sha256("".join(p.relative_to(adapter).as_posix() + "\0" +
                                          hashlib.sha256(p.read_bytes()).hexdigest() + "\n"
                                          for p in sorted(adapter.rglob("*")) if p.is_file()).encode()).hexdigest()
        assert registry.adapter_sha256(adapter) == expected
        for name in ("new", "nested/config.json"):
            p = adapter / name
            before = p.read_bytes() if p.exists() else None
            p.write_bytes(b"tampered")
            assert reader() is None and reader.problem == "HASH_MISMATCH"
            if before is None:
                p.unlink()
            else:
                p.write_bytes(before)
        weight = base / "models" / "fake" / "model.safetensors"
        cache = registry.WeightHashes()
        with mock.patch.object(registry, "file_sha256", wraps=registry.file_sha256) as hashing:
            cache.digest(weight)
            cache.digest(weight)
            assert hashing.call_count == 1
            weight.write_bytes(b"replaced weights")
            cache.digest(weight)
            assert hashing.call_count == 2
        assert reader() is None and reader.problem == "HASH_MISMATCH"
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        for bad in ("../outside", "/absolute"):
            try:
                registry.relative_path(base, bad)
                raise AssertionError("path traversal accepted")
            except registry.RegistryError:
                pass


def test_register_preserves_active_and_rejects_duplicate_or_ambiguous_eval():
    with tempfile.TemporaryDirectory(prefix="qb_register_") as tmp:
        base, target = _registry_fixture(Path(tmp), ("fake-v1",))
        _activate(target, "fake-v1")
        original = target.read_bytes()
        command = ["register_translator_model.py", "fake-v1", "adapters/fake", "fixture-run", "fixture-evaluation",
                   "--ai-layer-dir", base, "--base-model", "models/fake", "--eval-file", "evaluation.jsonl"]
        assert _script(*command).returncode != 0
        assert target.read_bytes() == original
        command[1] = "fake-v2"
        assert _script(*command).returncode == 0
        assert registry.read_registry(target)["active_version"] == "fake-v1"
        original = target.read_bytes()
        command[1] = "fake-v3"
        p = base / "evaluation.jsonl"
        p.write_bytes(p.read_bytes() * 2)
        evaluation = p.read_bytes()
        assert _script(*command).returncode != 0
        assert target.read_bytes() == original and p.read_bytes() == evaluation


def test_switch_31_dryrun_success_rollback_failures_and_lock():
    with tempfile.TemporaryDirectory(prefix="qb_set_") as tmp:
        root = Path(tmp)
        base, target = _registry_fixture(root)
        args = _switch_args(root, base)
        original = target.read_bytes()
        out = _script("set_translator_model.py", "--rollback", *args)
        assert out.returncode != 0 and target.read_bytes() == original
        with registry.FileLock(paths.lock_path(root)):
            out = _script("set_translator_model.py", "fake-v1", *args)
            assert out.returncode != 0 and "LOCKED" in out.stderr
        assert target.read_bytes() == original
        adapter = base / "adapters" / "fake" / "adapter.safetensors"
        adapter.write_bytes(b"wrong")
        out = _script("set_translator_model.py", "fake-v1", *args)
        assert out.returncode != 0 and "HASH_MISMATCH" in out.stderr and target.read_bytes() == original
        adapter.write_bytes(b"dummy adapter")
        weight = base / "models" / "fake" / "model.safetensors"
        weight.write_bytes(b"bad base model")
        out = _script("set_translator_model.py", "fake-v1", *args)
        assert out.returncode != 0 and "HASH_MISMATCH" in out.stderr and target.read_bytes() == original
        weight.write_bytes(b"dummy weights")
        ev = base / "evaluation.jsonl"
        original_ev = ev.read_bytes()
        ev.write_bytes(original_ev.replace(b'"x":1', b'"x":2'))
        out = _script("set_translator_model.py", "fake-v1", *args)
        assert out.returncode != 0 and "EVAL_HASH_MISMATCH" in out.stderr and target.read_bytes() == original
        ev.write_bytes(original_ev)
        out = _script("set_translator_model.py", "fake-v1", *_switch_args(root, base, mode="bad_output"))
        assert out.returncode != 0 and "SMOKE_PIPELINE_FAILED" in out.stderr and target.read_bytes() == original
        _check_fixture_pids(root)
        args = _switch_args(root, base)
        gate_script = root / "gate.py"
        gate_script.write_text("raise RuntimeError('temporary gate failure')\n")
        out = _script("set_translator_model.py", "fake-v1", *args)
        assert out.returncode != 0 and target.read_bytes() == original
        args = _switch_args(root, base)
        # 冒烟不衡量内容正确性：校验器说非法，也算完成了闸门调用。
        gate_script.write_text("import json,sys\njson.load(sys.stdin)\nprint(json.dumps({"
                               "'ok':True,'issues':[{'code':'OUT_OF_RANGE','path':'x','message':'fixture'}],"
                               "'reject_codes':['OUT_OF_RANGE'],'modules':[],'denied':0}))\n")
        out = _script("set_translator_model.py", "fake-v1", "--dry-run", *args)
        assert out.returncode == 0, (out.stdout, out.stderr)
        assert '"gate_calls": 10' in out.stdout and target.read_bytes() == original
        assert not (root / "data" / "admin_audit.jsonl").exists()
        for options, expected in ((["fake-v1"], "fake-v1"), (["fake-v2"], "fake-v2"), (["--rollback"], "fake-v1")):
            out = _script("set_translator_model.py", *options, *args)
            assert out.returncode == 0, (out.stdout, out.stderr)
            assert "实验版本" in out.stdout
            obj = registry.read_registry(target)
            assert obj["active_version"] == expected and obj["history"][-1]["to"] == expected
            _check_fixture_pids(root)
        assert len(obj["history"]) == 3 and obj["history"][-1]["via"] == "rollback"
        audits = [json.loads(line) for line in (root / "data" / "admin_audit.jsonl").read_text().splitlines()]
        assert len(audits) == 3 and all(r["action"] == "ai_translator_model_switched" for r in audits)
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert ev.read_bytes() == original_ev


def _normalize_smoke(text):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold() if c.isalnum())


def _smoke_overlaps(cases, source_files, threshold=0.65):
    # 只接收显式传入的文件；测试中源材料全部是在临时目录新写的模拟样本。
    def grams(text):
        return {text[i:i + 3] for i in range(max(1, len(text) - 2))}
    overlaps = []
    for source in source_files:
        for line in Path(source).read_text().splitlines():
            item = json.loads(line)
            text = item.get("idea")
            if text is None:
                text = next(m["content"] for m in item["messages"] if m["role"] == "user")
            other = _normalize_smoke(text)
            b = grams(other)
            for i, case in enumerate(cases):
                current = _normalize_smoke(case["idea"])
                a = grams(current)
                if current == other or len(a & b) / len(a | b) > threshold:
                    overlaps.append((i, str(source)))
    return overlaps


def test_smoke_32_normalization_ngram_and_temp_sources_only():
    cases = json.loads((REPO / "tests" / "ai_translator_smoke.json").read_text())
    assert len(cases) == 10 and sum(c["should_reject"] for c in cases) == 3
    normalized = [_normalize_smoke(c["idea"]) for c in cases]
    assert len(set(normalized)) == 10
    with tempfile.TemporaryDirectory(prefix="qb_smoke_overlap_") as tmp:
        sources = [Path(tmp) / name for name in ("training.jsonl", "validation.jsonl", "legacy.jsonl")]
        for p in sources:
            p.write_text(json.dumps({"idea": "unrelated synthetic source used only to test the checker"}) + "\n")
        assert _smoke_overlaps(cases, sources) == []
        sources[0].write_text(json.dumps({"idea": cases[0]["idea"].upper() + "！"}) + "\n")
        assert _smoke_overlaps(cases, sources)[0][0] == 0
        sources[0].write_text(json.dumps({"messages": [{"role": "user", "content": cases[1]["idea"] + " minor note"}]}) + "\n")
        assert _smoke_overlaps(cases, sources)[0][0] == 1


def _real_worker_fixture(root):
    base = root / "ai"
    (base / "models" / "dummy").mkdir(parents=True)
    (base / "adapters" / "dummy").mkdir(parents=True)
    (root / "research" / "statarb").mkdir(parents=True)
    (root / "research" / "statarb" / "__init__.py").write_text("")
    schema = root / "research" / "statarb" / "schema.py"
    schema.write_text("# 临时校验器模块，仅供协议测试\n")
    (base / "ai_lib.py").write_text(f"import sys\nsys.path.insert(0, {str(root / 'research')!r})\n"
                                   "from statarb import schema\nSYSTEM_PROMPT = 'temporary prompt'\n"
                                   "def make_messages(idea):\n return [{'role':'system','content':SYSTEM_PROMPT},"
                                   " {'role':'user','content':idea}]\n")
    module = base / "fake_model.py"
    module.write_text("import json,time\nclass Tokenizer:\n"
                      " def apply_chat_template(self, messages, *, add_generation_prompt, tokenize):\n"
                      "  assert add_generation_prompt is True and tokenize is False\n"
                      "  assert messages[0]['content'] == 'temporary prompt'\n"
                      "  return messages[1]['content']\n"
                      "def load(path, *, adapter_path):\n return object(), Tokenizer()\n"
                      "def make_sampler(*, temp):\n assert temp == 0.0\n return 'greedy'\n"
                      "def generate(model, tok, *, prompt, max_tokens, sampler, verbose):\n"
                      " assert max_tokens == 420 and sampler == 'greedy' and verbose is False\n"
                      " if prompt == 'hang': time.sleep(600)\n"
                      " if prompt == 'duplicate': return '{\"x\":1,\"x\":2}'\n"
                      " if prompt == 'nonfinite': return '{\"x\":NaN}'\n"
                      " if prompt == 'wrapped': return 'prefix {\"x\":1}'\n"
                      " print('model chatter redirected to stderr')\n"
                      " return json.dumps({'idea': prompt})\n")
    return [sys.executable, "-I", "-B", str(REPO / "scripts" / "ai_translator_worker.py"),
            "--ai-layer-dir", str(base), "--model", str(base / "models" / "dummy"),
            "--adapter", str(base / "adapters" / "dummy"), "--fake-model-module", str(module)]


def _worker_line(p):
    assert select.select([p.stdout], [], [], 5)[0], "worker 没有及时发出协议行"
    return p.stdout.readline()


def test_worker_injected_model_strict_protocol_eof_and_guard():
    with tempfile.TemporaryDirectory(prefix="qb_real_worker_") as tmp:
        command = _real_worker_fixture(Path(tmp))
        p = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            ready = json.loads(_worker_line(p))
            assert ready["op"] == "ready", ready
            assert ready["schema_sha256"] == hashlib.sha256((Path(tmp) / "research" / "statarb" / "schema.py").read_bytes()).hexdigest()
            for idea in ("hello", "duplicate", "nonfinite", "wrapped"):
                p.stdin.write((json.dumps({"op": "translate", "id": "one", "idea": idea}) + "\n").encode())
                p.stdin.flush()
                reply = json.loads(_worker_line(p))
                assert reply["op"] == "result" and reply["id"] == "one" and reply["ok"] is True
                assert reply["parse_mode"] == ("strict" if idea == "hello" else "none")
            p.stdin.write(b'{"op":"translate","id":"hang","idea":"hang"}\n')
            p.stdin.flush()
            time.sleep(0.1)
            p.stdin.close()
            p.wait(timeout=3)  # 推理未返回也必须因 EOF 退出。
            _assert_pid_gone(p.pid)
        finally:
            if p.poll() is None:
                p.kill()
            p.wait(timeout=3)
            for stream in (p.stdin, p.stdout, p.stderr):
                stream.close()
        env = dict(os.environ)
        env.pop("QB_TESTING", None)
        denied = subprocess.run(command, input=b"", capture_output=True, timeout=5, env=env)
        assert denied.returncode != 0 and b"requires QB_TESTING=1" in denied.stderr
        source = (REPO / "scripts" / "ai_translator_worker.py").read_text().lower()
        for banned in ("seal" + "ed", "test" + "2", "trials" + ".jsonl", "runs" + ".jsonl"):
            assert banned not in source
        ast.parse(source)


def test_worker_parent_death_with_open_stdin_and_hung_model():
    linux = sys.platform.startswith("linux")
    if linux:
        assert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0
    try:
        with tempfile.TemporaryDirectory(prefix="qb_worker_parent_") as tmp:
            command = _real_worker_fixture(Path(tmp))
            read_fd, write_fd = os.pipe()
            code = ("import subprocess,sys,time\n"
                    f"p=subprocess.Popen({command!r}, stdin={read_fd}, stdout=sys.stdout, stderr=sys.stderr, start_new_session=True)\n"
                    "print(p.pid, flush=True)\nwhile True: time.sleep(60)\n")
            parent = subprocess.Popen([sys.executable, "-I", "-B", "-c", code], pass_fds=(read_fd,),
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            os.close(read_fd)
            pid = None
            try:
                pid = int(_worker_line(parent))
                ready = json.loads(_worker_line(parent))
                assert ready["op"] == "ready", ready
                os.write(write_fd, b'{"op":"translate","id":"h","idea":"hang"}\n')
                time.sleep(0.1)
                parent.kill()
                parent.wait(timeout=3)
                # 根测试进程仍持有 write_fd，stdin 不会 EOF，单独检验 ppid 分支。
                _assert_pid_gone(pid, timeout=3)
                _fake_pids.add(pid)
            finally:
                os.close(write_fd)
                if parent.poll() is None:
                    parent.kill()
                parent.communicate(timeout=5)
                if pid is not None:
                    _assert_pid_gone(pid, timeout=3)
    finally:
        if linux:
            ctypes.CDLL(None).prctl(36, 0, 0, 0, 0)


def test_worker_import_file_guards_and_eof_during_loading():
    for mode in ("import", "file", "loading"):
        with tempfile.TemporaryDirectory(prefix="qb_worker_guards_") as tmp:
            root = Path(tmp)
            command = _real_worker_fixture(root)
            if mode == "import":
                (root / "research" / "statarb" / "unexpected.py").write_text("")
                with (root / "ai" / "ai_lib.py").open("a") as stream:
                    stream.write("import statarb.unexpected\n")
            else:
                module = Path(command[-1])
                original = module.read_text()
                if mode == "file":
                    material = root / "ai" / "data" / "synthetic.jsonl"
                    material.parent.mkdir()
                    material.write_text('{"synthetic":true}\n')
                    original += f"\nopen({str(material)!r}).read()\n"
                else:
                    original += "\ntime.sleep(600)\n"
                module.write_text(original)
            p = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                if mode == "loading":
                    time.sleep(0.15)
                    assert p.poll() is None
                    p.stdin.close()
                    assert p.wait(timeout=3) == 0
                else:
                    response = json.loads(_worker_line(p))
                    assert response["op"] == "fatal", response
                    assert p.wait(timeout=3) != 0
                _assert_pid_gone(p.pid)
            finally:
                if p.poll() is None:
                    p.kill()
                p.wait(timeout=3)
                for stream in (p.stdin, p.stdout, p.stderr):
                    stream.close()


def test_switch_cli_uses_config_save_audit_and_immediate_reap():
    with tempfile.TemporaryDirectory(prefix="qb_toggle_") as tmp:
        root = Path(tmp)
        base, target = _registry_fixture(root)
        _activate(target, "fake-v1")
        assert _script("ai_translator_switch.py", "status", "--root", root).returncode == 0
        assert not (root / "data" / "ai_translator.json").exists()
        out = _script("ai_translator_switch.py", "on", "--root", root,
                      "--ai-layer-dir", base, "--statarb-src-dir", root)
        assert out.returncode == 0, out.stderr
        loader = config.ConfigLoader(root)
        state = loader.load()
        assert state.enabled and state.backend == "local_lora" and state.cfg["timeouts"] == config.defaults()["timeouts"]
        owner = local_lora.Supervisor(state.cfg, root=root, model_provider=registry.Registry(base), config_loader=loader,
                                      test_command=_command(root / "pids"))
        try:
            assert owner.translate("switch lifecycle")["config"] is not None
            pid = owner._proc.pid
            out = _script("ai_translator_switch.py", "off", "--root", root)
            assert out.returncode == 0 and "立即回收" in out.stdout
            _wait_until(lambda: owner._proc is None, timeout=3)
            _assert_pid_gone(pid)
            assert owner.status()["state"] == "disabled"
        finally:
            owner.close()
        saved = config.ConfigLoader(root).load()
        assert not saved.enabled and saved.cfg["ai_layer_dir"] == str(base)
        assert stat.S_IMODE(loader.path.stat().st_mode) == 0o600
        rows = [json.loads(line) for line in (root / "data" / "admin_audit.jsonl").read_text().splitlines()]
        assert [r["action"] for r in rows] == ["ai_translator_toggle", "ai_translator_toggle"]
        _check_fixture_pids(root)


def test_api_local_lora_registry_fake_worker_full_pipeline_and_status_limits():
    with tempfile.TemporaryDirectory(prefix="qb_api_local_") as tmp:
        root = Path(tmp)
        base, target = _registry_fixture(root)
        loader = config.ConfigLoader(root)
        loader.save({"enabled": True, "backend": "local_lora", "ai_layer_dir": str(base)})
        document = root / "document.json"
        document.write_text(json.dumps(eq_cfg()))
        real_supervisor = local_lora.Supervisor
        class InjectedSupervisor(real_supervisor):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs, test_command=_command(root / "pids", "normal", "--document", str(document)))
        service = api.API(root, loader=loader, oracle=AlwaysLegal())
        # 保留默认 API 工厂和真实 Registry，只在 Supervisor 启动命令处注入假 worker。
        with mock.patch.object(local_lora, "Supervisor", InjectedSupervisor):
            code, status = _call(service, "status")
            assert code == 200 and status["state"] == "unavailable" and status["limits"]["max_input_chars"] == 500
            assert not (root / "pids").exists()
            _activate(target, "fake-v1")
            result = _translated(service)
            assert result["backend_id"] == "local_lora:fake-v1" and result["model_version"] == "fake-v1"
            assert _call(service, "confirm", {"request_id": result["request_id"], "ack_flags": True})[0] == 200
            rows = _rows(root)
            assert rows[0]["model"]["adapter_sha256"] == registry.Registry(base)()["adapter_sha256"]
            _activate(target, "fake-v2")
            assert _call(service, "status")[1]["model"]["version"] == "fake-v2"
            assert service._backend._proc is None
            assert _translated(service)["model_version"] == "fake-v2"
            loader.save({"enabled": False})
            # 同步中 isinstance 需真实类；恢复类后处理配置变化。
        code, status = _call(service, "status")
        assert code == 200 and status["state"] == "disabled" and status["limits"]["max_input_chars"] == 500
        service.close()
        _check_fixture_pids(root)


def test_api_local_registry_failures_and_disabled_never_start_worker():
    with tempfile.TemporaryDirectory(prefix="qb_api_registry_fail_") as tmp:
        root = Path(tmp)
        base, target = _registry_fixture(root)
        _activate(target, "fake-v1")
        for mode, problem in (("expect", "MODEL_VERSION_MISMATCH"), ("hash", "HASH_MISMATCH"),
                              ("missing", "MODEL_UNAVAILABLE"), ("disabled", None)):
            obj = registry.read_registry(target)
            obj["active_version"] = None if mode == "missing" else "fake-v1"
            obj["versions"][0]["adapter_sha256"] = "0" * 64 if mode == "hash" else registry.adapter_sha256(base / "adapters" / "fake")
            registry.atomic_write(target, obj)
            loader = config.ConfigLoader(root)
            loader.save({"enabled": True, "backend": "disabled" if mode == "disabled" else "local_lora",
                         "ai_layer_dir": str(base), "expect_model_version": "fake-v2" if mode == "expect" else None})
            service = api.API(root, loader=loader)
            try:
                with mock.patch("subprocess.Popen", side_effect=AssertionError("must not start")):
                    code, status = _call(service, "status")
                    assert code == 200 and status["problem"] == problem, status
                    assert status["state"] == ("disabled" if mode == "disabled" else "unavailable")
                    assert status["limits"]["max_input_chars"] == 500
                    code, response = _call(service, "translate", {"idea": "equal-weight reference"})
                    assert code == 503 and response["error"] == ("TRANSLATOR_DISABLED" if mode == "disabled" else "TRANSLATOR_UNAVAILABLE")
            finally:
                service.close()


# ================================================================ S8：只读反馈导出
def _feedback_exporter():
    import importlib.util
    spec = importlib.util.spec_from_file_location("feedback_exporter", REPO / "scripts" / "export_ai_feedback.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _feedback_jsonl(path, rows):
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def _feedback_records(path):
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["event"] == "export_metadata" and "脱敏" in rows[0]["notice"] and "截断" in rows[0]["notice"]
    return rows[1:]


def test_feedback_export_join_deduplicate_atomic_permissions_and_source_bytes():
    exporter = _feedback_exporter()
    with tempfile.TemporaryDirectory(prefix="qb_feedback_") as tmp:
        directory = Path(tmp).resolve()
        feedback = directory / "feedback.jsonl"
        first = {"request_id": "a" * 16, "status": "pending_review", "ts": "feedback time",
                 "verdict": "wrong", "note": "首次备注"}
        _feedback_jsonl(feedback, [first, {**first, "note": "重复备注"},
                                   {"request_id": "b" * 16, "status": "reviewed"}])
        translate = {"event": "translate", "request_id": first["request_id"], "ts": "translate time",
                     "idea": "独立构造的待审想法", "raw_output": "模型文本", "config": {"sample": 7},
                     "backend_id": "local_lora:fixture", "model": {"version": "fixture", "adapter_sha256": "a" * 64}}
        _feedback_jsonl(directory / "log_202609.jsonl", [translate])
        _feedback_jsonl(directory / "log_202610.jsonl", [{**translate, "idea": "重复上下文"},
                                                        {"event": "confirm", "request_id": first["request_id"]}])
        sources = {p: (p.read_bytes(), stat.S_IMODE(p.stat().st_mode)) for p in directory.glob("*.jsonl")}
        day = datetime.date(2026, 10, 21)
        count, target = exporter.export_feedback(directory, today=day)
        assert count == 1 and target == directory / "review" / "pending_20261021.jsonl"
        [row] = _feedback_records(target)
        for key in ("idea", "raw_output", "config", "model", "backend_id"):
            assert row[key] == translate[key]
        assert row["ts"] == first["ts"] and row["translate_ts"] == translate["ts"]
        assert row["note"] == first["note"] and row["status"] == "pending_review"
        assert row["reviewed_by_human"] is False and row["gold"] is None and row["reviewer_note"] == ""
        assert row["missing_context"] is False
        before_export = target.read_bytes()
        target.chmod(0o666)
        target.parent.chmod(0o777)
        real_replace = exporter.os.replace

        def replace(source, destination):
            assert Path(source).parent == target.parent and Path(destination) == target
            assert stat.S_IMODE(Path(source).stat().st_mode) == 0o600
            assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
            assert target.read_bytes() == before_export
            real_replace(source, destination)

        with mock.patch.object(exporter.os, "replace", side_effect=replace) as replaced:
            assert exporter.export_feedback(directory, today=day) == (1, target)
            replaced.assert_called_once()
        assert target.read_bytes() == before_export
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
        assert list(target.parent.iterdir()) == [target]
        assert {p: (p.read_bytes(), stat.S_IMODE(p.stat().st_mode)) for p in sources} == sources
        assert set(directory.iterdir()) == set(sources) | {target.parent}


def test_feedback_export_missing_context_including_confirm_and_shadow():
    exporter = _feedback_exporter()
    with tempfile.TemporaryDirectory(prefix="qb_feedback_missing_") as tmp:
        directory = Path(tmp).resolve()
        rows = [{"request_id": rid, "status": "pending_review", "note": "保留备注"} for rid in ("absent", "confirm", "shadow")]
        _feedback_jsonl(directory / "feedback.jsonl", rows)
        _feedback_jsonl(directory / "log_fixture.jsonl", [
            {"request_id": "confirm", "event": "confirm"}, {"request_id": "shadow", "event": "shadow", "idea": "不可混入"}])
        count, target = exporter.export_feedback(directory)
        assert count == 3
        exported = _feedback_records(target)
        assert [row["request_id"] for row in exported] == [row["request_id"] for row in rows]
        for row in exported:
            assert row["missing_context"] is True and row["note"] == "保留备注"
            assert all(row[key] is None for key in ("idea", "raw_output", "config", "model", "backend_id"))
            assert row["reviewed_by_human"] is False and row["gold"] is None and row["reviewer_note"] == ""


def test_feedback_export_empty_missing_or_no_pending_feedback():
    exporter = _feedback_exporter()
    for content in (None, b"", b'\n', b'{"status":"reviewed","request_id":"old"}\n'):
        with tempfile.TemporaryDirectory(prefix="qb_feedback_empty_") as tmp:
            directory = Path(tmp).resolve()
            feedback = directory / "feedback.jsonl"
            if content is not None:
                feedback.write_bytes(content)
            count, target = exporter.export_feedback(directory)
            assert count == 0 and _feedback_records(target) == []
            assert stat.S_IMODE(target.stat().st_mode) == 0o600
            assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
            assert feedback.exists() is (content is not None)
            if content is not None:
                assert feedback.read_bytes() == content


def test_feedback_export_logstore_redaction_and_truncation_preserved():
    exporter = _feedback_exporter()
    with tempfile.TemporaryDirectory(prefix="qb_feedback_logstore_") as tmp:
        store = logstore.LogStore(Path(tmp))
        rid = "c" * 16
        store.translate(request_id=rid, backend_id="fake", model={"version": "fixture", "adapter_sha256": "d" * 64},
                        idea="export fixture 192.0.2.51", precheck={}, raw_output="192.0.2.52 " + "字" * 4100,
                        parse_mode="strict", config={"sample": 1}, gate={}, flags=[], status="ok", latency_ms=1)
        store.feedback(request_id=rid, note="192.0.2.53 " + "错" * 210)
        [translate] = [json.loads(line) for p in store.directory.glob("log_*.jsonl") for line in p.read_text().splitlines()]
        note = json.loads((store.directory / "feedback.jsonl").read_text())["note"]
        count, target = exporter.export_feedback(store.directory)
        [row] = _feedback_records(target)
        assert count == 1 and row["idea"] == translate["idea"] and "[redacted]" in row["idea"]
        assert row["raw_output"] == translate["raw_output"] and len(row["raw_output"]) == 4000
        assert row["note"] == note and len(note) == 200
        assert row["model"] == translate["model"] and row["config"] == translate["config"]


def test_feedback_export_cli_count_path_and_no_idea_output():
    with tempfile.TemporaryDirectory(prefix="qb_feedback_cli_") as tmp:
        directory = Path(tmp).resolve()
        _feedback_jsonl(directory / "feedback.jsonl", [{"request_id": "cli", "status": "pending_review"}])
        _feedback_jsonl(directory / "log_fixture.jsonl", [{"event": "translate", "request_id": "cli", "idea": "PRIVATE_IDEA_FOR_EXPORT"}])
        result = subprocess.run([sys.executable, "-B", str(REPO / "scripts" / "export_ai_feedback.py"), "--data-dir", tmp],
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0 and result.stderr == "", result.stderr
        [target] = list((directory / "review").glob("pending_????????.jsonl"))
        assert "已导出 1 条反馈" in result.stdout and str(target) in result.stdout
        assert "PRIVATE_IDEA_FOR_EXPORT" not in result.stdout + result.stderr
        assert _feedback_records(target)[0]["idea"] == "PRIVATE_IDEA_FOR_EXPORT"


def test_feedback_export_invalid_input_keeps_previous_export_and_no_private_output():
    import io
    exporter = _feedback_exporter()
    with tempfile.TemporaryDirectory(prefix="qb_feedback_invalid_") as tmp:
        directory = Path(tmp).resolve()
        _, target = exporter.export_feedback(directory)
        before = target.read_bytes()
        for content in (b'PRIVATE_BROKEN_JSON\n', b'[]\n', b'{"status":"pending_review"}\n'):
            (directory / "feedback.jsonl").write_bytes(content)
            error = io.StringIO()
            with contextlib.redirect_stderr(error):
                assert exporter.main(["--data-dir", tmp]) == 1
            assert "导出失败" in error.getvalue() and "PRIVATE" not in error.getvalue()
            assert target.read_bytes() == before
        _feedback_jsonl(directory / "feedback.jsonl", [{"status": "pending_review", "request_id": "bad_log"}])
        (directory / "log_fixture.jsonl").write_bytes(b'PRIVATE_BROKEN_LOG\n')
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            assert exporter.main(["--data-dir", tmp]) == 1
        assert "PRIVATE" not in error.getvalue()
        assert target.read_bytes() == before


def test_feedback_export_rejects_symlinks_and_forbidden_data_paths():
    import io
    exporter = _feedback_exporter()
    with tempfile.TemporaryDirectory(prefix="qb_feedback_paths_") as tmp:
        root = Path(tmp).resolve()
        for name in ("feedback.jsonl", "log_fixture.jsonl", "review"):
            directory = root / name.replace(".", "_")
            directory.mkdir()
            if name != "feedback.jsonl":
                _feedback_jsonl(directory / "feedback.jsonl", [{"request_id": "fixture", "status": "pending_review"}])
            (directory / name).symlink_to(root / "not_opened")
            try:
                exporter.export_feedback(directory)
            except (OSError, ValueError):
                pass
            else:
                raise AssertionError("不应跟随符号链接")
        before = set(root.iterdir())
        for directory in (root / "ai_layer", root / "ai_layer" / "nested",
                          root / ".secret", root / ".secret" / "nested", exporter.DEFAULT_DATA_DIR):
            output = io.StringIO()
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(output), \
                    mock.patch.object(exporter.os, "open", side_effect=AssertionError("禁止打开源文件")):
                assert exporter.main(["--data-dir", str(directory)]) == 1
            assert output.getvalue() == ""
        assert set(root.iterdir()) == before
        # 显式禁用路径必须在 resolve 前拒绝，不能靠源文件打开失败间接拦截。
        for name in ("ai_layer", ".secret"):
            with mock.patch.object(Path, "resolve", side_effect=AssertionError("禁止解析私密路径")):
                try:
                    exporter.export_feedback(root / name / "nested")
                except ValueError:
                    pass
                else:
                    raise AssertionError("必须拒绝禁用目录")
            # 父目录链接解析到禁用路径时，也要在打开任何源文件前拒绝。
            parent_link = root / ("parent_link_" + name)
            parent_link.symlink_to(root / name)
            with contextlib.redirect_stderr(io.StringIO()), \
                    mock.patch.object(exporter.os, "open", side_effect=AssertionError("禁止打开源文件")):
                assert exporter.main(["--data-dir", str(parent_link / "nested")]) == 1
        link = root / "linked"
        link.symlink_to(root)
        with contextlib.redirect_stderr(io.StringIO()):
            assert exporter.main(["--data-dir", str(link)]) == 1


# ---------------------------------------------------------------- S11 本机假 API；不访问外网，不使用真实凭据
@contextlib.contextmanager
def _api_server(doc=None, *, raw=None, usage=True, status=200, delay=0, redirect=None,
                blocks=None, stop_reason="end_turn"):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    received = []
    reply = {"content": blocks if blocks is not None else [
        {"type": "text", "text": raw if raw is not None else json.dumps(doc or eq_cfg())}],
             "stop_reason": stop_reason}
    if usage:
        reply["usage"] = {"input_tokens": 100, "output_tokens": 200}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            received.append({"path": self.path, "body": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                             "authenticated": self.headers.get("x-api-key") == os.environ.get("QB_S11_CREDENTIAL"),
                             "version": self.headers.get("anthropic-version"),
                             "content_type": self.headers.get("content-type")})
            time.sleep(delay)
            body = received[-1]["body"]
            # 模拟本次支持的模型协议，避免假服务器掩盖真实参数不兼容。
            incompatible = (any(k in body for k in ("temperature", "top_p", "top_k"))
                            or body.get("thinking") != {"type": "disabled"}
                            or body.get("output_config", {}).get("effort") not in ("low", "medium", "high"))
            self.send_response(400 if incompatible else status)
            if redirect:
                self.send_header("Location", redirect)
            self.end_headers()
            try:
                self.wfile.write(json.dumps(reply).encode())
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    # serve_forever 默认每 0.5 秒才检查一次关闭标志，server.shutdown() 平均白等 0.25~0.5 秒；
    # 改成 10 毫秒轮询。只改关闭延迟，服务器行为和测试断言不变。
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port), received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


def _api_cfg(url, **overrides):
    return config.validate({"enabled": True, "backend": "api_llm", "api_llm": {
        "base_url": url, "api_key_env": "QB_S11_CREDENTIAL", **overrides},
        "statarb_src_dir": str(REPO.parent / "regime-statarb" / "src")})


@contextlib.contextmanager
def _api_credential():
    # 仅运行时生成的随机哨兵；源码、报告和测试输出均不保存其值。
    with mock.patch.dict(os.environ, {"QB_S11_CREDENTIAL": os.urandom(24).hex()}):
        yield os.environ["QB_S11_CREDENTIAL"]


@requires_external
def test_s11_prompt_examples_pass_real_gate():
    from src.ai_translator.api_prompt import EXAMPLES, SYSTEM_PROMPT
    oracle = real_gate()
    assert len(EXAMPLES) == 12 and sum("reject" in doc for _, doc in EXAMPLES) == 7      # S14：新增 OUT_OF_RANGE×2、EMBARGO_VIOLATION×1，去掉重复的追问例子"买还是卖"
    for idea, doc in EXAMPLES:
        assert idea in SYSTEM_PROMPT
        kind = backend.output_kind(doc)
        if kind != "clarify":
            assert oracle.validate(doc)["legal"]
    assert "lookback=252 ONLY" in SYSTEM_PROMPT and "skip=21 ONLY" in SYSTEM_PROMPT
    assert "12 months = 252" in SYSTEM_PROMPT and "1 month = 21" in SYSTEM_PROMPT


@requires_statarb_src
def test_s11_success_real_gate_pipeline_status_confirm_and_body():
    from src.ai_translator.api_prompt import SYSTEM_PROMPT
    with _api_credential(), _api_server(mom_cfg()) as (url, received):
        loader, _, _, root = make(_api_cfg(url))
        # 使用默认工厂和默认 API 闸门；没有 ai_layer_dir，也未注入 oracle。
        service = api.API(root, loader=loader)
        code, st = _call(service, "status")
        assert code == 200 and st["backend"] == "api_llm" and st["model"]["version"] == "claude-haiku-5-5"
        assert st["budget"]["requests_today"] == 0 and received == []
        idea = "动量，回看 252 日，跳过 21 日，每日调仓，前后 10%"
        result = _translated(service, idea)
        assert result["can_confirm"] and result["config"] == mom_cfg() and result["view"]
        assert _call(service, "confirm", {"request_id": result["request_id"], "ack_flags": True})[0] == 200
        record = received[0]
        assert record["authenticated"] and record["version"] == "2023-06-01"
        assert record["path"] == "/v1/messages" and record["content_type"] == "application/json"
        assert record["body"] == {"model": "claude-haiku-5-5", "max_tokens": 600,
                                  "thinking": {"type": "disabled"}, "output_config": {"effort": "high"},
                                  "system": SYSTEM_PROMPT, "messages": [{"role": "user", "content": idea}]}
        usage = _rows(root)[0]["usage"]
        assert usage == {"input_tokens": 100, "output_tokens": 200, "estimated_usd": 0.00011, "estimated": False}
        budget = _call(service, "status")[1]["budget"]
        assert budget["used_usd"] == 0.00011 and budget["requests_today"] == 1
        assert stat.S_IMODE((root / "data/ai_translator/usage.json").stat().st_mode) == 0o600
        service.close()
        cfg = loader.load().cfg
        cfg["enabled"] = False
        loader.save(cfg)
        disabled = _call(service, "status")[1]
        assert disabled["state"] == "disabled" and disabled["budget"]["requests_today"] == 1


@requires_statarb_src
def test_s11_gate_consistency_precheck_and_model_refusal():
    reject = {"schema_version": "1.0", "reject": {"code": "MISSING_REQUIRED", "message": "Name a strategy."}}
    bad = mom_cfg()
    bad["signal"]["lookback"] = 88
    for output, idea, source, rejection in [
        (bad, "momentum reference for my notebook", "gate", "OUT_OF_RANGE"),
        (mom_cfg(), "momentum lookback 88 skip 21", "counterfactual", "OUT_OF_RANGE"),
        (reject, "prepare a research entry for me", "model", "MISSING_REQUIRED"),
        (eq_cfg(), "equal-weight through 2029-03-31", "precheck", "SEALED_PERIOD"),
    ]:
        with _api_credential(), _api_server(output) as (url, received):
            loader, _, _, root = make(_api_cfg(url))
            service = api.API(root, loader=loader)
            result = _translated(service, idea)
            assert not result["can_confirm"] and result["config"] is None
            assert result["refusal"]["code"] == rejection and result["refusal"]["source"] == source, result
            assert len(received) == (0 if source == "precheck" else 1)
            service.close()


def test_s11_bad_output_timeout_http_and_redirect_never_retry():
    for options, error_code in [
        ({"raw": "plain prose"}, "TRANSLATOR_BAD_OUTPUT"),
        ({"raw": '{"a":1,"a":2}'}, "TRANSLATOR_BAD_OUTPUT"),
        ({"raw": '{"a":NaN}'}, "TRANSLATOR_BAD_OUTPUT"),
        ({"blocks": [{"type": "thinking", "thinking": "fixture"},
                     {"type": "text", "text": json.dumps(eq_cfg())}]}, "TRANSLATOR_BAD_OUTPUT"),
        ({"stop_reason": "max_tokens"}, "TRANSLATOR_BAD_OUTPUT"),
        ({"delay": 0.15}, "TRANSLATOR_TIMEOUT"),
        ({"status": 503, "raw": "private response details"}, "TRANSLATOR_BACKEND_FAILED"),
        ({"status": 302, "redirect": "https://example.invalid"}, "TRANSLATOR_BACKEND_FAILED"),
    ]:
        with _api_credential(), _api_server(**options) as (url, received):
            cfg = _api_cfg(url)
            cfg["timeouts"]["request_s"] = 0.04 if "delay" in options else 2
            loader, _, _, root = make(cfg)
            service = api.API(root, loader=loader)
            code, body = _call(service, "translate", {"idea": "equal-weight reference"})
            assert code == backend.ERROR_HTTP[error_code] and body == {"error": error_code, "message": error_code}, body
            assert len(received) == 1
            assert _rows(root)[0]["usage"]["estimated_usd"] > 0
            service.close()


def test_s11_request_parameters_and_text_contract_without_network():
    import io
    from src.ai_translator.api_prompt import SYSTEM_PROMPT
    idea = "equal-weight reference"
    with _api_credential():
        for blocks, stop, expected in [
            ([{"type": "text", "text": json.dumps(eq_cfg())}], "end_turn", None),
            ([{"type": "thinking", "thinking": "fixture"},
              {"type": "text", "text": json.dumps(eq_cfg())}], "end_turn", "TRANSLATOR_BAD_OUTPUT"),
            ([{"type": "text", "text": json.dumps(eq_cfg())}], "max_tokens", "TRANSLATOR_BAD_OUTPUT"),
        ]:
            worker = api_llm.ApiLlmBackend(_api_cfg("https://api.anthropic.com"), root=Path(tempfile.mkdtemp()))

            def open_(req, *, timeout):
                body = json.loads(req.data)
                assert set(body) == {"model", "max_tokens", "thinking", "output_config", "system", "messages"}
                assert body["model"] == "claude-haiku-5-5" and body["max_tokens"] == 600
                assert body["thinking"] == {"type": "disabled"}
                assert body["output_config"] == {"effort": "high"}
                assert body["system"] == SYSTEM_PROMPT
                assert body["messages"] == [{"role": "user", "content": idea}]
                return io.BytesIO(json.dumps({"content": blocks, "stop_reason": stop,
                    "usage": {"input_tokens": 100, "output_tokens": 200}}).encode())

            with mock.patch.object(worker._opener, "open", side_effect=open_) as opened:
                result = worker.translate(idea)
            assert opened.call_count == 1
            assert (result["refusal"] or {}).get("code") == expected, result
            assert result["config"] == (eq_cfg() if expected is None else None)
            assert result["usage"]["estimated_usd"] == 0.00011
            worker.close()


@requires_statarb_src
def test_s11_status_during_inflight_request_keeps_budget_and_ready():
    with _api_credential():
        loader, _, _, root = make(_api_cfg("https://api.anthropic.com"))
        service = api.API(root, loader=loader)
        assert _call(service, "status")[1]["state"] == "ready"
        worker = service._backend
        entered, release = threading.Event(), threading.Event()
        outcomes = []

        def send_(payload, key):
            entered.set()
            assert release.wait(5)
            return {"content": [{"type": "text", "text": json.dumps(eq_cfg())}],
                    "stop_reason": "end_turn", "usage": {"input_tokens": 100, "output_tokens": 200}}, None

        with mock.patch.object(worker, "_send", side_effect=send_):
            thread = threading.Thread(target=lambda: outcomes.append(
                _call(service, "translate", {"idea": "equal-weight reference"})))
            thread.start()
            try:
                assert entered.wait(3)
                code, body = _call(service, "status")
                assert code == 200 and body["state"] == "ready" and body["problem"] is None
                assert body["budget"]["requests_today"] == 1 and body["budget"]["used_usd"] > 0
                other = api_llm.ApiLlmBackend(loader.load().cfg, root=root)
                assert other.translate("equal-weight")["refusal"]["code"] == "TRANSLATOR_BUSY"
            finally:
                release.set()
                thread.join(timeout=5)
            assert not thread.is_alive()
        assert outcomes[0][0] == 200 and outcomes[0][1]["can_confirm"], outcomes
        assert _call(service, "status")[1]["budget"]["used_usd"] == 0.00011
        # 无锁状态读取仍严格拒绝损坏账本，发送也不能绕过。
        with worker.usage.locked():
            worker.usage.path.write_text("{broken")
            body = _call(service, "status")[1]
            assert body["state"] == "unavailable" and body["problem"] == "TRANSLATOR_BUDGET_EXCEEDED"
            assert body["budget"] is None
        assert worker.translate("equal-weight")["refusal"]["code"] == "TRANSLATOR_BUDGET_EXCEEDED"
        service.close()


def test_s11_missing_key_and_key_never_persisted_or_returned():
    with _api_server() as (url, received):
        cfg = _api_cfg(url)
        root = Path(tempfile.mkdtemp())
        with mock.patch.dict(os.environ):
            os.environ.pop("QB_S11_CREDENTIAL", None)
            worker = api_llm.ApiLlmBackend(cfg, root=root)
            assert worker.status()["problem"] == "TRANSLATOR_UNAVAILABLE"
            assert worker.translate("equal-weight")["refusal"]["code"] == "TRANSLATOR_UNAVAILABLE"
            assert received == []
    with _api_credential() as sentinel:
        # 上游恶意回显密钥（普通文本或 JSON unicode 转义）均失败即关闭。
        for raw in (sentinel, json.dumps({"echo": sentinel}).replace(sentinel[0], "\\u%04x" % ord(sentinel[0]))):
            with _api_server(raw=raw) as (url, received):
                loader, _, _, root = make(_api_cfg(url))
                service = api.API(root, loader=loader)
                code, body = _call(service, "translate", {"idea": "equal-weight " + sentinel})
                assert code == 502 and sentinel not in json.dumps(body)
                for path in (root / "data").rglob("*"):
                    if path.is_file():
                        assert sentinel.encode() not in path.read_bytes()
                assert sentinel not in str(backend.TranslatorError("TRANSLATOR_BAD_OUTPUT"))
                service.close()


@requires_statarb_src
def test_s11_budget_daily_persist_restart_and_missing_usage():
    for kind in ("monthly", "daily", "reserve"):
        with _api_credential(), _api_server(usage=False) as (url, received):
            cfg = _api_cfg(url, **({"max_monthly_usd": 0} if kind == "monthly" else
                                   {"max_monthly_usd": 0.000001} if kind == "reserve" else {"max_daily_requests": 1}))
            loader, _, _, root = make(cfg)
            service = api.API(root, loader=loader)
            if kind == "daily":
                assert _translated(service)["can_confirm"]
                assert _rows(root)[0]["usage"]["estimated"] is True
                service.close()
                service = api.API(root, loader=loader)
            code, body = _call(service, "translate", {"idea": "equal-weight reference"})
            assert code == 429 and body["error"] == "TRANSLATOR_BUDGET_EXCEEDED"
            assert len(received) == (1 if kind == "daily" else 0)
            service.close()
    # 已用金额达到月限，禁止发送；不只是零预算特例。
    with _api_credential(), _api_server() as (url, received):
        cfg = _api_cfg(url)
        root = Path(tempfile.mkdtemp())
        worker = api_llm.ApiLlmBackend(cfg, root=root)
        with worker.usage.locked() as row:
            row["estimated_usd"] = 2
            worker.usage.write(row)
        assert worker.translate("equal-weight")["refusal"]["code"] == "TRANSLATOR_BUDGET_EXCEEDED"
        assert received == []


def test_s11_usage_rollover_corruption_and_file_lock():
    with _api_credential(), _api_server() as (url, received):
        cfg = _api_cfg(url)
        root = Path(tempfile.mkdtemp())
        day = [datetime.date(2026, 8, 30)]
        worker = api_llm.ApiLlmBackend(cfg, root=root, today=lambda: day[0])
        assert worker.translate("equal-weight")["config"]
        day[0] = datetime.date(2026, 8, 31)
        assert worker.usage.snapshot()["requests_today"] == 0
        assert worker.translate("equal-weight")["config"]
        assert worker.usage.snapshot()["used_usd"] == 0.00022
        day[0] = datetime.date(2026, 9, 1)
        assert worker.usage.snapshot()["used_usd"] == 0 and worker.usage.snapshot()["requests_today"] == 0
        assert worker.translate("equal-weight")["config"]
        import fcntl
        with worker.usage.locked():
            other = api_llm.ApiLlmBackend(cfg, root=root)
            assert other.translate("equal-weight")["refusal"]["code"] == "TRANSLATOR_BUSY"
        valid = json.loads(worker.usage.path.read_text())
        for damaged in ("{broken", {**valid, "estimated_usd": -1}, {**valid, "requests": True},
                        {**valid, "day": "bad"}, {**valid, "estimated_usd": float("nan")},
                        {**valid, "month": "2025-01", "estimated_usd": -1}):
            worker.usage.path.write_text(damaged if isinstance(damaged, str) else json.dumps(damaged))
            before = len(received)
            assert worker.translate("equal-weight")["refusal"]["code"] == "TRANSLATOR_BUDGET_EXCEEDED"
            assert worker.status()["problem"] == "TRANSLATOR_BUDGET_EXCEEDED"
            assert len(received) == before


def test_s11_api_configuration_and_destination_allowlist():
    for key, values in {"max_monthly_usd": [-1, 50.01, True, "2", float("inf")],
                        "max_daily_requests": [-1, 10001, 1.5, True],
                        "price_input_per_mtok": [-1, True, float("nan")],
                        "price_output_per_mtok": [-1, "0.5"], "model": [None, 3, "a/b"]}.items():
        for value in values:
            try:
                config.validate({"backend": "api_llm", "api_llm": {key: value}})
            except config.ConfigError:
                pass
            else:
                raise AssertionError((key, value))
    for value in ("http://api.anthropic.com", "https://api.anthropic.com.evil", "https://api.anthropic.com/",
                  "https://api.anthropic.com/v1", "https://api.anthropic.com?x=1", "http://0.0.0.0:8000",
                  "http://127.0.0.1:8000/path", "http://localhost:8000#fragment", None):
        try:
            config.validate({"backend": "api_llm", "api_llm": {"base_url": value}})
        except config.ConfigError:
            pass
        else:
            raise AssertionError(value)
    with mock.patch.dict(os.environ, {"QB_TESTING": "0"}):
        for value in ("http://127.0.0.1:8000", "http://localhost:8000"):
            try:
                config.api_base_url(value)
            except config.ConfigError:
                pass
            else:
                raise AssertionError(value)
    assert config.validate({})["api_llm"]["model"] == "claude-haiku-5-5"
    legacy = config.validate({"enabled": True, "backend": "local_lora", "api_llm": {"model": None, "base_url": None}})
    assert legacy["api_llm"]["model"] == "claude-haiku-5-5"
    assert config.validate({"api_llm": {"base_url": "http://localhost:8000"}})
    assert config.api_base_url("http://127.0.0.1") == "http://127.0.0.1"
    assert config.api_base_url("http://localhost") == "http://localhost"


def test_s11_switch_cli_backend_usage_and_environment_yes_no():
    with _api_credential():
        os.environ.pop("AI_TRANSLATOR_API_KEY", None)      # 环境隔离：下面断言默认环境变量"未设置"；_api_credential 退出时会原样恢复 os.environ
        root = Path(tempfile.mkdtemp())
        out = _script("ai_translator_switch.py", "on", "--root", root, "--backend", "api_llm")
        assert out.returncode == 0, out.stderr
        result = json.loads(out.stdout)
        assert result["backend"] == "api_llm" and result["budget"]["requests_today"] == 0
        assert result["api_key_env_set"] == "no"  # 配置默认环境变量未设置。
        loader = config.ConfigLoader(root)
        cfg = loader.load().cfg
        cfg["api_llm"]["api_key_env"] = "QB_S11_CREDENTIAL"
        loader.save(cfg)
        # 本进程的 CLI 仅报告 yes/no，不打印环境变量内容。
        import importlib.util
        spec = importlib.util.spec_from_file_location("s11_switch", REPO / "scripts/ai_translator_switch.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        import io
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            assert module.main(["status", "--root", str(root)]) == 0
        result = json.loads(output.getvalue())
        assert result["api_key_env_set"] == "yes"
        assert os.environ["QB_S11_CREDENTIAL"] not in output.getvalue()
        out = _script("ai_translator_switch.py", "off", "--root", root)
        assert out.returncode == 0 and not loader.load().enabled


# ---------------------------------------------------------------- S12：评测只用临时用例、临时 schema、本机假 HTTP

def _s12_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("qb_eval_s12", REPO / "scripts/run_translator_eval.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _s12_case(identifier="sample", outcome="accept", idea="equal-weight reference", **extra):
    return {"id": identifier, "idea": idea, "language": "en", "category": 1,
            "expected_outcome": outcome, "expected_code": "UNSUPPORTED_REQUEST" if outcome == "reject" else None,
            "expected_fields": {"signal.type": "equal_weight"} if outcome == "accept" else None, **extra}


@contextlib.contextmanager
def _s12_setup(url, cases, **overrides):
    """临时 schema 只作测试判定器；实际使用完整 Gate 子进程和审计，不读取研究项目。"""
    with tempfile.TemporaryDirectory(prefix="qb_s12_") as tmp:
        root = Path(tmp)
        package = root / "fixture" / "src" / "statarb"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "schema.py").write_text('''REJECT_CODES = ["OUT_OF_RANGE", "UNSUPPORTED_REQUEST", "MISSING_REQUIRED", "SEALED_PERIOD", "EMBARGO_VIOLATION"]
def validate_document(doc):
    def issue(code, path):
        return [{"code": code, "path": path, "message": "fixture validation"}]
    if "reject" in doc:
        r = doc["reject"]
        return [] if r.get("code") in REJECT_CODES and r.get("message") else issue("UNSUPPORTED_REQUEST", "reject")
    if doc.get("signal", {}).get("type") not in ("equal_weight", "momentum"):
        return issue("UNSUPPORTED_REQUEST", "signal.type")
    if doc["market"]["cost_bp"] not in (0, 0.5, 1, 2, 5, 10, 20):
        return issue("OUT_OF_RANGE", "market.cost_bp")
    if doc["signal"]["type"] == "momentum" and doc["signal"].get("lookback") != 252:
        return issue("OUT_OF_RANGE", "signal.lookback")
    return []
''', encoding="utf-8")
        cfg = _api_cfg(url, **overrides)
        cfg["statarb_src_dir"] = str(package.parent)
        config.ConfigLoader(root).save(cfg)
        cases_path = root / "cases.json"
        cases_path.write_text(json.dumps(cases), encoding="utf-8")
        yield root, cases_path


@contextlib.contextmanager
def _s12_fast(module):
    # 保留入口限速判定，虚拟时钟每次前进 7 秒，避免测试真的等待数分钟。
    service_class = api.API
    tick = iter(range(0, 100000, 7))
    def factory(*args, **kwargs):
        return service_class(*args, **kwargs, clock=lambda: next(tick))
    with mock.patch.object(module.time, "sleep"), mock.patch.object(module.api, "API", side_effect=factory):
        yield


def test_s12_field_mapping_and_missing():
    module = _s12_module()
    expected = {"signal.type": "momentum", "signal.lookback": 252, "signal.skip": 21,
                "signal.rebalance": "daily", "signal.top_pct": 0.1,
                "period.start": "auto", "period.end": "2023-12-31", "market.cost_bp": 5}
    matches, fields = module.fields_match(expected, mom_cfg())
    assert matches and len(fields) == len(expected)
    assert {f["config_path"] for f in fields} >= {"portfolio.q", "portfolio.rebalance_every"}
    assert module.fields_match({"signal.fast": 20, "signal.slow": 50},
                               cfg_of({"type": "ma_cross", "fast": 20, "slow": 50}))[0]
    assert not module.fields_match({"signal.missing": None}, mom_cfg())[0]
    assert not module.fields_match({"portfolio.rebalance_every": True}, mom_cfg())[0]
    assert module.fields_match({}, eq_cfg())[0]


def test_s12_case_limits_ambiguous_and_cli():
    import io
    module = _s12_module()
    with tempfile.TemporaryDirectory(prefix="qb_s12_cases_") as tmp:
        p = Path(tmp) / "cases.json"
        cases = [_s12_case(str(i)) for i in range(50)] + [_s12_case("amb", "ambiguous")]
        p.write_text(json.dumps(cases))
        active, excluded = module.load_cases(p, 50)
        assert len(active) == 50 and excluded == ["amb"]
        for cap, content in [(49, cases), (50, cases + [_s12_case("overflow")])]:
            p.write_text(json.dumps(content))
            with mock.patch.object(module.CountingBackend, "_send", side_effect=AssertionError("不应发请求")):
                try:
                    module.evaluate(p, root=Path(tmp), max_calls=cap)
                except module.EvalError as exc:
                    assert str(exc) == "EVAL_TOO_MANY_CASES"
                else:
                    raise AssertionError("用例超限未拒绝")
        for value in ("51", "0", "-1", "1.5"):
            with contextlib.redirect_stderr(io.StringIO()):
                try:
                    module.main(["--root", tmp, "--max-calls", value])
                except SystemExit as exc:
                    assert exc.code == 2
                else:
                    raise AssertionError("CLI 放宽上限")
        assert module.MAX_CALLS == 50 and module.call_limit("1") == 1
        for cap in (51, True, 0):
            try:
                module.evaluate(p, root=Path(tmp), max_calls=cap)
            except module.EvalError as exc:
                assert str(exc) == "EVAL_MAX_CALLS_INVALID"
            else:
                raise AssertionError("函数入口放宽上限")


def test_s12_scoring_summary_isolation_and_protected_cases():
    import io
    module = _s12_module()
    protected = REPO / "tests" / "ai_translator_eval_cases.json"
    original = protected.read_bytes()
    with _api_credential() as sentinel, _api_server(eq_cfg()) as (url, received):
        cases = [_s12_case("ok"), _s12_case("altered", expected_fields={"market.cost_bp": 2}),
                 _s12_case("false", "reject", language="zh", category=2),
                 _s12_case("pre", "reject", "equal-weight through 2028-01-01", expected_code="SEALED_PERIOD", language="zh", category=3),
                 _s12_case("amb", "ambiguous")]
        with _s12_setup(url, cases) as (root, cases_path), _s12_fast(module):
            # 通过 main 覆盖完整输出和落盘；仅使用运行时哨兵凭据。
            output, errors = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                assert module.main(["--root", str(root), "--cases", str(cases_path)]) == 0
            files = list((root / "data/ai_translator/eval").glob("eval_*.json"))
            assert len(files) == 1 and not errors.getvalue()
            report = json.loads(files[0].read_text())
            s, rows = report["summary"], report["results"]
            assert report["excluded_ambiguous_ids"] == ["amb"] and len(rows) == 4
            assert len(received) == s["calls"] == 3
            assert s["false_accept"] == s["silent_alteration"] == 1
            assert s["accept_produced"]["numerator"] == 2
            assert s["accept_fields_all_correct"] == {"numerator": 1, "denominator": 2, "rate": 0.5}
            assert s["reject_correctly_rejected"]["rate"] == s["rejection_code_match"]["rate"] == 0.5
            assert rows[1]["silent_alteration"] and rows[1]["fields"][0]["actual"] == 5
            assert rows[2]["false_accept"] and not rows[2]["correct"]
            assert rows[3]["calls"] == 0 and rows[3]["stopped_by"] == "precheck"
            assert s["stopped_by"] == {"precheck": 1}
            assert all(r["authenticated"] for r in received)
            for dimension in ("language", "category"):
                groups = report["by_" + dimension]
                for value in {r[dimension] for r in rows}:
                    assert groups[str(value)] == module.summarize([r for r in rows if r[dimension] == value])
                assert sum(g["evaluated"] for g in groups.values()) == s["evaluated"]
                assert sum(g["false_accept"] for g in groups.values()) == s["false_accept"]
            for metric in ("false_accept", "silent_alteration", "gate_caught", "calls"):
                assert s[metric] == sum(r[metric] for r in rows)
            assert s["usage"]["input_tokens"] == 300 and s["usage"]["output_tokens"] == 600
            assert abs(s["usage"]["estimated_usd"] - sum(r["usage"]["estimated_usd"] for r in rows if r["usage"])) < 1e-12
            ledger = json.loads((root / "data/ai_translator/usage.json").read_text())
            assert ledger["requests"] == 3 and abs(ledger["estimated_usd"] - s["usage"]["estimated_usd"]) < 1e-12
            assert not list((root / "data").rglob("log_*.jsonl"))
            assert not list((root / "data").rglob("feedback.jsonl"))
            assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
            assert stat.S_IMODE(files[0].parent.stat().st_mode) == 0o700
            for file in (root / "data").rglob("*"):
                if file.is_file():
                    assert sentinel not in file.read_text()
            assert sentinel not in output.getvalue() + errors.getvalue()
            assert "raw_output" not in files[0].read_text() and "fixture validation" not in files[0].read_text()
            assert "altered" in output.getvalue() and "false" in output.getvalue()
    assert protected.read_bytes() == original


def test_s12_gate_model_and_consistency_layers():
    module = _s12_module()
    bad = eq_cfg(cost=0.3)
    reject = {"schema_version": "1.0", "reject": {"code": "MISSING_REQUIRED", "message": "fixture"}}
    scenarios = [(bad, "equal-weight reference", "gate", "OUT_OF_RANGE", True),
                 (eq_cfg(), "equal-weight 0.3 bp", "counterfactual", "OUT_OF_RANGE", False),
                 (reject, "prepare an entry", "model", "MISSING_REQUIRED", False)]
    for doc, idea, source, code, caught in scenarios:
        with _api_credential(), _api_server(doc) as (url, received):
            case = _s12_case("fixture", "reject", idea, expected_code="UNSUPPORTED_REQUEST", accepted_codes=[code])
            with _s12_setup(url, [case]) as (root, p), _s12_fast(module):
                report = module.evaluate(p, root=root)
                row = report["results"][0]
                assert row["stopped_by"] == source and row["code_match"] and row["correct"]
                assert row["gate_caught"] == caught and len(received) == 1
                assert report["summary"]["stopped_by"] == {source: 1}
                assert report["summary"]["gate_caught"] == int(caught)


def test_s12_budget_stops_all_and_marks_remaining():
    module = _s12_module()
    with _api_credential(), _api_server() as (url, received):
        cases = [_s12_case("first"), _s12_case("budget"),
                 _s12_case("later-precheck", "reject", "through 2028-01-01")]
        with _s12_setup(url, cases, max_daily_requests=1) as (root, p), _s12_fast(module):
            report = module.evaluate(p, root=root)
            assert report["stop_reason"] == api_llm.BUDGET_ERROR and len(received) == 1
            assert report["results"][1]["code"] == api_llm.BUDGET_ERROR
            assert not report["results"][1]["rejected"]
            assert report["results"][1]["usage"] is None and report["results"][1]["calls"] == 0
            assert report["results"][2]["actual"] == "not_evaluated"
            assert report["summary"]["not_evaluated"] == 1 and report["summary"]["calls"] == 1
            target = module.write_report(report, root)
            assert json.loads(target.read_text())["stop_reason"] == api_llm.BUDGET_ERROR
        with _s12_setup(url, cases, max_monthly_usd=0.000001) as (root, p), _s12_fast(module):
            report = module.evaluate(p, root=root)
            assert report["stop_reason"] == api_llm.BUDGET_ERROR
            assert report["summary"]["calls"] == 0 and report["summary"]["not_evaluated"] == 2
            assert len(received) == 1


def test_s12_missing_key_before_any_request():
    import io
    module = _s12_module()
    with _api_server() as (url, received), mock.patch.dict(os.environ):
        os.environ.pop("QB_S11_CREDENTIAL", None)
        with _s12_setup(url, [_s12_case()]) as (root, p):
            output, errors = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                assert module.main(["--root", str(root), "--cases", str(p)]) == 1
            assert errors.getvalue() == "EVAL_API_KEY_MISSING\n" and not output.getvalue()
            assert not received and not (root / "data/ai_translator").exists()


def test_s12_fifty_calls_and_send_boundary_hard_cap():
    module = _s12_module()
    cases = [_s12_case(str(i)) for i in range(50)]
    with _api_credential(), _api_server() as (url, received):
        with _s12_setup(url, cases) as (root, p), _s12_fast(module):
            report = module.evaluate(p, root=root)
            assert len(received) == report["summary"]["calls"] == 50
            assert report["summary"]["accept_fields_all_correct"]["numerator"] == 50
            worker = module.CountingBackend(config.ConfigLoader(root).load().cfg, root=root, max_calls=500)
            worker.calls = 50
            envelope, error = worker._send(b"{}", os.environ["QB_S11_CREDENTIAL"])
            assert error == "TRANSLATOR_BACKEND_FAILED" and envelope is None
            assert worker.limit_reached and len(received) == 50 and worker.max_calls == 50
            worker.close()
        with _s12_setup(url, cases[:2]) as (root, p), _s12_fast(module):
            # 防御性模拟：某个发送边界提前用尽预算以外的硬调用额度。
            original = module.CountingBackend._send
            def exhausted(self, payload, key):
                self.calls = self.max_calls
                return original(self, payload, key)
            with mock.patch.object(module.CountingBackend, "_send", exhausted):
                report = module.evaluate(p, root=root)
            assert report["stop_reason"] == module.CALL_LIMIT
            assert report["results"][1]["actual"] == "not_evaluated" and len(received) == 50


def test_s12_strict_errors_no_retry_and_no_secret_output():
    import io
    module = _s12_module()
    for options in ({"raw": "```json\n{}\n```"}, {"raw": '{"a":1,"a":2}'}, {"status": 503}):
        with _api_credential() as sentinel, _api_server(**options) as (url, received):
            with _s12_setup(url, [_s12_case()]) as (root, p), _s12_fast(module):
                report = module.evaluate(p, root=root)
                assert len(received) == 1 and report["results"][0]["actual"] == "error"
                assert not report["results"][0]["correct"] and not report["results"][0]["rejected"]
                target = module.write_report(report, root)
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    module.print_summary(report, target)
                assert sentinel not in target.read_text() + output.getvalue()
                assert "```" not in target.read_text()
    with _api_credential() as sentinel, _api_server(raw=json.dumps({"echo": os.environ["QB_S11_CREDENTIAL"]})) as (url, received):
        # 单独把密钥哨兵放进想法，网页日志接收器仍不落盘全文。
        with _s12_setup(url, [_s12_case(idea="equal-weight " + sentinel)]) as (root, p), _s12_fast(module):
            report = module.evaluate(p, root=root)
            target = module.write_report(report, root)
            assert sentinel not in target.read_text()
            assert report["results"][0]["code"] == "TRANSLATOR_BAD_OUTPUT"


# ---------------------------------------------------------------- S13：三类输出（全部使用临时目录、本机假 HTTP 和注入的 oracle）
def _control(kind, message, code=None):
    return {"schema_version": "1.0", kind: {"message": message, **({"code": code} if code else {})}}


def test_s13_clarify_no_config_gate_or_confirmation():
    for idea, message in [("你好", "你好！想研究哪种策略？"),
                          ("Buy or sell?", "Which signal would you like to describe?"),
                          ("请帮我", '<img src=x onerror="alert(1)">请说明信号。'),
                          ("Hi", "x" * backend.MAX_MESSAGE_CHARS)]:
        doc = _control("clarify", message)
        with _api_credential(), _api_server(doc) as (url, received):
            loader, _, _, root = make(_api_cfg(url))
            oracle = AlwaysLegal()
            service = api.API(root, loader=loader, oracle=oracle)
            try:
                result = _translated(service, idea)
                assert result["status"] == "clarify" and result["clarify"] == {"message": message}
                assert result["config"] is None and result["refusal"] is None
                assert result["view"] == result["flags"] == []
                assert not result["can_confirm"] and not result["needs_ack"] and oracle.calls == []
                assert _call(service, "confirm", {"request_id": result["request_id"], "config": eq_cfg()})[0] == 409
                assert len(received) == 1
                row = _rows(root)[0]
                assert row["status"] == "clarify" and row["raw_output"] == "" and row["config"] == doc
                assert row["gate"] is None and row["parse_mode"] == "strict"
                assert row["usage"]["estimated_usd"] == 0.00011
                assert _call(service, "status")[1]["budget"]["requests_today"] == 1
                assert len(_rows(root)) == 1
            finally:
                service.close()


def test_s13_all_existing_reject_codes_pass_protocol_and_original_gate():
    assert set(plainview.REJECT_CODES) == {"SEALED_PERIOD", "OUT_OF_RANGE", "EMBARGO_VIOLATION",
                                         "UNSUPPORTED_REQUEST", "MISSING_REQUIRED"}
    for code in plainview.REJECT_CODES:
        message = "缺少 signal，请说明策略。" if code == "MISSING_REQUIRED" else "signal.embargo must exceed signal.h."
        doc = _control("reject", message, code)
        with _api_credential(), _api_server(doc) as (url, received):
            loader, _, _, root = make(_api_cfg(url))
            oracle = AlwaysLegal()
            service = api.API(root, loader=loader, oracle=oracle)
            try:
                result = _translated(service, "Prepare a configuration for the comparison notebook.")
                assert result["status"] == "rejected" and result["config"] is None
                assert result["refusal"]["code"] == code and result["refusal"]["message"] == message
                assert result["refusal"]["source"] == "model" and oracle.calls == [doc]
                assert not result["can_confirm"] and not result["view"]
                assert _call(service, "confirm", {"request_id": result["request_id"]})[0] == 409
                row = _rows(root)[0]
                assert row["status"] == "rejected" and row["raw_output"] == "" and row["config"] == doc
                assert row["usage"]["estimated_usd"] == 0.00011 and len(received) == 1
            finally:
                service.close()


def test_s13_strict_invalid_shapes_messages_and_unknown_keys():
    clarify = _control("clarify", "Which signal?")
    reject = _control("reject", "Name signal.", "MISSING_REQUIRED")
    invalid = [
        {**eq_cfg(), "clarify": clarify["clarify"]},
        {**eq_cfg(), "reject": reject["reject"]},
        {**clarify, "reject": reject["reject"]},
        {**clarify, "extra": True},
        {"schema_version": "1.0", "clarify": {"message": "Which?", "extra": 1}},
        {"schema_version": "1.0", "reject": {**reject["reject"], "extra": 1}},
        {**clarify, "schema_version": "2.0"}, {"clarify": clarify["clarify"]},
        {**eq_cfg(), "extra": True}, {**eq_cfg(), "signal": {"type": "equal_weight", "extra": 1}},
        {"schema_version": "1.0", "config": eq_cfg()},
        _control("reject", "Bad code.", "NEW_CODE"),
        {"schema_version": "1.0", "reject": {"message": "Missing code."}},
        {"schema_version": "1.0", "clarify": None},
    ]
    for kind in ("clarify", "reject"):
        for message in ("", " \n\t", "x" * (backend.MAX_MESSAGE_CHARS + 1), 1, None):
            invalid.append(_control(kind, message, "MISSING_REQUIRED" if kind == "reject" else None))
    for doc in invalid:
        with _api_credential(), _api_server(doc) as (url, received):
            loader, _, _, root = make(_api_cfg(url))
            oracle = AlwaysLegal()
            service = api.API(root, loader=loader, oracle=oracle)
            try:
                code, body = _call(service, "translate", {"idea": "Please prepare my notebook configuration."})
                assert code == 502 and body["error"] == "TRANSLATOR_BAD_OUTPUT", (doc, body)
                assert len(received) == 1 and oracle.calls == [] and not service._cache
                assert _rows(root)[0]["raw_output"] == "" and _rows(root)[0]["config"] is None
            finally:
                service.close()
    # 公共入口也校验其他后端的形状，不能只保护 API 后端。
    service, _, _, root = _api_fixture([{**eq_cfg(), "clarify": clarify["clarify"]}])
    try:
        assert _call(service, "translate", {"idea": "equal-weight"})[1]["error"] == "TRANSLATOR_BAD_OUTPUT"
        assert _rows(root)[0]["raw_output"] == ""
    finally:
        service.close()


def test_s13_explicit_h_and_embargo_precheck_without_model():
    for text in ("Notebook: ridge, h=21 trading days, embargo=19 trading days.",
                 "岭回归，signal.h: 5，signal.embargo: 4。", "ridge h=5, embargo=0"):
        ex = extract.extract(text)
        assert ex.value("signal.h") in (5, 21) and ex.value("signal.embargo") in (0, 4, 19)
        service, fake, factory, root = _api_fixture([])
        try:
            result = _translated(service, text)
            assert result["refusal"]["code"] == "EMBARGO_VIOLATION"
            assert result["refusal"]["source"] == "precheck" and not result["can_confirm"]
            assert not fake.calls and not factory.called and _rows(root)[0]["raw_output"] == ""
        finally:
            service.close()
    for text in ("ridge h=5, embargo=6", "ridge h=5, h=21, embargo=6", "ridge without h=5, embargo=4",
                 "alpha_h=5, embargo=4", "ridge h=5x, embargo=4", "ridge h=5.5, embargo=4"):
        assert not precheck.precheck(text)["blocked"], text


def test_s13_config_gate_consistency_confirmation_and_field_reasons_preserved():
    def validate(doc):
        if doc.get("market", {}).get("cost_bp") == 7:
            return {"legal": False, "code": "OUT_OF_RANGE", "errors": [
                {"path": "market.cost_bp", "code": "OUT_OF_RANGE", "message": "fixture reason"},
                {"path": "/private/internal", "code": "OUT_OF_RANGE", "message": "private detail"}]}
        return {"legal": True, "code": None, "errors": []}

    with _api_credential(), _api_server(eq_cfg()) as (url, received):
        loader, _, _, root = make(_api_cfg(url))
        oracle = mock.Mock()
        oracle.validate.side_effect = validate
        service = api.API(root, loader=loader, oracle=oracle)
        try:
            with mock.patch.object(consistency, "compare", wraps=consistency.compare) as compared:
                ok = _translated(service, "equal-weight")
                assert ok["status"] == "ok" and ok["config"] == eq_cfg() and ok["can_confirm"] and ok["view"]
                assert _call(service, "confirm", {"request_id": ok["request_id"]})[1]["config"] == eq_cfg()
                warn = _translated(service, "equal-weight with 10 bp")
                assert warn["needs_ack"] and warn["can_confirm"]
                assert _call(service, "confirm", {"request_id": warn["request_id"]})[0] == 409
                assert _call(service, "confirm", {"request_id": warn["request_id"], "ack_flags": True})[0] == 200
                blocked = _translated(service, "equal-weight with 7 bp")
                assert blocked["refusal"]["source"] == "counterfactual" and blocked["config"] is None
                assert _call(service, "confirm", {"request_id": blocked["request_id"]})[0] == 409
                assert compared.call_count == 3 and oracle.validate.call_count >= 5
            assert len(received) == 3 and _rows(root)[0]["raw_output"]
        finally:
            service.close()
    service, _, _, _ = _api_fixture([eq_cfg(cost=7)], oracle=oracle)
    try:
        result = _translated(service, "equal-weight")
        assert result["refusal"]["source"] == "gate" and result["refusal"]["fields"] == ["market.cost_bp"]
        assert result["config"] is None and not result["can_confirm"]
        assert "private detail" not in json.dumps(result) and "/private/internal" not in json.dumps(result)
    finally:
        service.close()


def test_s13_frontend_control_messages_plain_text_examples_and_no_confirm():
    source = (REPO / "dashboard" / "ai_translator.js").read_text()
    test_frontend_safe_text_and_single_status_request()
    branch = source.split('if (result.status === "clarify" || result.clarify) {', 1)[1].split('} else if', 1)[0]
    assert 'el("p", result.clarify?.message' in branch
    assert '"ai.btn.confirm"' not in branch and 'result.config' not in branch
    assert '["ai.example.equal_weight", "ai.example.momentum"]' in branch
    assert 'state.idea = t(key)' in branch and 'result.clarify.examples' not in source
    assert 'el("p", refusal.message' in source and 'if (refusal.fields?.length)' in source
    helper = source.split('function el(', 1)[1].split('function button(', 1)[0]
    assert 'node.textContent = String(text)' in helper
    test_frontend_all_design_keys_have_both_languages()
    test_frontend_every_used_translation_key_exists()


def test_s13_isolated_gate_process_pipeline():
    # 临时 schema + 真实隔离 Gate 进程；验证接线，不能替代研究项目 schema 的验收。
    for doc in (eq_cfg(), _control("reject", "Please name signal.", "MISSING_REQUIRED"),
                _control("reject", "signal.embargo is too small.", "EMBARGO_VIOLATION")):
        with _api_credential(), _api_server(doc) as (url, received), _s12_setup(url, []) as (root, _):
            service = api.API(root)
            try:
                result = _translated(service, "Notebook configuration please.")
                assert len(received) == 1 and _rows(root)[0]["gate"]["legal"]
                if "reject" in doc:
                    assert result["refusal"]["code"] == doc["reject"]["code"]
                    assert result["config"] is None and not result["can_confirm"]
                else:
                    assert result["config"] == doc and result["can_confirm"]
                    assert _call(service, "confirm", {"request_id": result["request_id"]})[0] == 200
            finally:
                service.close()


def test_s13_prompt_protocol_and_growth_proxies():
    from src.ai_translator.api_prompt import EXAMPLES, SYSTEM_PROMPT
    assert {backend.output_kind(doc) for _, doc in EXAMPLES} == {"config", "reject", "clarify"}
    assert {doc["reject"]["code"] for _, doc in EXAMPLES if "reject" in doc} >= {"MISSING_REQUIRED", "EMBARGO_VIOLATION"}
    assert 'embargo<h+1 FIRST' in SYSTEM_PROMPT and '1-200 characters' in SYSTEM_PROMPT
    # S11 基线；本机无供应商 tokenizer，用三种代理约束增长，不声称精确 token 数。
    assert len(SYSTEM_PROMPT) <= 5509 * 1.25
    assert len(SYSTEM_PROMPT.encode()) <= 5713 * 1.25
    assert len(re.findall(r'[\u4e00-\u9fff]|[A-Za-z0-9_]+|[^\w\s]', SYSTEM_PROMPT)) <= 1843 * 1.25


_S13_SOCKET_TESTS = {
    "test_s11_bad_output_timeout_http_and_redirect_never_retry", "test_s11_missing_key_and_key_never_persisted_or_returned",
    "test_s11_budget_daily_persist_restart_and_missing_usage", "test_s11_usage_rollover_corruption_and_file_lock",
    "test_s13_all_existing_reject_codes_pass_protocol_and_original_gate", "test_s13_clarify_no_config_gate_or_confirmation",
    "test_s13_config_gate_consistency_confirmation_and_field_reasons_preserved", "test_s13_strict_invalid_shapes_messages_and_unknown_keys",
    "test_s13_isolated_gate_process_pipeline",
}


def test_s13_without_socket_transport_regression():
    """沙箱不能 bind 时额外验证同一组断言；不代替本机假 HTTP 服务器验收。"""
    import io

    @contextlib.contextmanager
    def transport(doc=None):
        received = []

        def open_(opener, req, *, timeout):
            body = json.loads(req.data)
            assert set(body) == {"model", "max_tokens", "thinking", "output_config", "system", "messages"}
            received.append({"body": body})
            return io.BytesIO(json.dumps({"content": [{"type": "text", "text": json.dumps(doc)}],
                "stop_reason": "end_turn", "usage": {"input_tokens": 100, "output_tokens": 200}}).encode())

        with mock.patch.object(api_llm.request.OpenerDirector, "open", new=open_):
            yield "http://127.0.0.1:1", received

    with mock.patch.dict(globals(), {"_api_server": transport}):
        for fn in (test_s13_clarify_no_config_gate_or_confirmation,
                   test_s13_all_existing_reject_codes_pass_protocol_and_original_gate,
                   test_s13_strict_invalid_shapes_messages_and_unknown_keys,
                   test_s13_config_gate_consistency_confirmation_and_field_reasons_preserved,
                   test_s13_isolated_gate_process_pipeline):
            fn()
            print("ok", fn.__name__, "[memory transport]")


_S13_REGRESSIONS = {
    "test_backend_is_abstract", "test_make_result_contract", "test_error_codes", "test_extract_patterns",
    "test_extract_date_roles", "test_precheck_p_embargo", "test_precheck_p_date_positive", "test_precheck_p_date_negative",
    "test_api_16_17_logs_complete_private_and_no_transport_secrets",
    "test_api_18_confirm_warn_server_copy_duplicate_expiry_and_lru", "test_api_18_concurrent_confirmation_appends_once",
    "test_api_29_fake_contract_timeout_errors_and_gate_unavailable",
    "test_frontend_safe_text_and_single_status_request", "test_frontend_all_design_keys_have_both_languages",
    "test_frontend_every_used_translation_key_exists", "test_s11_request_parameters_and_text_contract_without_network",
    "test_s11_bad_output_timeout_http_and_redirect_never_retry", "test_s11_missing_key_and_key_never_persisted_or_returned",
    "test_s11_budget_daily_persist_restart_and_missing_usage", "test_s11_usage_rollover_corruption_and_file_lock",
    "test_s11_api_configuration_and_destination_allowlist",
}


if __name__ == "__main__":
    if _S13_NO_SOCKET:
        print("S13 无 socket 补充验证；未运行本机假 HTTP，不能代替沙箱外重跑。")
    if _ISOLATED:
        def isolated_boundary(event, args):
            if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
                raw_path = Path(os.fsdecode(args[0]))
                # 临时目录清理通过 dir_fd 打开相对名字，audit 不提供该 fd；
                # 被测路径工厂全部返回绝对路径，这里只核查可确定的绝对访问。
                if not raw_path.is_absolute():
                    return
                p = raw_path.resolve()
                if (REPO / "data" in p.parents or p == REPO / "data"
                        or any(part in (".secret", "ai_layer", "sealed") for part in p.parts)
                        or "test2" in p.name.lower()):
                    raise AssertionError("隔离测试越过读取边界")
            if event == "socket.connect" and isinstance(args[1], tuple):
                if args[1][0] not in ("127.0.0.1", "::1", "localhost"):
                    raise AssertionError("隔离测试禁止外网")
        sys.addaudithook(isolated_boundary)
    n, skipped, failures = 0, [], []
    for name, fn in sorted(globals().items()):
        selected = (name.startswith("test_s13_") or name in _S13_REGRESSIONS) if _S13_ONLY else name.startswith("test_s12_" if _S12_ONLY else "test_")
        if _S13_NO_SOCKET and name in _S13_SOCKET_TESTS:
            continue
        if selected and callable(fn):
            try:
                fn()
            except unittest.SkipTest as e:      # 含 Skip 和 skip_unless 装饰器抛出的跳过
                skipped.append(name)
                print("SKIP", name, e)
                continue
            except Exception:
                if not _ISOLATED:
                    raise
                import traceback
                traceback.print_exc()
                failures.append(name)
                print("FAIL", name)
                continue
            n += 1
            print("ok", name)
    print(f"{n} 项通过，{len(skipped)} 项跳过" + (f"：{skipped}" if skipped else ""))
    if _ISOLATED:
        stage = "S13" if _S13_ONLY else "S12"
        print(f"{stage} 失败：{len(failures)} 项" + (f"：{failures}" if failures else ""))
        sys.exit(1 if failures else 0)
    for pid in sorted(_fake_pids):
        _assert_pid_gone(pid)
    print(f"假 worker/孙进程残留检查：{len(_fake_pids)} 个 pid 均已消失（os.kill(pid, 0)）")
    assert external_fingerprint() == EXTERNAL_BEFORE, "测试改动了外部受保护文件"
    print("外部文件字节哈希守卫：登记、运行记录、适配器、试验记录均未变化")
    res = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ZRealDataUntouchedTests))
    sys.exit(0 if res.wasSuccessful() else 1)
