#!/usr/bin/env python3
"""生态蒸馏门 distill_stage（版本迭代策略 v2 门③，USER 画像侧）。

把 L2 详情层（memories/detail/）中稳定的 semantic 记忆蒸馏为 USER.md 画像特质：
1) 候选资格（规则判稳，双审共识：LLM 不判「稳定 vs 瞬时」）：
   type=semantic + status=active + occurrences≥2 + session_count≥2
2) LLM 只做画像措辞生成（失败回退原文，不阻断管道）
3) 观察期：候选写入 memories/user_candidates/，30 天后复查无矛盾才升 USER.md
   （观察期内源条目被 superseded/归档 → 候选 rejected）
4) 冲突替换：新特质与 USER.md 现有条目**同义**（相似度 ≥0.8）才整块替换；
   旧条目内容入 quarantine 记录（可回滚）；相似但非同义 → 不替换（追加候选）
5) USER 配额保护：**写入前**检查 USER.md 占用，>90% 则整轮跳过（等 quota 门挤出）
6) USER 写入原子（临时文件+os.replace）+ 修改前备份 .bak-蒸馏前-<ts>

设计：独立脚本（保护区外）；只读 detail/pending；--dry-run 只报告（不建目录不建表）。

用法: python distill_stage.py [--dry-run] [--detail DIR] [--cand DIR]
      [--user PATH] [--quarantine DIR] [--db PATH]   （cron 每日 13:10 no_agent）
"""
import argparse
import datetime
import difflib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path

from lib.config import hermes_root
from lib.fs import atomic_write, norm, slug_of
from lib import llm as _llm

HERMES = hermes_root()
DETAIL_DIR = HERMES / "memories" / "detail"
CAND_DIR = HERMES / "memories" / "user_candidates"
QUARANTINE_DIR = HERMES / "memories" / "quarantine"
USER_FILE = HERMES / "memories" / "USER.md"
DB = HERMES / "eco.db"
LOCK_FILE = HERMES / "scripts" / ".distill_stage.lock"

OBSERVE_DAYS = 30       # 观察期
MAX_TRAIT = 80          # 特质句长度上限
MAX_BATCH = 3           # 每轮最多蒸馏条数
USER_QUOTA = 1500       # USER.md 字符配额
USER_WATERMARK = 0.90   # USER 占用 >90% 暂停蒸馏
MIN_OCC = 2             # 候选 occurrences 门槛
MIN_SESS = 2            # 候选 session_count 门槛
REPLACE_RATIO = 0.80    # 同义替换阈值（相似但非同义 → 不替换，防误伤行为规则）

PROMPT = """你是用户画像蒸馏器。把一条「已确认的长期记忆」浓缩成一句话用户画像特质。
要求：1) 保留事实核心，去掉过程细节；2) 用陈述句，第三人称「用户」；3) ≤80 字；
4) 不推断、不添加原记忆没有的信息。
输出 JSON：{"trait": "..."}。只输出 JSON。
原记忆：
{body}
"""




def llm_trait(body: str) -> str:
    content = _llm.complete(PROMPT.replace("{body}", body[:300]),
                            max_tokens=200, temperature=0.1)
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        return ""
    try:
        return json.loads(m.group(0)).get("trait", "").strip()
    except json.JSONDecodeError:
        return ""


def parse_frontmatter(text: str) -> tuple[dict, str]:
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
    lines = ["---"]
    for k, v in fm.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    return "\n".join(lines) + "\n"








def load_detail(detail_dir: Path) -> list[dict]:
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


def load_candidates(cand_dir: Path) -> list[dict]:
    items = []
    if not cand_dir.exists():
        return items
    for f in sorted(cand_dir.glob("*.md")):
        if f.name.endswith((".promoted.md", ".rejected.md")):
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, body = parse_frontmatter(text)
        items.append({"path": f, "name": f.stem, "fm": fm, "body": body})
    return items


def user_entries(user_file: Path) -> list[str]:
    """USER.md 按 § 分隔拆条目（整块，strip）。"""
    if not user_file.exists():
        return []
    text = user_file.read_text(encoding="utf-8")
    return [e.strip() for e in text.split("§") if e.strip()]


def user_usage(user_file: Path) -> int:
    return len(user_file.read_text(encoding="utf-8")) if user_file.exists() else 0


