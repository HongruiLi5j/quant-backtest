"""pipeline_check 测试：只验证 --out 路径检查，不联网、不读 data/。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import pipeline_check as pc  # noqa: E402


class OutPathTests(unittest.TestCase):
    def test_out_inside_data_rejected(self):
        for bad in (ROOT / "data" / "x.md", ROOT / "data" / "daily" / "y.md", ROOT / "data" / ".." / "data" / "z.md"):
            with self.assertRaises(SystemExit) as cm:
                pc.main(["--expect-day", "2026-10-26", "--out", str(bad)])
            self.assertEqual(cm.exception.code, 2)
            self.assertFalse(Path(bad).exists())


if __name__ == "__main__":
    unittest.main()
