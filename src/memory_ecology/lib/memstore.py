"""memstore — L1/L2/detail/experiences 唯一读写层（PORT_SPEC §C）。

此前 parse_frontmatter / load_detail / parse_candidates 分散在
write_gate / eco_quota / collector / eco_note_query 四处，各自实现。
本模块收敛后，任何一个脚本改格式只改这里。

零依赖（stdlib only）——勿在此 import three/llm/web 任何东西。
"""
from __future__ import annotations
import re
from pathlib import Path


# ── frontmatter 解析/生成 ────────────────────────────────────────────

def parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 YAML 简化 frontmatter（--- 块），返回 (dict, 正文)。
    兼容 BOM/CRLF。实现取自 write_gate.py（比 regex 版更健壮）。"""
    if text.startswith("\ufeff"):
        text = text[1:]
    fm: dict = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end > 0:
            block = text[3:end].strip()
            body = text[end + 4:].strip()
            for line in block.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    fm[k.strip()] = v.strip()
    return fm, body


def dump_frontmatter(fm: dict, body: str) -> str:
    """frontmatter dict + body → md 文本（--- 块包裹，正文前空一行）。"""
    lines = ["---"]
    for k, v in fm.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    return "\n".join(lines) + "\n"


def clean_value(s) -> str:
    """极简 YAML 标量清理：去首尾空白、去尾注释、剥引号。"""
    s = (s or "").strip()
    idx = s.find(" #")
    if idx != -1:
        s = s[:idx].strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        s = s[1:-1]
    return s.strip()


def parse_frontmatter_strict(text: str) -> tuple[dict | None, str]:
    """严格版 frontmatter 解析：缺失/损坏 → (None, 全文)。

    与 parse_frontmatter（宽松版，返回空 dict）的语义差异正是它存在的理由——
    复核门要区分「无 frontmatter」和「字段为空」，宽松版无法表达。
    逐字移植自 eco_review.parse_frontmatter（含 clean_value 取值口径），零行为差异。
    """
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n").strip() != "---":
        return None, text
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n").strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        return None, text
    fields: dict = {}
    for ln in lines[1:end_idx]:
        s = ln.rstrip("\r\n").strip()
        if not s or s.startswith("#") or ":" not in s:
            continue
        key, _, val = s.partition(":")
        fields[key.strip()] = clean_value(val)
    body = "".join(lines[end_idx + 1:]).strip()
    return fields, body


# ── L1 操作 ──────────────────────────────────────────────────────────

def parse_l1(raw: str) -> tuple[list[str], str]:
    """解析 L1 文件文本 → (条目列表, 换行风格)。分隔符 = 单独一行的 §。"""
    nl = "\r\n" if "\r\n" in raw else "\n"
    parts = re.split(r"(?m)^[ \t]*§[ \t]*\r?$", raw)
    entries = [p.strip() for p in parts if p.strip()]
    return entries, nl


def chars_of(entries) -> int:
    """条目列表的字符数：\\n§\\n 连接 + 末尾 \\n（内部 \\r\\n 归一为 \\n 计 1）。"""
    if not entries:
        return 0
    return len("\n§\n".join(e.replace("\r\n", "\n") for e in entries) + "\n")


def serialize_l1(entries, nl: str) -> str:
    """把条目序列化回 § 分隔格式，保持文件原有换行风格，末尾补换行。"""
    if not entries:
        return ""
    return (nl + "§" + nl).join(entries) + nl


# ── L2 detail 操作 ───────────────────────────────────────────────────

def load_detail(detail_dir: Path) -> list[dict]:
    """读取 detail 目录全部 .md → [{path, name, fm, body}]。"""
    items = []
    if not detail_dir.exists():
        return items
    for f in sorted(detail_dir.glob("*.md")):
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, body = parse_frontmatter(text)
        if not body:
            continue
        items.append({"path": f, "name": f.stem, "fm": fm, "body": body})
    return items


def detail_prefixes(items: list[dict], n: int = 30) -> dict:
    """detail 条目 → {slug: 规范化前 N 字符}（供 L1 同源映射）。"""
    from lib.fs import norm
    return {d["name"]: norm(d["body"])[:n] for d in items if d.get("body")}


def qualified_prefixes(items: list[dict], n: int = 30) -> set:
    """status=active 的 detail 条目规范化前缀集合。"""
    from lib.fs import norm
    return {norm(d["body"])[:n] for d in items
            if d.get("fm", {}).get("status") == "active" and d.get("body")}


def is_same_source(detail_body: str, entry_text: str, n: int = 30) -> bool:
    """判定 L1 条目是否与 detail 正文同源（规范化前 N 字符比对）。"""
    from lib.fs import norm
    nd = norm(detail_body)[:n]
    ne = norm(entry_text)[:n]
    return bool(nd and ne and (nd in ne or ne in nd))


# ── pending 候选操作 ─────────────────────────────────────────────────

def parse_candidates(pending_dir: Path) -> list[dict]:
    """读取 pending 目录 .md（排除已消费/拒绝）→ 候选列表。"""
    cands = []
    if not pending_dir.exists():
        return cands
    _idx = 0
    for f in sorted(pending_dir.glob("*.md")):
        if f.name in ("README.md", ".watermark") or f.name.endswith((".done.md", ".rejected.md")):
            continue
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for ln in lines:
            m = re.match(r"^\s*-\s*\[([^\]]+)\]\s*(.+)$", ln)
            if m:
                cands.append({
                    "file": f.name, "raw": ln.strip(), "idx": _idx,
                    "ctype": m.group(1).strip(), "text": m.group(2).strip(),
                })
                _idx += 1
    return cands


def mark_consumed(pending_dir: Path) -> int:
    """消费后把 pending 文件改名 .done.md。返回改名数。（Q22）"""
    n = 0
    for f in sorted(pending_dir.glob("*.md")):
        if f.name in ("README.md", ".watermark") or f.name.endswith((".done.md", ".rejected.md")):
            continue
        try:
            f.rename(f.with_name(f.name + ".done.md"))
            n += 1
        except OSError:
            pass
    return n


# ── experiences 操作 ─────────────────────────────────────────────────

def parse_experience(text: str) -> dict | None:
    """解析 exp-*.md → {meta, fields, body, full}；无 frontmatter 返回 None。"""
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        return None
    meta = {}
    for line in m.group(1).splitlines():
        fm = re.match(r"^(\w+):\s*(.*)$", line)
        if fm:
            meta[fm.group(1)] = fm.group(2)
    body = m.group(2)
    fields = {}
    for line in body.splitlines():
        fm = re.match(r"^(\w+):\s*(.*)$", line)
        if fm:
            fields[fm.group(1)] = fm.group(2)
    return {"meta": meta, "fields": fields, "body": body, "full": text}
