#!/usr/bin/env python3
"""记忆生态「巩固/配额门」eco_quota（memory-skill-ecosystem 项目；纯规则、零 LLM 调用）。

作用：L1 常驻记忆的自动提升（L2→L1）与挤出（L1→L2），按配额管理文件大小，
     保证 L1 精简、信息不丢失（挤出条目先落盘 L2 detail 或确认 L2 已有同源）。

L1 常驻文件（默认路径，可用 --l1 覆盖，可多次指定）：
  memories/MEMORY.md   配额 3000 字符（85% 触发线 = 2550）
  memories/USER.md     配额 1500 字符（85% 触发线 = 1275）
  格式：条目之间以单独一行 § 分隔（实测为 CRLF；LF/CRLF 自动识别并在写回时保持原样）。
L2 详情层（默认）：memories/detail/，每条一个 .md（YAML frontmatter + 正文 ≤200 字符）。
日志库（默认）：hermes/eco.db，建表 quota_log(ts, action, slug, l1_preview, reason)。

功能 1 提升（L2→L1）：
  detail 条目 frontmatter 满足 type∈{semantic, procedural} 且 status=active 且
  occurrences≥3 且 session_count≥2 → 提升候选。与 L1 各条目做同源判定
  （规范化 = 去空白/标点 re.sub(r"[\\s\\W_]+","",s)；detail 正文前 30 字符的规范化形式
  被某 L1 条目包含即视为同源）→ 无同源才把正文（原样，超 220 字符截到 220）追加到
  第一个 L1 文件末尾（新条目前补 § 行，保持 § 分隔格式）。

功能 2 挤出（L1→L2）：
  字符数 = 条目规范化序列化后的 UTF-8 码点数（分隔符按 \n§\n 计，与生态体检
  read_text 字符口径一致，CRLF 的 \r 不计）。超过配额 85% 时按序挤出直到 ≤85%：
    ① 与 detail 已有条目同源的条目（L2 已存，挤出不丢信息）
    ② 正文含历史日期关键词（正则：2026-0[1-5] 或 2025 及更早年份 19xx/20xx）的条目
    ③ 文件靠后的条目（近似 LRU，从末尾往前）
  挤出条目若在 detail 无同源 → 先写入 detail（保守默认 frontmatter：type=semantic,
  status=active, occurrences=1, session_count=1, first_seen/transaction_time/
  last_verified=现在, origin_session_id=extrude-from-L1）——绝不静默丢弃。

  防震荡（验收实测发现「提升→挤出同源→再提升」跨轮 2-循环，2026-08-30 修复）：
  1) 挤出候选默认排除两类「高价值」条目——同轮刚提升的；其同源 detail 本身是提升
     候选（occ≥3 且 sess≥2）的。挤出顺序 = ①非高价值同源 → ②非高价值历史日期 →
     ③非高价值靠后；仍超限且本轮有提升 → 撤掉提升（还原原条目，打印「跳过提升」，
     信息留 L2 不丢）；仅纯合规（无提升、文件本就超限）才按字面 ①②③ 全序挤（含高价值）。
  2) 挤出落盘 detail 的条目（origin_session_id=extrude-from-L1）有 COOLDOWN_DAYS 天
     冷却期不提升，防止刚挤出内容被立刻提升回来（并发验收补充）。
  由此保证：稳态零震荡（重跑无动作）；L1 始终 ≤85%；信息永不静默丢弃。

安全（必须遵守）：
  - 修改 L1 前先复制备份 <名>.bak-配额前-<YYYYMMDD-HHMMSS>（同目录，备份绝不删除）；
  - 绝不删除任何文件；不碰 memories/pending/、scripts/ 下其他脚本；
  - 文件读写一律 UTF-8 + pathlib；Windows 路径用 pathlib 处理；
  - 先全部计算好，再一次性原子写回（临时文件 + os.replace）；
  - 任何异常 try/except 捕获，打印 ⚠️ 并返回非 0 退出码，不留半修改文件。

用法：
  python eco_quota.py               # 实际执行并写 eco.db 日志（cron no_agent 模式）
  python eco_quota.py --dry-run     # 只打印将执行的动作，不修改任何文件、不写日志
  python eco_quota.py --l1 <path> --detail <dir> --db <path>   # 路径覆盖（自测用）
  python eco_quota.py --quota 500   # 统一配额覆盖（自测用小配额触发挤出）
"""
import argparse
import datetime
import hashlib
import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path

