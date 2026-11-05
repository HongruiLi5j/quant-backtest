"""scripts/t008_power_calibration.py 的自检:.venv/bin/python tests/test_t008_power_calibration.py(或 pytest)。

只用合成数据和临时目录,不读写 data/,不碰 handoff/。
导入脚本之前先把 T008_TRIAL_LOG_PATH 指到临时文件,即使误写账本也落在临时目录。
"""
import importlib.util
import json
import math
import os
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
_TMP = tempfile.mkdtemp(prefix="c01_test_")
os.environ["T008_TRIAL_LOG_PATH"] = str(Path(_TMP) / "trial_log.jsonl")
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("t008_power_calibration", ROOT / "scripts" / "t008_power_calibration.py")
pc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pc)

SMALL = {"n_stocks": 60, "n_days": 120}
QUICK_ARGS = ["--quick", "--workers", "1", "--n-stocks", "60", "--n-days", "120",
              "--null-k", "50", "--n-boot", "200", "--top-n", "5"]


def _world(seed, ic, persist=0.0):
    rng = np.random.default_rng(seed)
    n, T = SMALL["n_stocks"], SMALL["n_days"]
    dates = pd.bdate_range(pc.SEG_START, periods=T)
    codes = [f"S{i:04d}" for i in range(n)]
    r = pc.simulate_returns(rng, n, T)
    noise = pc.noise_matrix(rng, T, n, persist)
    ics = pc.ic_path(rng, T, ic, 0.0)
    return r, dates, codes, noise, ics


def test_wilson_ci_known_values():
    lo, hi = pc.wilson_ci(5, 10)
    assert abs(lo - 0.2366) < 1e-3 and abs(hi - 0.7634) < 1e-3      # 教科书值
    lo, hi = pc.wilson_ci(0, 10)
    assert lo == 0.0 and 0.0 < hi < 0.35
    assert abs(pc._norm_ppf(0.975) - 1.959964) < 1e-5


def test_ic_to_loading_is_inverse_of_spearman_formula():
    for ic in (-0.05, 0.0, 0.02, 0.05, 0.3):
        a = pc.ic_to_loading(ic)
        assert abs(6 / math.pi * math.asin(a / 2) - ic) < 1e-12


def test_binom_sf():
    assert abs(pc.binom_sf(0, 20, 0.3) - 1.0) < 1e-12
    assert abs(pc.binom_sf(20, 20, 0.5) - 0.5 ** 20) < 1e-15


def test_noise_matrix_ar_has_unit_variance_and_persistence():
    e = pc.noise_matrix(np.random.default_rng(0), 4000, 20, 0.9)
    assert abs(e.std() - 1.0) < 0.05
    ac = np.mean([np.corrcoef(e[:-1, j], e[1:, j])[0, 1] for j in range(20)])
    assert abs(ac - 0.9) < 0.03


def test_planted_factor_hits_target_ic():
    # 大截面下植入的秩相关应接近目标值(检验 a = 2·sin(π·IC/6) 的换算)
    rng = np.random.default_rng(3)
    y = rng.standard_normal((200, 2000))
    f = pc.plant_factor(y, rng.standard_normal((200, 2000)), np.full(200, 0.05))
    assert abs(pc._rank_ic(f, y) - 0.05) < 0.005


def test_rule4_strong_signal_passes_and_reports_ic():
    r, dates, codes, noise, ics = _world(1, 0.4)
    fwd = pc.monotonic.forward_returns(pc.build_panel(r, dates, codes))
    # 面板约定:open[t+1] = close[t],所以 20 日标签 = close[t+20]/close[t]-1
    close = 10.0 * np.cumprod(1.0 + r, axis=0)
    assert np.allclose(fwd.to_numpy()[0], close[20] / close[0] - 1)
    assert fwd.iloc[-20:].isna().all().all()                      # 段尾 20 天没有标签
    f = pc.plant_factor(fwd.to_numpy(float), noise, ics)
    res = pc.rule4_eval(pd.DataFrame(f, index=fwd.index, columns=fwd.columns), fwd, 200)
    assert res["r4_pass"] and res["r4_ic_real"] > 0.3


