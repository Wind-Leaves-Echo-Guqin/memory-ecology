#!/usr/bin/env python3
"""生态检索 eco_search 静态版（memory-skill-ecosystem 项目 3.4 节）。

两级路由：技能能力记录 + workflows 项目记录。静态级联排序：
忍耐范围（tolerance，可选）→ 活性（status）→ 存续（fate）→ 版本 → 生态位重要度（in-degree）。
结果 top5 一行摘要 + 依赖链预检 + 已加载标注。

用法:
  python eco_search.py <关键词>          # 检索（自动重建索引若过期）
  python eco_search.py --rebuild         # 强制重建索引
"""
import datetime
import re
import sqlite3
import sys
from pathlib import Path

from lib.config import hermes_root

HERMES = hermes_root()
SKILLS = HERMES / "skills"
AGENT_INDEX = Path.home() / ".memory-ecology"
DB_PATH = HERMES / "ecosystem.db"
MAX_AGE = 86400  # 索引 1 天重建一次（增量原则）

STATUS_RANK = {"active": 0, "candidate": 1, "dormant": 2, "frozen": 3, "undeclared": 4}
FATE_RANK = {"retained": 0, "superseded": 1, "archived": 2, "merged": 3, "undeclared": 4}


def _version_key(v: str) -> tuple:
    parts = re.findall(r"\d+", v or "0")
    return tuple(int(x) for x in parts[:3]) or (0,)


def rebuild(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        DROP TABLE IF EXISTS skills; DROP TABLE IF EXISTS workflows;
        CREATE TABLE skills(
          name TEXT PRIMARY KEY, path TEXT, description TEXT, version TEXT,
          status TEXT, fate TEXT, tolerance TEXT, aliases TEXT,
          in_degree INT DEFAULT 0, body_heads TEXT, refs TEXT, mtime REAL);
        CREATE TABLE workflows(
          name TEXT PRIMARY KEY, path TEXT, description TEXT, tags TEXT, mtime REAL);
    """)
    now = datetime.datetime.now().timestamp()

    # --- 技能 ---
    in_degree: dict[str, int] = {}
    for p in SKILLS.rglob("SKILL.md"):
        if ".candidates" in p.parts or ".hub" in p.parts:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
        fm = {}
        if m:
            for line in m.group(1).splitlines():
                if ":" in line and not line.startswith(" "):
                    k, v = line.split(":", 1)
                    fm[k.strip().strip('"')] = v.strip().strip('"')
        name = p.parent.name
        # in-degree：related_skills 引用
        for ref in re.findall(r"related_skills:\s*\[([^\]]*)\]", text, re.S):
            for r in ref.split(","):
                r = r.strip().strip('"').strip("'")
                if r:
                    in_degree[r] = in_degree.get(r, 0) + 1
        # references 浅索引：文件名 + 一级标题
        refs_dir = p.parent / "references"
        refs = []
        if refs_dir.is_dir():
            for rf in sorted(refs_dir.rglob("*")):
                if rf.is_file() and rf.suffix.lower() in (".md", ".txt"):
                    head = ""
                    try:
                        first = rf.read_text(encoding="utf-8", errors="replace").splitlines()
                        for ln in first[:5]:
                            if ln.startswith("#"):
                                head = ln.lstrip("# ").strip()[:60]
                                break
                    except OSError:
                        pass
                    refs.append(f"{rf.name}: {head}" if head else rf.name)
        # 正文标题（一级+二级标题，浅索引）
        heads = [ln.lstrip("# ").strip() for ln in text.splitlines() if ln.startswith(("# ", "## "))]
        conn.execute(
            "INSERT OR REPLACE INTO skills VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, str(p), fm.get("description", ""), fm.get("version", ""),
             fm.get("status", "undeclared"), fm.get("fate", "undeclared"),
             fm.get("tolerance", ""), fm.get("aliases", ""), 0,
             " | ".join(heads)[:800], " | ".join(refs)[:1200], now))
    # in-degree 回填
    for name, deg in in_degree.items():
        conn.execute("UPDATE skills SET in_degree=? WHERE name=?", (deg, name))

    # --- workflows ---
    for p in (AGENT_INDEX / "workflows").glob("*.md"):
        text = p.read_text(encoding="utf-8", errors="replace")
        m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
        fm = {}
        if m:
            for line in m.group(1).splitlines():
                if ":" in line and not line.startswith(" "):
                    k, v = line.split(":", 1)
                    fm[k.strip()] = v.strip()
        conn.execute("INSERT OR REPLACE INTO workflows VALUES (?,?,?,?,?)",
                     (p.stem, str(p), fm.get("description", ""),
                      fm.get("tags", ""), now))
    conn.commit()


def search(conn: sqlite3.Connection, kw: str) -> list[dict]:
    like = f"%{kw}%"
    rows = conn.execute(
        "SELECT name, path, description, version, status, fate, tolerance, "
        "aliases, in_degree, body_heads, refs FROM skills "
        "WHERE name LIKE ? OR description LIKE ? OR aliases LIKE ? OR body_heads LIKE ?",
        (like, like, like, like)).fetchall()
    out = []
    for (name, path, desc, ver, status, fate, tol, aliases, deg, heads, refs) in rows:
        # 忍耐范围过滤（tolerance 声明且不含关键词 → 降序靠后，不剔除）
        tol_ok = (not tol) or kw in tol
        out.append({
            "name": name, "path": path, "desc": desc[:80], "ver": ver,
            "status": status, "fate": fate, "deg": deg,
            "tol_ok": tol_ok, "heads": heads[:120],
            "score": (
                (0 if tol_ok else 100)          # 忍耐范围优先
                + STATUS_RANK.get(status, 4) * 10
                + FATE_RANK.get(fate, 4) * 10
                + (50 if fate == "archived" else 0)   # 已归档成员靠后（hub 优先）
                + (5 - min(len(_version_key(ver)), 5)) * 2  # 版本新者先
                - deg * 3                        # 生态位重要度（被引用多优先）
            ),
        })
    out.sort(key=lambda x: x["score"])
    return out


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python eco_search.py <关键词> [--rebuild]"); return 1
    kw = sys.argv[1]
    rebuild_flag = "--rebuild" in sys.argv

    need = rebuild_flag or not DB_PATH.exists()
    if not need and DB_PATH.exists():
        age = datetime.datetime.now().timestamp() - DB_PATH.stat().st_mtime
        need = age > MAX_AGE

    conn = sqlite3.connect(DB_PATH)
    try:
        if need:
            rebuild(conn)
            print(f"（索引已重建 {datetime.datetime.now().strftime('%H:%M')}）")
        skills = search(conn, kw)
        wfs = conn.execute(
            "SELECT name, path, description, tags FROM workflows "
            "WHERE name LIKE ? OR description LIKE ? OR tags LIKE ?",
            (f"%{kw}%", f"%{kw}%", f"%{kw}%")).fetchall()

        print(f"\n== 技能（{len(skills)} 命中）==")
        for s in skills[:5]:
            loaded = "已加载" if s["status"] == "active" else s["status"]
            print(f"  {s['name']}  v{s['ver'] or '?'} [{s['status']}/{s['fate']}] 被引{s['deg']}  {s['desc']}")
            if s["heads"]:
                print(f"      ├ 章节: {s['heads'][:100]}")
        if len(skills) > 5:
            print(f"  …另有 {len(skills)-5} 个命中（top5 预算）")
        print(f"\n== 项目工作流（{len(wfs)} 命中）==")
        for name, path, desc, tags in wfs[:5]:
            print(f"  {name}: {desc[:70]}")
        if not skills and not wfs:
            print("  无命中")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