from lib.config import hermes_root
from lib.cli_base import bootstrap as _bootstrap   # v2.5.0：统一编码兜底
from lib.fs import atomic_write, norm, slug_of
from lib import pii   # v3.0.0a1：EXTRUDE 账本行 note 脱敏（与 write_gate.log_gate 同口径）
from lib.memstore import (parse_frontmatter, dump_frontmatter, load_detail,
                          detail_prefixes, is_same_source)  # §C：解析/口径单源（2026-10-04 收编）

HERMES = hermes_root()
L1_DEFAULT = HERMES / "memories" / "MEMORY.md"
USER_DEFAULT = HERMES / "memories" / "USER.md"
DETAIL_DEFAULT = HERMES / "memories" / "detail"
DB_DEFAULT = HERMES / "eco.db"

# 口径单源（PORT_SPEC §4-C）：配额常量与 L1 计数移居 lib/metrics，此处仅 re-export 兼容
from lib.metrics import (MEMORY_QUOTA, USER_QUOTA, RATIO, MEMORY_TRIGGER, USER_TRIGGER,
                         chars_of, parse_l1, serialize_l1)  # noqa: F401
QUOTAS = {"MEMORY": MEMORY_QUOTA, "USER": USER_QUOTA}   # 文件名主干 → 配额（字符）
DEFAULT_QUOTA = MEMORY_QUOTA              # 未知文件名默认配额
from lib.config import TRUNC_MARK, gate_max_body      # v2.5.6：上限与截断标记单源
MAX_BODY = gate_max_body(220)                            # 提升时正文截断上限（字符）
PREFIX_LEN = 30                           # 同源判定：detail 正文规范化前 N 字符
PROMO_TYPES = ("semantic", "procedural")  # 提升候选允许的 type
MIN_OCCURRENCES = 3
MIN_SESSION_COUNT = 2
COOLDOWN_DAYS = 30  # 挤出冷却：extrude-from-L1 条目 N 天内不提升（防提升↔挤出震荡）
# 历史日期关键词：需带日期上下文（年份后跟 年/-/月 才命中，防「预算2000元」误伤）
DATE_HIST_RE = re.compile(r"(?:19\d{2}|20[0-2]\d)年|(?:19\d{2}|20(?:0\d|1\d|2[0-5]))[-/.年]\d{1,2}")











def qualified_prefixes(items: list) -> set:
    """提升候选（高价值）detail 正文的规范化前缀集合（防震荡策略：保护其 L1 同源条目）。
    注意与 memstore 口径不同：这里 = is_promo_candidate（type/occ/sess/冷却），非仅 active。"""
    s = set()
    for d in items:
        if is_promo_candidate(d):
            p = norm(d["body"])[:PREFIX_LEN]
            if p:
                s.add(p)
    return s


def is_promo_candidate(d: dict) -> bool:
    """提升候选：type∈{semantic,procedural} 且 status=active 且 occurrences≥3 且 session_count≥2。"""
    fm = d["fm"]
    if fm.get("type", "").strip().lower() not in PROMO_TYPES:
        return False
    if fm.get("status", "").strip().lower() != "active":
        return False
    # 挤出冷却：刚被 eco_quota 挤出落盘的条目（origin_session_id=extrude-from-L1）
    # 30 天内不提升，防止「提升↔挤出」跨轮震荡（2026-08-30 验收实测发现）
    if fm.get("origin_session_id", "").startswith("extrude-from-L1"):
        tt = fm.get("transaction_time", "")
        try:
            d = datetime.datetime.fromisoformat(tt).date()
        except (ValueError, TypeError):
            # P1：日期损坏时不再静默永久卡死——保守仍不提升（防震荡优先），但必须可见可修
            print(f"⚠️ {d.get('stem', '?')} transaction_time 无法解析（{tt!r}），冷却期判定失败，本轮保守不提升")
            return False
        if (datetime.date.today() - d).days < COOLDOWN_DAYS:
            return False
    try:
        occ = int(fm.get("occurrences", "0"))
    except ValueError:
        occ = 0
    try:
        sc = int(fm.get("session_count", "0"))
    except ValueError:
        sc = 0
    return occ >= MIN_OCCURRENCES and sc >= MIN_SESSION_COUNT


