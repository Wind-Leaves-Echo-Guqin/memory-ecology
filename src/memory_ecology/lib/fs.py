"""文件系统公共函数（memory-ecology lib）：原子写 / slug / 规范化。

零行为改动：实现取自各脚本已有函数（mkstemp 原子写为最稳版本，
最终产物与各脚本现行为等价——os.replace 后临时文件消失）。
"""
import hashlib
import os
import re
import tempfile
from pathlib import Path


def atomic_write(path: Path, text: str) -> None:
    """临时文件 + os.replace 原子写回（失败清理临时文件并重抛）。"""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".eco_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def slug_of(text: str) -> str:
    """从文本生成文件名 slug（中文保留，截断+哈希防重）。"""
    s = re.sub(r"[\s\W_]+", "-", text.strip())[:40].strip("-")
    if not s:
        s = "mem"
    return f"{s}-{hashlib.md5(text.encode('utf-8')).hexdigest()[:6]}"


def norm(s: str) -> str:
    """规范化：去空白与标点（相似比较用；中文保留）。"""
    return re.sub(r"[\s\W_]+", "", s)
