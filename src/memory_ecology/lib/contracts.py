"""contracts — 类型化数据契约（PORT_SPEC §D）。

dataclass 定义 CLI --format json 与未来 FastAPI 共用的数据模型。
零依赖（stdlib dataclasses），pydantic 在观测门面按需转换。

命名约定：
  字段名 = CLI JSON 输出键名 = collector API 字段名
  （一处定义，三处引用——CLI / REST / 内部调用）
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class SkillEntry:
    """技能条目（对应 collector.scan_skills 的每项）。"""
    name: str
    cat: str = ""
    desc: str = ""
    version: str = "?"
    status: str = "undeclared"
    fate: str = "undeclared"
    evolved_from: str | None = None
    merged_into: str | None = None
    related: list[str] = field(default_factory=list)
    indeg: int = 0
    dup: bool = False
    chars: int = 0


@dataclass
class L1Status:
    """L1 文件水位状态。"""
    name: str = "MEMORY.md"
    chars: int = 0
    raw_chars: int = 0
    quota: int = 2550
    entries: list[str] = field(default_factory=list)


@dataclass
class GateAction:
    """门操作的输出记录。"""
    action: str           # ADD / UPDATE / NOOP / CONFLICT / idle
    target: str = ""
    note: str = ""
    fingerprint: str = ""


@dataclass
class SearchResult:
    """检索结果条目（三检索 CLI --format json 的统一 schema）。"""
    name: str
    version: str = "?"
    status: str = "?"
    fate: str = "?"
    indeg: int = 0
    desc: str = ""
    score: float = 0.0
    source: str = ""  # "eco_search" | "eco_note_query" | "eco_note_error_query"


@dataclass
class GateDecisionRecord:
    """注入判定影子记账条目。"""
    ts: str
    unit_id: str
    decision: str       # keep / shorten / drop
    confidence: str     # high / medium / low
    reason: str
    signals: list[str] = field(default_factory=list)
