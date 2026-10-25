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
import json
import os  # 2026-10-05：MEMORY_ECOLOGY_DISTILL_BATCH 首轮清淤批量覆盖
import re
import shutil
import sqlite3
import sys
from pathlib import Path

from lib.config import hermes_root
from lib.cli_base import bootstrap as _bootstrap   # v2.5.0：统一编码兜底
from lib.config import cfg
from lib.fs import atomic_write, norm, slug_of
from lib import llm as _llm
from lib import similarity
from lib.gatekit import acquire_lock, release_lock, connect_db
from lib.memstore import (parse_frontmatter, dump_frontmatter, load_detail,
                          parse_l1, serialize_l1)  # §C：解析/IO 单源

HERMES = hermes_root()
DETAIL_DIR = HERMES / "memories" / "detail"
CAND_DIR = HERMES / "memories" / "user_candidates"
QUARANTINE_DIR = HERMES / "memories" / "quarantine"
USER_FILE = HERMES / "memories" / "USER.md"
DB = HERMES / "eco.db"
LOCK_FILE = HERMES / "scripts" / ".distill_stage.lock"

OBSERVE_DAYS = 30       # 观察期
MAX_TRAIT = 80          # 特质句长度上限
MAX_BATCH = 3           # 每轮最多蒸馏条数（阶段2）
PROMOTE_MAX = 3         # 阶段1每轮最多晋升条数（P1：旧版无上限，可把 USER 推过配额）
USER_QUOTA = 1500       # USER.md 字符配额
USER_WATERMARK = 0.90   # USER 占用 >90% 暂停蒸馏
MIN_OCC = 2             # 候选 occurrences 门槛
MIN_SESS = 2            # 候选 session_count 门槛
STABLE_DAYS = 14        # 驻留稳定口径（2026-10-05）：first_seen 早于该天数且仍 active
                        # 视为稳定。背景：occurrences 计数依赖同类事实被再次写入且命中
                        # write_gate 相似合并，词面相似层漏配时条目永久停留 1/1
                        # （实测 84 条 detail 全部 occurrences=1，画像层自 08-30 起零产出）。
                        # 可经 configs/eco.toml [distill] stable_days 覆盖。
REPLACE_RATIO = 0.80    # 同义替换阈值（difflib 口径默认；实际经 gate_threshold 按后端迁移）
                        # 相似但非同义 → 不替换，防误伤行为规则

PROMPT = """你是用户画像蒸馏器。把一条「已确认的长期记忆」浓缩成一句话用户画像特质。
要求：1) 保留事实核心，去掉过程细节；2) 用陈述句，第三人称「用户」；3) ≤80 字；
4) 不推断、不添加原记忆没有的信息。
输出 JSON：{"trait": "..."}。只输出 JSON。
原记忆：
{body}
"""




def llm_trait(body: str) -> str:
    # deepseek-v4-flash 为推理模型（2026-10-05 修复）：reasoning_content 先行产出，
    # 旧预算 200 会被思考全吃光（finish_reason=length）→ content 恒空 → 全面回退
    # 原文截断。且思考长度随条目复杂度波动大（实测 4086 字符），故预算阶梯重试：
    # 2000 不够升 6000，仍空才回退原文（LLM 失败不阻断管道的既定语义不变）。
    prompt = PROMPT.replace("{body}", body[:300])
    content = ""
    for budget in (2000, 6000):
        content = _llm.complete(prompt, max_tokens=budget, temperature=0.1)
        if content and content.strip():
            break
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        return ""
    try:
        return json.loads(m.group(0)).get("trait", "").strip()
    except json.JSONDecodeError:
        return ""


def load_candidates(cand_dir: Path) -> list[dict]:
    """读观察期候选。P1：包含 evolve approve 产出的 *.md.approved（人工已确认）——
    旧版 glob("*.md") 看不见它们，人工决策被无痕丢弃；approved 候选排最前。"""
    items = []
    if not cand_dir.exists():
        return items
    for f in sorted(cand_dir.glob("*.md")) + sorted(cand_dir.glob("*.md.approved")):
        if f.name.endswith((".promoted.md", ".rejected.md")):
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, body = parse_frontmatter(text)
        items.append({"path": f, "name": f.stem, "fm": fm, "body": body,
                      "approved": f.name.endswith(".approved")})
    items.sort(key=lambda c: (not c["approved"], c["name"]))
    return items


