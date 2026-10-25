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
    """detail 条目 → {slug: 规范化前 N 字符}（仅 **active** 条目参与——
    dormant/superseded 不参与同源判定，口径与门②生产版一致，2026-10-04 单源收编）。"""
    from lib.fs import norm
    return {d["name"]: norm(d["body"])[:n]
            for d in items
            if d.get("fm", {}).get("status", "").strip().lower() == "active" and d.get("body")}


def is_same_source(detail_body: str, entry_text: str, n: int = 30) -> bool:
    """同源判定（单源收编 2026-10-04，取门②生产语义）：detail 正文规范化前 N 字符
    被 L1 条目规范化文本**包含**（单向）。旧版双向包含（nd in ne or ne in nd）
    从无调用方，且双向会把「L1 条目是 detail 前缀」误判同源。"""
    from lib.fs import norm
    nd = norm(detail_body)[:n]
    ne = norm(entry_text)
    return bool(nd) and bool(ne) and nd in ne


# ── pending 候选操作 ─────────────────────────────────────────────────

PENDING_SKIP_NAMES = ("README.md", ".watermark")
PENDING_SKIP_SUFFIXES = (".done.md", ".rejected.md")


def is_pending_active(name: str) -> bool:
    """pending 目录里待处理文件名判定（parse_candidates / mark_consumed / 门①消费判定共用）。"""
    return name not in PENDING_SKIP_NAMES and not name.endswith(PENDING_SKIP_SUFFIXES)


def parse_candidates(pending_dir: Path) -> list[dict]:
    """读取 pending 目录 .md（排除已消费/拒绝）→ 候选列表。"""
    cands = []
    if not pending_dir.exists():
        return cands
    _idx = 0
    for f in sorted(pending_dir.glob("*.md")):
        if not is_pending_active(f.name):
            continue
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for ln in lines:
            # 剥离任务 checkbox 前缀（上游提取管道泄漏）："- [ ] [fact] x"、"- [] x"、"- [x] y"
            # 旧版会把前者解析出空 type、后者整行静默丢弃
            ln2 = re.sub(r"^\s*-\s*\[\s*[xX]?\s*\]\s+", "- ", ln)
            m = re.match(r"^\s*-\s*\[([^\]]+)\]\s*(.+)$", ln2)
            if m:
                cands.append({
                    "file": f.name, "raw": ln.strip(), "idx": _idx,
                    "ctype": m.group(1).strip(), "text": m.group(2).strip(),
                })
                _idx += 1
            elif ln2 != ln:
                # 剥过 checkbox 但无类型标注 → 仍作候选（type 交给门① LLM/规则判定）
                text = ln2.strip().lstrip("-").strip()
                if text:
                    cands.append({
                        "file": f.name, "raw": ln.strip(), "idx": _idx,
                        "ctype": "", "text": text,
                    })
                    _idx += 1
    return cands


def mark_consumed(pending_dir: Path, only: set[str] | None = None) -> int:
    """消费后把 pending 文件改名 .done.md。返回改名数。（Q22）

    only=None：消费全部待处理文件（旧行为）。
    only=文件名集合：只消费集合内文件——门①截断/失败时未达终态的文件
    留在 pending，下轮继续（P0-1：防截断候选被静默吞掉）。
    """
    n = 0
    for f in sorted(pending_dir.glob("*.md")):
        if not is_pending_active(f.name):
            continue
        if only is not None and f.name not in only:
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