def pick_evict(meta: list, prefixes: set, qprefixes: set, safe_only: bool):
    """按优先级挑出本轮挤出条目。meta 每项 = (text, is_promoted_this_run, orig_index)。
    safe_only=True（防震荡段）：排除同轮新提升 + 高价值（其同源 detail 为提升候选）条目；
    ① detail 同源 → ② 历史日期关键词 → ③ 文件靠后（近似 LRU）；同类内从末尾往前。
    safe_only=False（纯合规段）：字面全序 ①②③，配额合规优先。"""
    n = len(meta)

    def cands(pred):
        return [i for i in range(n - 1, -1, -1) if not meta[i][1] and pred(meta[i][0])]

    def is_twin(t):
        return any(p in norm(t) for p in prefixes)

    def is_high_value(t):
        return any(p in norm(t) for p in qprefixes)

    if safe_only:
        for i in cands(lambda t: is_twin(t) and not is_high_value(t)):
            return i, "①detail同源(L2已存)"
        for i in cands(lambda t: bool(DATE_HIST_RE.search(t)) and not is_high_value(t)):
            return i, "②历史日期关键词"
        for i in cands(lambda t: not is_high_value(t)):
            return i, "③文件靠后(近似LRU)"
    else:
        for i in cands(is_twin):
            return i, "①detail同源(L2已存)"
        for i in cands(lambda t: bool(DATE_HIST_RE.search(t))):
            return i, "②历史日期关键词"
        for i in cands(lambda t: True):
            return i, "③文件靠后(近似LRU)"
    return None, None




def write_detail(detail_dir: Path, text: str, slug: str) -> Path:
    """挤出条目落盘 detail（保守默认 frontmatter）；文件名冲突自动加序号，绝不覆盖。"""
    now_d = datetime.date.today().isoformat()
    now_t = datetime.datetime.now().isoformat(timespec="seconds")
    fm = {
        "type": "semantic",
        "status": "active",
        "occurrences": "1",
        "session_count": "1",
        "first_seen": now_d,
        "last_seen": now_d,
        "valid_time": now_d,
        "transaction_time": now_t,
        "last_verified": now_d,
        "origin_session_id": "extrude-from-L1",
        # v3.0.1：scope 统一键位（挤出直写无候选来源 → 诚实空串；A5 按此分段判）
        "scope": "",
        "superseded_by": "",
    }
    path = detail_dir / f"{slug}.md"
    n = 2
    while path.exists():
        path = detail_dir / f"{slug}-{n}.md"
        n += 1
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    tmp.write_text(dump_frontmatter(fm, text), encoding="utf-8")
    os.replace(tmp, path)
    return path


