#!/usr/bin/env python3
"""中英文词条一致性检查(只读):对比 zh.json 与 en.json 的键,列出缺失与空值。

不修改任何文件。示例:.venv/bin/python scripts/i18n_key_diff.py
退出码恒为 0(只做报告)。
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D = os.path.join(ROOT, "dashboard", "ui", "i18n")


def load(name):
    with open(os.path.join(D, name), encoding="utf-8") as f:
        return json.load(f)


def main():
    zh, en = load("zh.json"), load("en.json")
    only_zh = sorted(set(zh) - set(en))
    only_en = sorted(set(en) - set(zh))
    empty = sorted(k for k in set(zh) & set(en) if not str(zh[k]).strip() or not str(en[k]).strip())
    # 仍含中文的英文词条(疑似漏翻)
    cjk = sorted(k for k in en if any("一" <= c <= "鿿" for c in str(en[k])))
    print(f"zh 词条 {len(zh)},en 词条 {len(en)}")
    for title, ks in (("只在 zh 有(en 缺失)", only_zh), ("只在 en 有(zh 缺失)", only_en),
                      ("两边都有但有一边为空", empty), ("en 词条里仍含中文", cjk)):
        print(f"\n## {title}: {len(ks)}")
        for k in ks[:50]:
            print("  -", k)
        if len(ks) > 50:
            print(f"  ...另有 {len(ks) - 50} 条")


if __name__ == "__main__":
    main()