def user_entries(user_file: Path) -> list[str]:
    """USER.md 按 § 行锚定拆条目（P0：不用字符级 split，防正文内联 § 被误拆）。"""
    if not user_file.exists():
        return []
    entries, _ = parse_l1(user_file.read_text(encoding="utf-8"))
    return entries


def user_usage(user_file: Path) -> int:
    return len(user_file.read_text(encoding="utf-8")) if user_file.exists() else 0


def find_conflict(trait: str, entries: list[str]) -> str | None:
    """同义替换判定：候选与某条 USER 条目**同义**才返回该条目（整块替换）。
    同义 = 包含关系（新候选是旧条目的提炼/细化，norm 子串）或高度相似（≥门③阈值）。
    相似但非同义 → 返回 None（不替换，避免误伤互补的行为规则——P1-4）。
    阈值经 similarity.gate_threshold('merge') 按生效后端解析（difflib 口径 0.80）。"""
    nt = norm(trait)
    if not nt:
        return None
    threshold = similarity.gate_threshold("merge", REPLACE_RATIO)
    for e in entries:
        ne = norm(e)
        if not ne:
            continue
        if nt in ne or ne in nt:
            return e  # 包含关系 = 同义
        ratio = similarity.ratio(nt, ne)  # 批 5：统一相似度层
        if ratio >= threshold:
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


def _read_raw(user_file: Path) -> str:
    """newline='' 读取——不做通用换行转换，配合 parse_l1 的换行风格探测保真往返。"""
    with user_file.open("r", encoding="utf-8", newline="") as f:
        return f.read()


def _replace_user_entry(user_file: Path, old: str, new: str) -> bool:
    """整块替换（P0-4：经 memstore.parse_l1/serialize_l1 行锚定重写）。

    旧实现 `"§".join(text.split("§"))` 会丢掉被替换块两端的换行，产物形如
    `trait A\\n§NEW§\\ntrait C`——§ 不再独占一行，门② parse_l1 把整个文件解析成
    一条 → 超配额整体挤出 → USER.md 被清空。行锚定重写保证 § 结构往返守恒。
    old 按整块精确匹配（strip 后比对）；找不到 → 返回 False 不替换。
    """
    raw = _read_raw(user_file)
    entries, nl = parse_l1(raw)
    target = old.strip()
    for i, e in enumerate(entries):
        if e == target:
            entries[i] = new.strip()
            atomic_write(user_file, serialize_l1(entries, nl))
            return True
    return False  # 未找到整块（异常状态：不替换，交报告）


_logged_actions: list[str] = []  # v0.3：本轮已发生的动作（供零动作心跳判断）


def log_distill(conn: sqlite3.Connection, action: str, target: str, note: str, dry: bool = False) -> None:
    if dry:
        return
    _logged_actions.append(action)  # v0.3：记录动作，零动作路径据此写 idle 心跳
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO distill_log(ts, action, target, note) VALUES(?,?,?,?)",
        (ts, action, target, note),
    )
    conn.commit()  # P1：逐条落账——中途异常时账本与已落地动作一致（旧版最后才 commit）


def _stamp_distilled_at(d: dict, day: str) -> None:
    """源条目登记 distilled_at（蒸馏状态位，v2.3.0 证据链）。

    只标记不判定纪律不变：此位仅表示「该源已产出一条观察期候选」，
    不代表画像已收录（promoted 才是）。失败静默——登记不阻塞蒸馏管线。"""
    try:
        raw = d["path"].read_text(encoding="utf-8")
        fm, body = parse_frontmatter(raw)
        fm["distilled_at"] = day
        atomic_write(d["path"], dump_frontmatter(fm, body))
        d["fm"]["distilled_at"] = day
    except (OSError, ValueError):
        # ValueError：非法 UTF-8（UnicodeDecodeError）等——「失败静默不阻塞管线」是承诺，
        # 兜底必须覆盖非 OSError 的读取异常（2026-10-07 OCR 评审）
        pass


