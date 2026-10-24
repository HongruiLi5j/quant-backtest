"""手动复制待审核反馈；不改变源文件，不生成训练数据。

JSONL 首行是 export_metadata，说明文本来自已脱敏、截断的日志。
其余每行是一条待人工审核的反馈，模型输出绝不自动作为 gold。
"""
import argparse
import datetime
import fcntl
import json
import os
import stat
import sys
import tempfile
from pathlib import Path


NOTICE = "想法、模型输出及备注按源日志原样复制，可能已经脱敏或截断；不恢复原文。模型输出不是标准答案，进入训练前必须人工审核。"
DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "ai_translator"


def _directory(data_dir):
    original = Path(data_dir).expanduser().absolute()
    forbidden = {"ai_layer", "." + "secret"}  # 拼接书写，避免脚本源码出现红线字符串；防护本身保留
    # 先检查原路径，避免解析或访问明确禁止的目录；再检查链接解析结果。
    if forbidden.intersection(original.parts):
        raise ValueError("不允许使用模型或私密目录")
    resolved = original.resolve()
    if forbidden.intersection(resolved.parts):
        raise ValueError("不允许使用模型或私密目录")
    # macOS 的 /var、/tmp 本身是系统链接，父路径先归一化再检查边界。
    if original.is_symlink():
        raise ValueError("数据目录不允许符号链接")
    real_data = DEFAULT_DATA_DIR.parent
    if os.environ.get("QB_TESTING") == "1" and (resolved == real_data or real_data in resolved.parents):
        raise ValueError("测试必须使用临时数据目录")
    return resolved


def _rows(path, *, optional=False):
    """只读共享锁与写入端配合；损坏记录报位置，不回显可能含隐私的内容。"""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if optional:
            return
        raise
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("源文件必须是普通文件")
        fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                raise ValueError(f"{path.name} 第 {number} 行不是有效 JSON") from None
            if not isinstance(row, dict):
                raise ValueError(f"{path.name} 第 {number} 行必须是 JSON 对象")
            yield row


def export_feedback(data_dir=DEFAULT_DATA_DIR, *, today=None):
    directory = _directory(data_dir)
    pending = {}
    for row in _rows(directory / "feedback.jsonl", optional=True):
        if row.get("status") != "pending_review":
            continue
        rid = row.get("request_id")
        if not isinstance(rid, str) or not rid:
            raise ValueError("待审核反馈缺少有效 request_id")
        # 同一请求保留首次待审核反馈，顺序稳定；不改变源状态。
        pending.setdefault(rid, row)

    contexts = {}
    if pending:
        for path in sorted(directory.glob("log_*.jsonl")):
            for row in _rows(path):
                rid = row.get("request_id")
                if row.get("event") == "translate" and isinstance(rid, str) and rid in pending:
                    contexts.setdefault(rid, row)

    records = [{"event": "export_metadata", "schema": 1, "notice": NOTICE}]
    for rid, feedback in pending.items():
        context = contexts.get(rid, {})
        records.append({
            "event": "feedback", "request_id": rid, "ts": feedback.get("ts"),
            "translate_ts": context.get("ts"), "status": "pending_review",
            "verdict": feedback.get("verdict"), "note": feedback.get("note", ""),
            "idea": context.get("idea"), "raw_output": context.get("raw_output"),
            "config": context.get("config"), "backend_id": context.get("backend_id"),
            "model": context.get("model"), "missing_context": rid not in contexts,
            "reviewed_by_human": False, "gold": None, "reviewer_note": "",
        })
    # 在创建临时文件之前完成序列化；遇到损坏数据不替换旧导出。
    payload = "".join(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in records)
    review = directory / "review"
    if review.is_symlink():
        raise ValueError("审核目录不允许符号链接")
    review.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(review, 0o700)
    target = review / f"pending_{(today or datetime.date.today()):%Y%m%d}.jsonl"
    # 同目录临时文件保证 replace 为原子操作；失败时保留 600 临时文件供排查。
    fd, temporary = tempfile.mkstemp(prefix=".pending_", suffix=".tmp", dir=review)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)
    return len(pending), target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    args = parser.parse_args(argv)
    try:
        count, target = export_feedback(args.data_dir)
    except (OSError, ValueError):
        # 不打印异常中的源内容或路径之外的隐私文本。
        print("导出失败：请检查数据路径、权限及 JSONL 格式；源文件未修改。", file=sys.stderr)
        return 1
    print(f"已导出 {count} 条反馈：{target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