def process_file(path: Path, quota_override: int, detail_items: list, prefixes: set, qprefixes: set,
                 allow_promo: bool = True):
    """处理单个 L1 文件：计算提升+挤出，返回 (actions, final_text|None)。
    final_text=None 表示无变化（不写回）。allow_promo=False 时跳过提升（只挤不升，
    避免同一条 detail 被重复提升进多个 L1 文件——P1-6）。"""
    if not path.exists():
        print(f"⚠️ L1 文件不存在，跳过: {path}")
        return None, None
    quota = quota_override or QUOTAS.get(path.stem, DEFAULT_QUOTA)
    limit = int(quota * RATIO)
    raw = path.read_text(encoding="utf-8", errors="replace")
    entries, nl = parse_l1(raw)
    before_chars = chars_of(entries)

    # ---- 功能1：提升（L2→L1），无同源才追加 ----
    promo_records = []
    if allow_promo:
        seen_prefixes: set = set()  # P1：同轮去重——近重复 detail 不双双提升进 L1
        for d in detail_items:
            if not is_promo_candidate(d):
                continue
            pfx = norm(d["body"])[:PREFIX_LEN]
            if pfx and pfx in seen_prefixes:
                continue
            if any(is_same_source(d["body"], e) for e in entries):
                continue
            seen_prefixes.add(pfx)
            _body = d["body"]
            # v2.5.6（审计 P0-2）：截断必须可见。这是"门②挤出直接写 detail"的那条路径，
            # 此前 51 篇正文被静默切在词中间，且没有 gate_log 记录。
            _body = _body if len(_body) <= MAX_BODY else _body[:MAX_BODY] + TRUNC_MARK
            promo_records.append((_body, d["name"], d["fm"]))
    work = [(e, False, i) for i, e in enumerate(entries)]
    work += [(b, True, None) for (b, _s, _f) in promo_records]

    # ---- 功能2：挤出（L1→L2），直到 ≤85%（防震荡两段式）----
    extrude_records = []
    skipped_promos = []
    if chars_of([m[0] for m in work]) > limit:
        if promo_records:
            # 第一段：安全挤出（排除同轮新提升 + 高价值同源条目）
            while chars_of([m[0] for m in work]) > limit:
                i, why = pick_evict(work, prefixes, qprefixes, safe_only=True)
                if i is None:
                    break
                t, _p, _o = work[i]
                extrude_records.append((t, why))
                del work[i]
        if chars_of([m[0] for m in work]) > limit:
            # 仍超限：撤掉提升（如有，信息留 L2）→ 还原原条目 → 字面 ①②③ 纯合规挤出
            if promo_records:
                for body, _stem, _fm in promo_records:
                    skipped_promos.append(body)
                    print(f"  跳过提升: {body[:40]}（配额不足，信息保留在 L2）")
                promo_records = []
            extrude_records = []
            work = [(e, False, i) for i, e in enumerate(entries)]
            while chars_of([m[0] for m in work]) > limit:
                i, why = pick_evict(work, prefixes, qprefixes, safe_only=False)
                if i is None:
                    break
                t, _p, _o = work[i]
                extrude_records.append((t, why))
                del work[i]

    if not promo_records and not extrude_records:
        print(f"[{path.name}] {before_chars}/{quota} 字符（85%线={limit}）→ 无动作")
        return None, None

    print(f"[{path.name}] {before_chars}/{quota} 字符（85%线={limit}）")
    actions = []
    # P1：has_src 只认 **active** detail——旧版扫全部条目，L1 条目仅与 dormant/superseded
    # 副本匹配时被当「L2 已存」跳写 detail，挤出后 active 副本凭空消失（违背绝不静默丢弃）
    active_bodies = [norm(d["body"]) for d in detail_items
                     if d["fm"].get("status", "").strip().lower() == "active"]
    for body, stem, fm in promo_records:
        t = fm.get("type", "?"); o = fm.get("occurrences", "?"); s = fm.get("session_count", "?")
        actions.append({"kind": "promote", "slug": stem, "preview": body[:40],
                        "reason": f"提升L2→L1 type={t} occ={o} sess={s}", "text": body,
                        "l1": path.name})
        print(f"  提升: {body[:40]} → L1 [type={t} occ={o} sess={s}]")
    for t, why in extrude_records:
        # 免写判定 = L1 条目被某 **active** detail 完整包含（前缀包含会静默丢尾部信息）
        has_src = any(norm(t) in b for b in active_bodies)
        slug = slug_of(t)
        actions.append({"kind": "extrude", "slug": slug, "preview": t[:40],
                        "reason": f"挤出-{why}" + ("" if has_src else f" → 写入detail:{slug}"),
                        "text": t, "write_detail": not has_src, "l1": path.name})
        print(f"  挤出: {t[:40]} [{why}]" + ("" if has_src else " → 无detail同源 → 将写入detail"))

    final_entries = [m[0] for m in work]
    final_chars = chars_of(final_entries)
    if final_chars > limit:
        print(f"  ⚠️ 完成后仍超限 {final_chars - limit} 字符（本轮无可挤出条目）")
    note = f"，跳过提升 {len(skipped_promos)} 条" if skipped_promos else ""
    print(f"  完成后: {len(final_entries)} 条, {final_chars} 字符（目标 ≤{limit}）{note}")
    return actions, serialize_l1(final_entries, nl)


