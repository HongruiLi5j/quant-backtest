"""真实数据守卫：任何测试都不应该读写 data/ 下的真实文件。

用法：在测试文件顶部加一行
    from realdata_guard import ZRealDataUntouchedTests  # noqa: F401
导入时（也就是测试开始前）先取受保护文件的内容哈希；全部测试跑完后由 ZRealDataUntouchedTests 比对，有任何新增/修改/删除都会失败。
类名以 Z 开头，unittest 按字母顺序会最后运行；pytest 按定义顺序，它在文件里导入，也排在本文件测试之前——
所以这里的比对只保证“从导入到它运行之间”没有改动，请把 import 放在其他导入之后、测试类定义之前。

注意：如果每日定时任务恰好在测试期间运行，也会引起差异——先看 logs/daily.log，不要直接忽略。
"""
import hashlib
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 每日任务 / 账本 / 模拟盘 / 清单 / 跟踪 会写入的真实文件
PROTECTED_GLOBS = [
    'data/signal_ledger.db', 'data/simtrade.db', 'data/members.db', 'data/main_strategy.json', 'data/auto_params.json',
    'data/daily/ledger/**/*', 'data/daily/simtrade/**/*', 'data/daily/trade_list/**/*', 'data/daily/trade_list_cn.json',
    'data/daily/trade_list_attempts.json', 'data/daily/trade_signals_*', 'data/daily/turnover_*', 'data/daily/signals_history.parquet',
    'data/daily/notifications.json', 'data/daily/summary_*.json', 'data/daily/tracking_*.json', 'data/daily/scan_*',
    'data/backups/**/*',
]


def fingerprint(root=ROOT):
    """{相对路径: sha256}；文件不存在就不出现。新增、删除、修改都会让两次指纹不同。"""
    out = {}
    for pat in PROTECTED_GLOBS:
        for f in sorted(Path(root).glob(pat)):
            if f.is_file():
                out[str(f.relative_to(root))] = hashlib.sha256(f.read_bytes()).hexdigest()
    return out


BEFORE = fingerprint()


class ZRealDataUntouchedTests(unittest.TestCase):
    def test_real_data_files_unchanged(self):
        after = fingerprint()
        changed = sorted(k for k in set(BEFORE) | set(after) if BEFORE.get(k) != after.get(k))
        self.assertEqual(changed, [], f'测试改动了真实数据文件：{changed}')

    def test_guard_detects_changes(self):
        """守卫本身有效：在临时目录里模拟 修改 / 新增 / 删除，指纹都必须变化（不碰真实目录）。"""
        with tempfile.TemporaryDirectory(prefix='quant_guard_') as tmp:
            root = Path(tmp)
            (root / 'data' / 'daily' / 'ledger').mkdir(parents=True)
            db = root / 'data' / 'signal_ledger.db'
            db.write_bytes(b'v1')
            base = fingerprint(root)
            self.assertEqual(list(base), ['data/signal_ledger.db'])
            db.write_bytes(b'v2')
            self.assertNotEqual(fingerprint(root), base)         # 修改
            db.write_bytes(b'v1')
            self.assertEqual(fingerprint(root), base)            # 恢复
            (root / 'data' / 'daily' / 'ledger' / 'latest.json').write_text('{}')
            self.assertNotEqual(fingerprint(root), base)         # 新增
            (root / 'data' / 'daily' / 'ledger' / 'latest.json').unlink()
            db.unlink()
            self.assertNotEqual(fingerprint(root), base)         # 删除
