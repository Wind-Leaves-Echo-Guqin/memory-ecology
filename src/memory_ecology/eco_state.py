#!/usr/bin/env python3
"""生态两轴状态机模块（memory-skill-ecosystem 项目 4.1 节）。

status（活性轴，可逆）: active | dormant | frozen
fate（存续轴，不可逆但可回溯）: retained | superseded | merged | archived
merged = 过渡态（合并即转 dormant/archived，非驻留）。

合法组合矩阵（设计稿 4.1）:
| status \ fate | retained | superseded | merged | archived |
| active        |   ✓     |     ✗     |   ✗   |    ✗     |
| dormant       |   ✓     |     ✓     |   ✗   |    ✓     |
| frozen        |   ✓     |     ✗     |   ✗   |    ✓     |

供 eco_breed / 体检脚本 / 未来自动化共用。
"""
from __future__ import annotations

import re
from pathlib import Path

STATUSES = ("active", "dormant", "frozen", "candidate", "undeclared")
FATES = ("retained", "superseded", "merged", "archived", "undeclared")

# 合法组合（candidate 是 MVP 过渡态：只允许 candidate/retained）
VALID = {
    ("active", "retained"),
    ("dormant", "retained"),
    ("dormant", "superseded"),
    ("dormant", "archived"),
    ("frozen", "retained"),
    ("frozen", "archived"),
    ("candidate", "retained"),
}

# 合法转换（设计稿 4.1；供未来自动化校验，MVP 期仅登记）
VALID_TRANSITIONS = {
    ("active/retained", "dormant/retained"),      # 闲置
    ("dormant/retained", "active/retained"),      # 复活
    ("active/retained", "frozen/retained"),       # 环境切换
    ("frozen/retained", "active/retained"),       # 回归
    ("dormant/retained", "frozen/retained"),
    ("frozen/retained", "dormant/retained"),
    ("active/retained", "dormant/superseded"),    # 被取代，观察期
    ("dormant/superseded", "dormant/archived"),   # 验证期过退场
    ("dormant/superseded", "active/retained"),    # 取代回滚
    ("active/retained", "dormant/archived"),      # 合并（经 merged 过渡）
    ("frozen/retained", "frozen/archived"),       # 长眠待查
}

REQUIRED_FIELDS = ("name", "description", "version", "status", "fate",
                   "domain", "environment", "merged_into", "evolved_from")


def parse_frontmatter(text: str) -> dict:
    """解析 SKILL.md frontmatter；缺字段补默认值。"""
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    fm: dict = {}
    if m:
        for line in m.group(1).splitlines():
            if ":" in line and not line.startswith(" "):
                k, v = line.split(":", 1)
                fm[k.strip().strip('"')] = v.strip().strip('"')
    fm.setdefault("status", "undeclared")
    fm.setdefault("fate", "undeclared")
    return fm


def validate(state: str, fate: str) -> tuple[bool, str]:
    """校验 (status, fate) 组合。返回 (合法?, 说明)。"""
    if state not in STATUSES:
        return False, f"未知 status={state}"
    if fate not in FATES:
        return False, f"未知 fate={fate}"
    if (state, fate) in VALID:
        return True, "合法"
    if state == "undeclared" or fate == "undeclared":
        return True, "未声明（体检口径：不算非法，建议补全）"
    return False, f"非法组合 {state}/{fate}"


def scan_skills(root: Path) -> list[dict]:
    """扫描技能目录，返回每技能的状态校验结果。"""
    out = []
    for p in sorted(root.rglob("SKILL.md")):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm = parse_frontmatter(text)
        ok, why = validate(fm.get("status", "undeclared"), fm.get("fate", "undeclared"))
        out.append({"name": p.parent.name, "path": str(p),
                    "status": fm.get("status"), "fate": fm.get("fate"),
                    "valid": ok, "why": why})
    return out


if __name__ == "__main__":
    # 自测：合法/非法矩阵 + 缺省
    tests = [
        ("active", "retained", True), ("active", "archived", False),
        ("dormant", "superseded", True), ("dormant", "merged", False),
        ("frozen", "archived", True), ("frozen", "superseded", False),
        ("candidate", "retained", True), ("active", "merged", False),
        ("undeclared", "undeclared", True),
    ]
    fails = 0
    for st, fa, want in tests:
        ok, why = validate(st, fa)
        mark = "✅" if ok == want else "❌"
        if ok != want:
            fails += 1
        print(f"{mark} validate({st}, {fa}) -> {ok} (期望 {want}) {why}")
    print(f"\n自测 {'通过' if fails == 0 else f'{fails} 失败'}")
    sys_exit = 1 if fails else 0
    import sys
    sys.exit(sys_exit)