def main() -> int:
    _bootstrap()   # v2.5.0：编码兜底先于一切
    from lib.cli_base import banner as _banner   # v2.5.6：部署可观测（哈希+版本+数据根）
    _banner("distill_stage", file=__file__)
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--detail", type=Path, default=DETAIL_DIR)
    ap.add_argument("--cand", type=Path, default=CAND_DIR)
    ap.add_argument("--user", type=Path, default=USER_FILE)
    ap.add_argument("--quarantine", type=Path, default=QUARANTINE_DIR)
    ap.add_argument("--db", type=Path, default=DB)
    args = ap.parse_args()

    if not args.dry_run:
        if not acquire_lock(LOCK_FILE):
            print("⚠️ 已有 distill_stage 实例在运行，本轮跳过")
            return 0
    try:
        return _run(args)
    except Exception as e:
        # P1：顶层兜底——阶段1已落地的动作有逐条 commit 的账本可查，这里只负责可见性
        print(f"⚠️ 蒸馏门异常终止: {e}")
        return 1
    finally:
        if not args.dry_run:
            release_lock(LOCK_FILE)


def _run(args) -> int:
    if not args.dry_run:
        args.cand.mkdir(parents=True, exist_ok=True)
    # dry-run 且库不存在：connect_db 用 :memory: 顶替——不建库不留 0 字节文件
    # （docstring「不建目录不建表」兑现）；库已存在时照常连接读真数据，行为与修复前完全一致
    conn = connect_db(args.db, dry_run=args.dry_run)
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
        # v0.3：整轮跳过也是"门在跑"的证据，写 idle 心跳
        log_distill(conn, "idle", "", "heartbeat 零动作（USER 配额暂停）", args.dry_run)
        conn.commit()
        conn.close()
        return 0

    # ---- 阶段 1：观察期候选复查（created 30 天前 → 升 USER 或 rejected）----
    promoted = 0  # P1：本轮晋升计数（上限 PROMOTE_MAX，逐条配额复查）
    for c in load_candidates(args.cand):
        created = c["fm"].get("created", "")
        try:
            age = (today - datetime.date.fromisoformat(created)).days
        except ValueError:
            # P1：日期损坏不再静默永久卡在观察期——跳过并告警（人工修 fm 后下轮正常）
            report.append(f"⚠️ SKIP   {c['name']}（created 无法解析：{created!r}，请修正候选文件）")
            continue
        if not c["approved"] and age < OBSERVE_DAYS:
            continue  # 观察期未满（evolve approve 的人工确认候选豁免观察期——人工已裁决）
        if promoted >= PROMOTE_MAX:
            report.append(f"⏸ 本轮晋升已达上限 {PROMOTE_MAX}，{c['name']} 留待下轮")
            continue  # 不改名——候选保持待处理
        usage_now = user_usage(args.user)
        if usage_now + len(c["body"]) + 3 > USER_QUOTA:
            report.append(f"⏸ USER 配额将满（{usage_now}/{USER_QUOTA}），{c['name']} 留待下轮")
            continue
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
                promoted += 1
                if bak:
                    report.append(f"  备份: {bak.name}")
            promoted += 1  # dry-run 也计数：PROMOTE_MAX 上限在预览中同样生效
            report.append(f"REPLACE {conflict[:30]} → {c['body'][:40]}（同义整块替换，旧条目入 quarantine）")
            log_distill(conn, "replace", conflict, f"new={c['name']}", args.dry_run)
        else:
            if not args.dry_run:
                bak = _backup_user(args.user)
                _append_user_entry(args.user, c["body"])
                c["path"].rename(c["path"].with_suffix(".promoted.md"))
                promoted += 1
                if bak:
                    report.append(f"  备份: {bak.name}")
            promoted += 1  # dry-run 也计数：PROMOTE_MAX 上限在预览中同样生效
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
    try:
        stable_days = int(cfg("distill", "stable_days", STABLE_DAYS))
    except (TypeError, ValueError):
        stable_days = STABLE_DAYS
    batch_cap = MAX_BATCH
    try:
        batch_cap = int(os.environ.get("MEMORY_ECOLOGY_DISTILL_BATCH", MAX_BATCH))
    except ValueError:
        pass  # 非法 env 值 → 回退代码默认（首轮清淤可临时调大批量，平时 3）
    for d in detail:
        fm = d["fm"]
        if fm.get("type") != "semantic" or fm.get("status") != "active":
            continue
        double_hit_ok = False
        try:
            double_hit_ok = (int(fm.get("occurrences", "0")) >= MIN_OCC
                             and int(fm.get("session_count", "0")) >= MIN_SESS)
        except ValueError:
            double_hit_ok = False
        if not double_hit_ok:
            # 驻留稳定口径（2026-10-05）：双命中（occurrences≥2）依赖同类事实被再次
            # 写入并命中 write_gate 相似合并，词面相似层漏配时条目永久停留 1/1
            # （实测 84 条全部 1/1 → 画像层自 08-30 起零产出）。first_seen 驻留超
            # STABLE_DAYS 且仍 active 无矛盾，是规则可判的稳定证据（仍然零 LLM 判稳）。
            try:
                fs = datetime.date.fromisoformat(str(fm.get("first_seen", ""))[:10])
            except ValueError:
                continue
            if (today - fs).days < stable_days:
                continue
        nt = norm(d["body"])
        if any(norm(e) == nt for e in user_entries(args.user)):
            continue
        if d["name"] in cand_sources:
            continue
        eligible.append(d)
    eligible.sort(key=lambda d: d["name"])
    for d in eligible[:batch_cap]:
        if args.dry_run:
            # P1：dry-run 不发起真实 LLM 调用（旧版有网络副作用与费用，且报告与实际执行可能不一致）
            trait = d["body"][:MAX_TRAIT]
        else:
            try:
                trait = llm_trait(d["body"]) or d["body"][:MAX_TRAIT]
            except Exception as e:
                # P1-1：LLM 失败优雅降级（不阻断管道）
                trait = d["body"][:MAX_TRAIT]
                report.append(f"  ⚠️ LLM 措辞失败，回退原文: {e}")
        trait = trait[:MAX_TRAIT]
        slug = slug_of(trait)
        if not args.dry_run:
            # P1：同 slug 候选已存在（不同 source 产出同一句通用措辞）→ 跳过不覆盖，
            # 防止 created 被反复刷新、观察期永远无法完成的乒乓循环
            if (args.cand / f"{slug}.md").exists():
                report.append(f"SKIP    候选 {slug} 已存在（源 {d['name']}），不覆盖不重置观察期")
                continue
            cfm = {
                "source": d["name"], "created": today.isoformat(),
                "status": "observing", "sessions": d["fm"].get("session_count", "?"),
            }
            atomic_write(args.cand / f"{slug}.md", dump_frontmatter(cfm, trait))
            # v2.3.0 证据链：候选落盘即登记源条目 distilled_at（Hindsight consolidated_at 同思想）
            # ——源失效（write_gate.supersede_entry）或复活（memory_query.revive）时清空此位，
            # 下轮门③把该源重新纳入候选，蒸馏随源状态可逆
            _stamp_distilled_at(d, today.isoformat())
        report.append(f"CAND    {trait[:40]}（源 {d['name']}，观察期 {OBSERVE_DAYS} 天）")
        log_distill(conn, "candidate", slug, f"src={d['name']}", args.dry_run)

    # v0.3：本轮零动作时写 idle 心跳（"门在跑、本轮零动作"有据可查）
    if not _logged_actions:
        log_distill(conn, "idle", "", "heartbeat 零动作", args.dry_run)

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