def find_conflict(trait: str, entries: list[str]) -> str | None:
    """同义替换判定：候选与某条 USER 条目**同义**才返回该条目（整块替换）。
    同义 = 包含关系（新候选是旧条目的提炼/细化，norm 子串）或高度相似（≥0.8）。
    相似但非同义（0.45~0.8）→ 返回 None（不替换，避免误伤互补的行为规则——P1-4）。"""
    nt = norm(trait)
    if not nt:
        return None
    for e in entries:
        ne = norm(e)
        if not ne:
            continue
        if nt in ne or ne in nt:
            return e  # 包含关系 = 同义
        ratio = difflib.SequenceMatcher(None, nt, ne).ratio()
        if ratio >= REPLACE_RATIO:
            return e
    return None


def _backup_user(user_file: Path) -> Path | None:
    """修改 USER.md 前备份（P0-2）：<name>.bak-蒸馏前-<YYYYMMDD-HHMMSS-mmm>（毫秒防同秒覆盖）。"""
    if not user_file.exists():
        return None
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    bak = user_file.with_name(user_file.name + f".bak-蒸馏前-{ts}")
    shutil.copy2(user_file, bak)
    return bak


def _append_user_entry(user_file: Path, text: str) -> None:
    """追加 USER 条目（保持 § 分隔；原子写；追加前 rstrip 防双空行——P2-8）。"""
    s = text.strip()
    if user_file.exists() and user_file.read_text(encoding="utf-8").strip():
        raw = user_file.read_text(encoding="utf-8").rstrip()
        atomic_write(user_file, raw + "\n§\n" + s + "\n")
    else:
        atomic_write(user_file, s + "\n")


def _replace_user_entry(user_file: Path, old: str, new: str) -> bool:
    """整块替换（按 § 分块精确匹配，P1-5：不做子串替换；找不到 → 返回 False 不替换）。"""
    text = user_file.read_text(encoding="utf-8")
    blocks = text.split("§")
    replaced = False
    for i, b in enumerate(blocks):
        if b.strip() == old.strip():
            blocks[i] = new.strip()
            replaced = True
            break
    if not replaced:
        return False  # 未找到整块（异常状态：不替换，交报告）
    atomic_write(user_file, "§".join(blocks))
    return True


def log_distill(conn: sqlite3.Connection, action: str, target: str, note: str, dry: bool = False) -> None:
    if dry:
        return
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO distill_log(ts, action, target, note) VALUES(?,?,?,?)",
        (ts, action, target, note),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--detail", type=Path, default=DETAIL_DIR)
    ap.add_argument("--cand", type=Path, default=CAND_DIR)
    ap.add_argument("--user", type=Path, default=USER_FILE)
    ap.add_argument("--quarantine", type=Path, default=QUARANTINE_DIR)
    ap.add_argument("--db", type=Path, default=DB)
    args = ap.parse_args()

    if not args.dry_run:
        try:
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
        except FileExistsError:
            print("⚠️ 已有 distill_stage 实例在运行，本轮跳过")
            return 0
    try:
        return _run(args)
    finally:
        if not args.dry_run:
            LOCK_FILE.unlink(missing_ok=True)