def execute(actions: list, changed_files: list, detail_dir: Path, db: Path) -> None:
    """实际执行：备份 → 挤出落盘 detail → L1 原子写回 → eco.db 日志。

    P1：账本逐动作落库（每步成功即 commit）——旧版全部动作执行完才一次性写日志，
    中途任一 atomic_write 失败时已落地动作一行账都没有（账本与实际状态双向背离）。
    任一步失败即停止后续动作并抛出（由 main 捕获报告），失败前的动作已记账。
    """
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    # 1) 修改 L1 前先备份（备份绝不删除）
    for path, _ft in sorted(changed_files, key=lambda x: str(x[0])):
        bak = path.with_name(path.name + f".bak-配额前-{ts}")
        shutil.copy2(path, bak)
        print(f"  备份: {path.name} → {bak.name}")
    # 2) 挤出且 detail 无同源 → 先落盘 detail（保证信息不丢）
    detail_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db), timeout=10)
    n = 0
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS quota_log("
                     "ts TEXT, action TEXT, slug TEXT, l1_preview TEXT, reason TEXT)")
        # v3.0.0a1（审计 P0-3）：gate_log 与门① 同形（ts, fingerprint PK, action, target, note）。
        # 门② 挤出直写 detail 此前只记 quota_log，detail 账本视角是「无账直写」——
        # 存量 51 篇无 gate_log 的根因。schema 必须与 write_gate 建表一致，fingerprint
        # 用 "EXTRUDE|slug" 域隔离（门① 的指纹是候选 md5，两者互不碰撞）。
        conn.execute("CREATE TABLE IF NOT EXISTS gate_log("
                     "ts TEXT, fingerprint TEXT PRIMARY KEY, action TEXT, target TEXT, note TEXT)")
        now = datetime.datetime.now().isoformat(timespec="seconds")

        def log(action: str, slug: str, preview: str, reason: str) -> None:
            nonlocal n
            conn.execute(
                "INSERT INTO quota_log(ts, action, slug, l1_preview, reason) VALUES(?,?,?,?,?)",
                (now, action, slug, preview, reason))
            conn.commit()
            n += 1

        def log_gate_extrude(slug: str, text: str, reason: str) -> None:
            """v3.0.0a1（审计 P0-3）：挤出直写 detail 逐条记 gate_log EXTRUDE 行。
            与 write_gate.log_gate 同口径：INSERT OR IGNORE（幂等）+ pii.mask（note
            含 L1 原文，账本可被导出，先脱敏）。"""
            fp = hashlib.md5(f"EXTRUDE|{slug}".encode("utf-8")).hexdigest()
            conn.execute(
                "INSERT OR IGNORE INTO gate_log(ts, fingerprint, action, target, note) "
                "VALUES(?,?,?,?,?)",
                (datetime.datetime.now().isoformat(timespec="seconds"),
                 fp, "EXTRUDE", slug, pii.mask(reason)))
            conn.commit()

        for a in actions:
            if a["kind"] == "extrude" and a.get("write_detail"):
                p = write_detail(detail_dir, a["text"], a["slug"])
                print(f"  写入detail: {p.name} ← {a['preview']}")
                log("extrude", a["slug"], a["preview"], a["reason"] + "（detail已落盘）")
                log_gate_extrude(a["slug"], a["text"], a["reason"] + "（detail已落盘）")
        # 3) L1 原子写回（逐文件写、逐文件记账——失败时账本停在最后成功处）
        for path, final_text in changed_files:
            atomic_write(path, final_text)
            print(f"  写回: {path.name}")
            for a in actions:
                if a.get("l1") == path.name:
                    log(a["kind"], a["slug"], a["preview"], a["reason"])
    except Exception:
        conn.commit()  # 异常路径：把已成功落地的动作先记账再抛出
        raise
    finally:
        conn.close()
    print(f"  日志: {n} 行 → {db}")


LOCK_FILE = HERMES / "scripts" / ".eco_quota.lock"


def run(args) -> int:
    """入口：dry-run 不抢锁；实际执行抢锁防 cron 与手动并发（P1-7）。

    v2.5.0 修复（B7 + stale 回收）：
      旧实现直接 `os.open(LOCK_FILE, O_CREAT|O_EXCL)`，而 LOCK_FILE 在
      `<root>/scripts/` 下 —— 用户按 README 设 MEMORY_ECOLOGY_ROOT 指向新目录时
      该目录不存在 -> FileNotFoundError，门②在自定义数据根上必崩。
      改走 gatekit.acquire_lock_auto（mkdir 父目录 + 回收死进程残留锁），与门①/门④统一。
      附带修掉两个隐患：
        - 旧实现不回收 stale 锁 -> 上次异常退出会让本门永久跳过
        - 抢不到锁时仍 return 0（跳过）——与"执行完成"共用退出码，调度器无法区分。
          本轮不改退出码语义（会动调度契约），只补 stderr 提示。
    """
    if not args.dry_run:
        from lib.gatekit import acquire_lock_auto
        ok, how = acquire_lock_auto(LOCK_FILE)
        if not ok:
            print("⚠️ 已有 eco_quota 实例在运行，本轮跳过", file=sys.stderr)
            return 0
        if how == "stale-cleared":
            print("ℹ️ 已回收上次残留的锁", file=sys.stderr)
    try:
        return _run_inner(args)
    finally:
        if not args.dry_run:
            LOCK_FILE.unlink(missing_ok=True)