def test_rule1_strong_negative_signal_triggers_positive_does_not():
    for ic, expect in ((-0.4, True), (0.4, False)):
        r, dates, codes, noise, ics = _world(2, ic)
        label = np.full_like(r, np.nan)
        label[:-1] = r[1:]
        f = pc.plant_factor(label, noise, ics)
        res = pc.rule1_eval(f, r, dates, codes, top_n=5, null_k=50, seed="t")
        assert res["r1_evaluable"] and res["r1_triggered"] is expect
        assert res["r1_pass"] is (not expect)


def test_main_quick_writes_outputs_and_leaves_ledgers_alone():
    out = Path(_TMP) / "out"
    rc = pc.main(QUICK_ARGS + ["--reps", "2", "--out", str(out)])
    assert rc == 0
    for name in ("raw_results.csv", "summary.csv", "ledger_proof.json", "run_meta.json"):
        assert (out / name).exists(), name
    proof = json.loads((out / "ledger_proof.json").read_text(encoding="utf-8"))
    assert proof["unchanged"] is True
    assert proof["trial_log_env"].startswith(_TMP)
    assert proof["tmp_trial_log_exists_after"] is False                 # 模拟不写任何账本
    raw = pd.read_csv(out / "raw_results.csv")
    assert len(raw) == 8 and set(raw["scenario"]) == {"r1_iid", "r4_iid"}
    s = pd.read_csv(out / "summary.csv")
    assert s.loc[s["scenario"] == "r1_iid", "joint_pass_count"].isna().all()     # 规则 1 场景没跑规则 4,不算联合
    j = s[(s["scenario"] == "r4_iid") & (s["ic_target"] == 0.0)]
    assert len(j) == 1 and j["joint_pass_count"].notna().all()


def test_rep_start_batches_merge_and_match_single_run():
    # 分两批(重复号 0 和 1)再合并,结果应与一次跑 2 次完全相同(种子只由重复号决定)
    one, b1, b2, merged = (Path(_TMP) / n for n in ("one", "b1", "b2", "merged"))
    assert pc.main(QUICK_ARGS + ["--reps", "2", "--ics", "0", "--out", str(one)]) == 0
    assert pc.main(QUICK_ARGS + ["--reps", "1", "--ics", "0", "--out", str(b1)]) == 0
    assert pc.main(QUICK_ARGS + ["--reps", "1", "--rep-start", "1", "--ics", "0", "--out", str(b2)]) == 0
    assert pc.main(["--quick", "--merge-from", f"{b1},{b2}", "--out", str(merged)]) == 0
    cols = ["scenario", "ic", "rep", "r1_pass", "r1_score", "r4_pass"]
    a = pd.read_csv(one / "raw_results.csv")[cols]
    b = pd.read_csv(merged / "raw_results.csv")[cols]
    pd.testing.assert_frame_equal(a, b)
    try:                                                              # 同一批合并两次 = 重复种子,必须拒绝
        pc.main(["--quick", "--merge-from", f"{b1},{b1}", "--out", str(Path(_TMP) / "dup")])
    except SystemExit:
        return
    raise AssertionError("重复的重复号应被拒绝合并")


def test_refuses_output_inside_data_or_handoff():
    for bad in (ROOT / "data" / "x", ROOT / "handoff" / "y"):
        try:
            pc._check_out(bad)
        except SystemExit:
            continue
        raise AssertionError(f"应拒绝 {bad}")


def test_script_does_not_call_ledger_writing_functions():
    src = (ROOT / "scripts" / "t008_power_calibration.py").read_text(encoding="utf-8")
    for fn in ("train_eliminate(", "validate_survivors(", "holdout_report(", "TrialLog(", "write_json_once("):
        assert fn not in src, fn


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
