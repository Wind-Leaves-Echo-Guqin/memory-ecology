#!/usr/bin/env python3
"""生态检索 eco_search 静态版（memory-skill-ecosystem 项目 3.4 节）。

两级路由：技能能力记录 + workflows 项目记录。静态级联排序：
忍耐范围（tolerance，可选）→ 活性（status）→ 存续（fate）→ 版本 → 生态位重要度（in-degree）。
结果 top5 一行摘要 + 依赖链预检 + 已加载标注。

用法:
  python eco_search.py <关键词>          # 检索（自动重建索引若过期）
  python eco_search.py --rebuild         # 强制重建索引（关键词可省，GUI/first_aid 即此形态）
"""
import datetime
import re
import sqlite3
import sys
from pathlib import Path

from lib.config import hermes_root

try:  # v0.3：子进程/管道下 stdout 走 GBK 会导致中文乱码，显式固定 UTF-8
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

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


def _version_score(v: str) -> int:
    """版本数值分：按分量大小比较（major 主导），新版本分更高。

    OCR 2026-10-03：原实现按"分量个数"计分——'0.0.1'（3 段）反而压过 '9.0'（2 段）、
    '1.2.3' 与 '4.5.6' 同分，与"版本新者先"相悖。截前 3 段、每段 <1000。
    """
    nums = [int(x) for x in re.findall(r"\d+", v or "")[:3]]
    while len(nums) < 3:
        nums.append(0)
    return min(nums[0], 999) * 1000000 + min(nums[1], 999) * 1000 + min(nums[2], 999)


def rebuild(conn: sqlite3.Connection) -> None:
    # OCR 2026-10-03：drop+create+insert 包进显式事务——executescript 会先 COMMIT 且
    # DDL 走 autocommit，中途被杀会留下"表已 DROP/半填充"的库，读端静默查空
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DROP TABLE IF EXISTS skills")
        conn.execute("DROP TABLE IF EXISTS workflows")
        conn.execute("""
            CREATE TABLE skills(
              name TEXT PRIMARY KEY, path TEXT, description TEXT, version TEXT,
              status TEXT, fate TEXT, tolerance TEXT, aliases TEXT,
              in_degree INT DEFAULT 0, body_heads TEXT, refs TEXT, mtime REAL)""")
        conn.execute("""
            CREATE TABLE workflows(
              name TEXT PRIMARY KEY, path TEXT, description TEXT, tags TEXT, mtime REAL)""")
        _rebuild_populate(conn)
        conn.commit()
    except BaseException:
        conn.rollback()  # 回滚到上一份好索引
        raise


def _rebuild_populate(conn: sqlite3.Connection) -> None:
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
        # OCR 2026-10-03：lower 对齐 SQL LIKE 的 ASCII 大小写不敏感口径
        tol_ok = (not tol) or kw.lower() in tol.lower()
        out.append({
            "name": name, "path": path, "desc": desc[:80], "ver": ver,
            "status": status, "fate": fate, "deg": deg,
            "tol_ok": tol_ok, "heads": heads[:120],
            "score": (
                (0 if tol_ok else 100)          # 忍耐范围优先
                + STATUS_RANK.get(status, 4) * 10
                + FATE_RANK.get(fate, 4) * 10
                + (50 if fate == "archived" else 0)   # 已归档成员靠后（hub 优先）
                - deg * 3                        # 生态位重要度（被引用多优先）
            ),
        })
    # OCR 2026-10-03：版本改为数值分量比较、作同分裁决（新者先）——原"分量个数"计分
    # 会让 '0.0.1' 压过 '9.0'、'1.2.3' 与 '4.5.6' 同分
    out.sort(key=lambda x: (x["score"], -_version_score(x["ver"])))
    return out


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="生态检索 eco_search（只读）")
    # OCR 2026-10-03：keyword 改可选——GUI/first_aid 真实调用 `--rebuild` 无关键词，
    # 原必填位置参数会 exit 2 直接打断重建
    ap.add_argument("keyword", nargs="?", default="", help="检索关键词（--rebuild 单用时可省）")
    ap.add_argument("--rebuild", action="store_true", help="强制重建索引")
    ap.add_argument("--format", choices=["text", "json"], default="text", help="输出格式（PORT_SPEC §4-C）")
    args = ap.parse_args()
    kw = args.keyword
    rebuild_flag = args.rebuild

    need = rebuild_flag or not DB_PATH.exists()
    if not need and DB_PATH.exists():
        age = datetime.datetime.now().timestamp() - DB_PATH.stat().st_mtime
        need = age > MAX_AGE

    conn = sqlite3.connect(DB_PATH)
    try:
        if need:
            rebuild(conn)
            # json 契约纯净性：stdout 只输出 JSON，人读提示走 stderr（终端显示不变）
            print(f"（索引已重建 {datetime.datetime.now().strftime('%H:%M')}）", file=sys.stderr)
            if rebuild_flag and not kw:
                # --rebuild 无关键词：重建即目的，不再检索
                if args.format == "json":
                    import json
                    print(json.dumps({"ok": True, "query": "", "rebuilt": True,
                                      "skills": [], "workflows": []},
                                     ensure_ascii=False, indent=2))
                else:
                    n_s = conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0]
                    n_w = conn.execute("SELECT COUNT(*) FROM workflows").fetchone()[0]
                    print(f"✅ 索引重建完成：{n_s} 技能 / {n_w} 工作流")
                return 0
        skills = search(conn, kw)
        wfs = conn.execute(
            "SELECT name, path, description, tags FROM workflows "
            "WHERE name LIKE ? OR description LIKE ? OR tags LIKE ?",
            (f"%{kw}%", f"%{kw}%", f"%{kw}%")).fetchall()

        if args.format == "json":
            import json
            print(json.dumps({
                "ok": True, "query": kw,
                "skills": [{"name": s["name"], "version": s["ver"], "status": s["status"],
                            "fate": s["fate"], "indeg": s["deg"], "desc": s["desc"]} for s in skills[:10]],
                "workflows": [{"name": n, "desc": d} for n, p, d, t in wfs[:5]],
            }, ensure_ascii=False, indent=2))
            return 0
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
