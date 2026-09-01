#!/usr/bin/env python3
"""生态变体创建命令 breed（memory-skill-ecosystem 项目 3.5 节，进化种子，MVP 即有）。

用法:
  python eco_breed.py --name <新技能名> --sources <亲代1,亲代2> --motivation <动机> [--domain <域>] [--dry-run]

行为:
  - 读亲代 SKILL.md 的 frontmatter（name/version）与 references 段
  - 生成候选技能：frontmatter 九字段完整（status=candidate, evolved_from=亲代血缘）
  - 继承 = 引用亲代已验证 references 段（不复制正文）
  - 写入候选区 skills/.candidates/<name>/SKILL.md（隔离，不直接进生态）
  - 亲代保留（不合并、不删除）
"""
import argparse
import datetime
import re
import shutil
import sys
from pathlib import Path

from lib.config import hermes_root

HERMES = hermes_root()
SKILLS = HERMES / "skills"
CANDIDATES = SKILLS / ".candidates"


def read_skill(p: Path) -> tuple[dict, str, str]:
    """返回 (frontmatter dict, references 段文本, 正文开头)。"""
    text = p.read_text(encoding="utf-8", errors="replace")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    fm = {}
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                fm[k.strip()] = v.strip()
    refs = ""
    rm = re.search(r"## References?\n(.*?)(?=\n## |\Z)", text, re.S)
    if rm:
        refs = rm.group(1).strip()
    return fm, refs, text


def find_skill(name: str) -> Path | None:
    for p in SKILLS.rglob("SKILL.md"):
        if p.parent.name == name:
            return p
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--sources", required=True, help="逗号分隔的亲代技能名")
    ap.add_argument("--motivation", required=True)
    ap.add_argument("--domain", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    src_names = [s.strip() for s in args.sources.split(",") if s.strip()]
    if not src_names:
        print("错误：至少一个亲代"); return 1

    # 读亲代
    parents = []
    for n in src_names:
        p = find_skill(n)
        if not p:
            print(f"错误：找不到技能 {n}"); return 1
        fm, refs, _ = read_skill(p)
        parents.append({"name": n, "path": p, "version": fm.get("version", "?"), "refs": refs})
        print(f"  亲代: {n} (v{fm.get('version','?')})")

    # 组装新技能
    today = datetime.date.today().isoformat()
    refs_block = ""
    for pr in parents:
        if pr["refs"]:
            refs_block += f"\n### 继承自 {pr['name']}（已验证 references）\n{pr['refs']}\n"
    if not refs_block:
        refs_block = "\n（亲代无 references 段）\n"

    body = f"""---
name: {args.name}
description: "（待补）{args.motivation[:50]}"
version: 0.1.0
status: candidate
fate: retained
domain: {args.domain or '未定'}
environment: home
merged_into: ""
evolved_from: [{', '.join(src_names)}]
---

# {args.name}

> **候选技能（candidate）**：由 breed 命令创建于 {today}。
> 动机：{args.motivation}
> 血缘：evolved_from = {', '.join(src_names)}（亲代保留，不合并不删除）

## 用途

（待填写：本技能解决什么问题、何时触发）

## 继承的已验证引用

{refs_block}
## 待办

- [ ] 补全 description / 触发条件
- [ ] 验证引用段可用（skill_view 实测）
- [ ] 评估通过后移入生态（skills/ 正式区）并置 status=active
"""
    out = CANDIDATES / args.name / "SKILL.md"
    if args.dry_run:
        print(f"\n[dry-run] 将写入: {out}")
        print(body[:600])
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding="utf-8")
    print(f"\n✅ 候选技能已创建: {out}")
    print(f"   血缘: {', '.join(src_names)} → {args.name}（亲代保留）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
