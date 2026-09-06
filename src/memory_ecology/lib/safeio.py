"""安全写路径（memory-ecology lib，v2.2.0 首项——迭代策略「安全写路径加固 schema+入口变更」）。

三层防护：
  1. 路径安全   safe_entry_path：文件名白名单 + 拒绝穿越 + 限root内 + 强制 .md
  2. schema 校验 validate_detail_fm / validate_experience_fm：枚举/日期/计数
  3. 写入原语   write_entry（可选校验+可选备份+原子写）/ append_jsonl（容量可选）

构建于 fs.atomic_write 之上；校验失败抛 ValueError（调用方决定回退或中止）。
"""
import datetime
import json
import re

from . import fs

SAFE_NAME_RE = re.compile(r"^[\w.\-\u4e00-\u9fff]+$")  # \w 含字母数字下划线

DETAIL_TYPES = {"semantic", "episodic", "procedural", "lesson"}
DETAIL_STATUS = {"active", "dormant", "superseded"}
EXPERIENCE_TYPES = {"error", "pattern", "negative", "success", "link"}
EXPERIENCE_STATUS = {"draft", "verified", "dormant", "superseded"}
DATE_KEYS = ("first_seen", "last_seen", "valid_time", "last_verified", "last_hit", "created")
COUNT_KEYS = ("occurrences", "session_count")


def safe_entry_path(root, stem: str):
    """条目路径安全化：stem 白名单 + 限 root 内 + 强制 .md。返回 Path；非法抛 ValueError。"""
    from pathlib import Path
    root = Path(root).resolve()
    stem = str(stem).strip()
    if not stem or ".." in stem or "/" in stem or "\\" in stem:
        raise ValueError(f"非法条目名: {stem!r}")
    if not SAFE_NAME_RE.match(stem):
        raise ValueError(f"条目名含越界字符: {stem!r}")
    p = (root / f"{stem}.md").resolve()
    if p.parent != root:
        raise ValueError(f"路径越界: {p}")
    return p


def _check_enums(fm: dict, types: set, statuses: set, errs: list) -> None:
    t = fm.get("type")
    if t and t not in types:
        errs.append(f"type 非法: {t!r}")
    s = fm.get("status")
    if s and s not in statuses:
        errs.append(f"status 非法: {s!r}")


def _check_dates_counts(fm: dict, errs: list) -> None:
    for k in DATE_KEYS:
        v = fm.get(k)
        if v:
            try:
                datetime.date.fromisoformat(str(v)[:10])
            except ValueError:
                errs.append(f"{k} 非法日期: {v!r}")
    for k in COUNT_KEYS:
        v = fm.get(k)
        if v:
            try:
                if int(v) < 0:
                    errs.append(f"{k} 负数: {v!r}")
            except ValueError:
                errs.append(f"{k} 非整数: {v!r}")


def validate_detail_fm(fm: dict) -> list:
    errs: list = []
    _check_enums(fm, DETAIL_TYPES, DETAIL_STATUS, errs)
    _check_dates_counts(fm, errs)
    return errs


def validate_experience_fm(fm: dict) -> list:
    errs: list = []
    _check_enums(fm, EXPERIENCE_TYPES, EXPERIENCE_STATUS, errs)
    _check_dates_counts(fm, errs)
    return errs


def parse_frontmatter(text: str):
    """最小 frontmatter 解析（--- 块 key: value）→ (fm, body)。"""
    fm: dict = {}
    body = text
    if text.startswith("\ufeff"):
        text = text[1:]
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end > 0:
            for line in text[3:end].strip().splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    fm[k.strip()] = v.strip()
            body = text[end + 4:].strip()
    return fm, body


def write_entry(path, text: str, *, kind: str | None = None, backup_tag: str | None = None) -> None:
    """安全条目写：可选 schema 校验 → 可选 .bak 备份 → 原子写。校验失败抛 ValueError。"""
    from pathlib import Path
    path = Path(path)
    if kind:
        fm, _ = parse_frontmatter(text)
        errs = validate_detail_fm(fm) if kind == "detail" else validate_experience_fm(fm)
        if errs:
            raise ValueError(f"{path.name} schema 校验失败: {'; '.join(errs)}")
    if backup_tag:
        try:
            # R11（v2.2.0 review）：备份也走原子写 + 毫秒时间戳防同秒覆盖
            bak = path.with_name(f"{path.name}.bak-{backup_tag}-{int(datetime.datetime.now().timestamp() * 1000)}")
            fs.atomic_write(bak, path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass  # 备份失败不阻塞写入（fail-open，与生态 hook 同哲学）
    fs.atomic_write(path, text)


def append_jsonl(path, rec: dict, *, max_bytes: int | None = None) -> None:
    """JSONL 追加（单行）；max_bytes 给定时超限则静默停止记账（容量护栏）。"""
    from pathlib import Path
    p = Path(path)
    if max_bytes is not None:
        try:
            if p.exists() and p.stat().st_size > max_bytes:
                return
        except OSError:
            return
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=True) + "\n")