def _run_inner(args) -> int:
    l1_files = list(args.l1) if args.l1 else [L1_DEFAULT, USER_DEFAULT]
    detail_items = load_detail(args.detail)
    prefixes = detail_prefixes(detail_items)
    qprefixes = qualified_prefixes(detail_items)

    print("== eco_quota 巩固/配额门 ==")
    print(f"L1: {', '.join(str(p) for p in l1_files)} | detail: {args.detail} | db: {args.db}")
    if args.dry_run:
        print("--dry-run：以下为将执行的动作，未修改任何文件、未写日志--")

    all_actions = []
    changed_files = []
    failures = 0
    for i, path in enumerate(l1_files):
        try:
            actions, final_text = process_file(path, args.quota, detail_items, prefixes, qprefixes,
                                               allow_promo=(i == 0))
        except Exception as e:
            failures += 1
            print(f"⚠️ {path} 处理失败: {e}")
            continue
        if actions:
            all_actions.extend(actions)
        if final_text is not None:
            changed_files.append((path, final_text))

    n_p = sum(1 for a in all_actions if a["kind"] == "promote")
    n_e = sum(1 for a in all_actions if a["kind"] == "extrude")
    n_d = sum(1 for a in all_actions if a["kind"] == "extrude" and a.get("write_detail"))
    if not all_actions:
        print("ℹ️ 无提升/挤出动作")
        # v0.3 对齐门③④：零动作轮次写 idle 心跳（"门在跑"有据可查）
        if not args.dry_run:
            try:
                conn = sqlite3.connect(str(args.db), timeout=10)
                try:
                    conn.execute("CREATE TABLE IF NOT EXISTS quota_log("
                                 "ts TEXT, action TEXT, slug TEXT, l1_preview TEXT, reason TEXT)")
                    conn.execute(
                        "INSERT INTO quota_log(ts, action, slug, l1_preview, reason) VALUES(?,?,?,?,?)",
                        (datetime.datetime.now().isoformat(timespec="seconds"),
                         "idle", "", "", "heartbeat 零动作（门在跑）"))
                    conn.commit()
                finally:
                    conn.close()
            except Exception as e:
                print(f"⚠️ idle 心跳写入失败: {e}")
    else:
        print(f"汇总: 提升 {n_p} 条 / 挤出 {n_e} 条（其中 {n_d} 条落盘 detail）/ 涉及 {len(changed_files)} 个 L1 文件")
        if not args.dry_run:
            execute(all_actions, changed_files, args.detail, args.db)
            print("✅ eco_quota 执行完成")
    return 1 if failures else 0


def main() -> int:
    _bootstrap()   # v2.5.0：编码兜底先于一切
    from lib.cli_base import banner as _banner   # v2.5.6：部署可观测（哈希+版本+数据根）
    _banner("eco_quota", file=__file__)
    ap = argparse.ArgumentParser(description="记忆生态巩固/配额门（纯规则，零 LLM 调用）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印将执行的动作（提升/挤出各条），不修改任何文件、不写日志")
    ap.add_argument("--l1", action="append", type=Path, metavar="PATH",
                    help="L1 文件路径（可多次指定；默认 memories/MEMORY.md + memories/USER.md；"
                         "提升写入第一个 L1 文件）")
    ap.add_argument("--detail", type=Path, default=DETAIL_DEFAULT, metavar="DIR",
                    help="L2 详情目录（默认 memories/detail）")
    ap.add_argument("--db", type=Path, default=DB_DEFAULT, metavar="PATH",
                    help="eco.db 路径（默认 hermes/eco.db）")
    ap.add_argument("--quota", type=int, default=0, metavar="N",
                    help="统一配额覆盖（默认按文件名：MEMORY=3000/USER=1500/其他=3000）")
    args = ap.parse_args()
    try:
        return run(args)
    except Exception as e:
        print(f"⚠️ eco_quota 异常: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