def _run(args) -> int:
    if not args.dry_run:
        args.cand.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(args.db, timeout=10)
    if not args.dry_run:
        conn.execute("""CREATE TABLE IF NOT EXISTS distill_log(
            ts TEXT, action TEXT, target TEXT, note TEXT)""")

    today = datetime.date.today()
    report = []
    failures = 0

    # ---- P1-2：配额水印检查前置（stage 1 之前，整轮跳过）----
    usage = user_usage(args.user)
    if usage > USER_QUOTA * USER_WATERMARK:
        print(f"⏸ USER.md 占用 {usage}/{USER_QUOTA} >{int(USER_WATERMARK*100)}%，本轮暂停蒸馏（等 quota 门挤出）")
        conn.commit()
        conn.close()
        return 0

    # ---- 阶段 1：观察期候选复查（created 30 天前 → 升 USER 或 rejected）----
    for c in load_candidates(args.cand):
        created = c["fm"].get("created", "")
        try:
            age = (today - datetime.date.fromisoformat(created)).days
        except ValueError:
            age = -1
        if age < OBSERVE_DAYS:
            continue  # 观察期未满
        src = c["fm"].get("source", "")
        src_active = True
        if src:
            sp = args.detail / f"{src}.md"
            if sp.exists():
                sfm, _ = parse_frontmatter(sp.read_text(encoding="utf-8"))
                src_active = sfm.get("status", "active") == "active"
            else:
                # P1-3：源 detail 缺失（被 superseded 移 quarantine / 被 review 归档）→ 视为失效
                src_active = False
        if not src_active:
            if not args.dry_run:
                c["path"].rename(c["path"].with_suffix(".rejected.md"))
            report.append(f"REJECT  {c['name']}（源条目已失效）")
            log_distill(conn, "reject", c["name"], "source superseded/archived", args.dry_run)
            continue
        # 同义冲突检查（对 USER 现有条目）
        entries = user_entries(args.user)
        conflict = find_conflict(c["body"], entries)
        if conflict:
            if not args.dry_run:
                bak = _backup_user(args.user)
                date_dir = args.quarantine / today.isoformat()
                date_dir.mkdir(parents=True, exist_ok=True)
                qf = date_dir / f"user-{slug_of(conflict)}.md"
                qf.write_text(
                    dump_frontmatter({"type": "user-trait", "status": "superseded",
                                      "superseded_by": c["name"], "transaction_time": today.isoformat()},
                                     conflict),
                    encoding="utf-8")
                ok = _replace_user_entry(args.user, conflict, c["body"])
                if not ok:
                    failures += 1
                    report.append(f"⚠️ REPLACE 未找到整块（未修改）: {conflict[:30]}")
                    # 备份还原（未修改则备份无用，删除避免堆积）
                    if bak and bak.exists():
                        bak.unlink()
                    continue
                c["path"].rename(c["path"].with_suffix(".promoted.md"))
                if bak:
                    report.append(f"  备份: {bak.name}")
            report.append(f"REPLACE {conflict[:30]} → {c['body'][:40]}（同义整块替换，旧条目入 quarantine）")
            log_distill(conn, "replace", conflict, f"new={c['name']}", args.dry_run)
        else:
            if not args.dry_run:
                bak = _backup_user(args.user)
                _append_user_entry(args.user, c["body"])
                c["path"].rename(c["path"].with_suffix(".promoted.md"))
                if bak:
                    report.append(f"  备份: {bak.name}")
            report.append(f"PROMOTE {c['name']} → USER.md")
            log_distill(conn, "promote", c["name"], "", args.dry_run)

    # ---- 阶段 2：新候选生成（规则判稳 + LLM 措辞）----
    detail = load_detail(args.detail)
    # 幂等：所有候选（含已 promoted/rejected）按 source 去重，一个 detail 源只允许一个候选
    cand_sources: set[str] = set()
    if args.cand.exists():
        for f in args.cand.glob("*.md"):
            try:
                fm, _ = parse_frontmatter(f.read_text(encoding="utf-8"))
            except OSError:
                continue
            if fm.get("source"):
                cand_sources.add(fm["source"])
    eligible = []
    for d in detail:
        fm = d["fm"]
        if fm.get("type") != "semantic" or fm.get("status") != "active":
            continue
        try:
            if int(fm.get("occurrences", "0")) < MIN_OCC or int(fm.get("session_count", "0")) < MIN_SESS:
                continue
        except ValueError:
            continue
        nt = norm(d["body"])
        if any(norm(e) == nt for e in user_entries(args.user)):
            continue
        if d["name"] in cand_sources:
            continue
        eligible.append(d)
    eligible.sort(key=lambda d: d["name"])
    for d in eligible[:MAX_BATCH]:
        try:
            trait = llm_trait(d["body"]) or d["body"][:MAX_TRAIT]
        except Exception as e:
            # P1-1：LLM 失败优雅降级（不阻断管道）
            trait = d["body"][:MAX_TRAIT]
            report.append(f"  ⚠️ LLM 措辞失败，回退原文: {e}")
        trait = trait[:MAX_TRAIT]
        slug = slug_of(trait)
        if not args.dry_run:
            cfm = {
                "source": d["name"], "created": today.isoformat(),
                "status": "observing", "sessions": d["fm"].get("session_count", "?"),
            }
            atomic_write(args.cand / f"{slug}.md", dump_frontmatter(cfm, trait))
        report.append(f"CAND    {trait[:40]}（源 {d['name']}，观察期 {OBSERVE_DAYS} 天）")
        log_distill(conn, "candidate", slug, f"src={d['name']}", args.dry_run)

    conn.commit()
    conn.close()
    if not report:
        report.append("ℹ️ 无蒸馏动作（观察期候选 0 条；detail 无达标候选或 USER 配额未空余）")
    if args.dry_run:
        print("== DRY-RUN（未修改任何文件）==")
    for r in report:
        print(r)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
