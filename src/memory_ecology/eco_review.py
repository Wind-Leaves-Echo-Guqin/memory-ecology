#!/usr/bin/env python3
"""记忆生态复核门 eco_review（纯规则，无 LLM 调用）。

把 L2 详情层记忆（memories/detail/）做过期复核与状态迁移，并产出碎片合并候选
与 quarantine 过期清理。仅依赖标准库，是 write_gate 写入管道之外的复核侧门：

1) 过期复核：扫描 detail 目录所有 .md，按 type 超期阈值判定
   - status=active  且超期 → frontmatter status 改为 dormant（原地原子重写，可逆）
   - status=dormant 且超期 → 移动到 memories/archive/<slug>.md（重名加时间戳后缀）
   - 超期阈值：semantic/procedural/lesson/缺失 = 90 天；episodic = 30 天
   - last_verified 格式 YYYY-MM-DD；解析失败视为超期
   - last_verified 不因本次复核自动刷新（刷新是写入管道的事），本次只做状态迁移
2) 碎片合并候选：detail 内两两比较正文相似度（difflib.SequenceMatcher ratio≥0.7
   且 slug 不同）→ 输出候选清单，不执行合并。detail≤300 个文件全量两两比较，
   >300 个文件只比较同 type 的。
3) quarantine 清理：quarantine 下所有文件（含日期子目录），mtime 距今>90 天 →
   移动到 archive/，保留子目录结构，重名加时间戳后缀。

每次实际执行（非 --dry-run）写清单 memories/gate_log/review-<YYYY-MM-DD>.md，
并把每条动作写入 eco.db 的 review_log 表（ts/action/slug/reason）。

用法:
    python eco_review.py [--dry-run]
        [--detail DIR] [--quarantine DIR] [--archive DIR]
        [--db PATH] [--logdir DIR]

默认路径以本脚本上一级目录为 hermes 根目录：
    detail      = <hermes>/memories/detail/
    quarantine  = <hermes>/memories/quarantine/
    archive     = <hermes>/memories/archive/
    db          = <hermes>/eco.db
    logdir      = <hermes>/memories/gate_log/
若传入了 detail/quarantine/archive 任一覆盖，根目录自动重定位为其上一级
（即 <...>/memories），其余未覆盖项跟随重定位，便于测试。

安全：
- 绝不删除任何文件（移动/归档不算删除）
- 不触碰 memories/MEMORY.md、USER.md、pending/ 与 scripts/ 其他脚本
- --dry-run 只打印将执行的动作，不修改/移动任何文件、不写日志

返回码：0=成功（无失败项），1=存在失败项。
"""
from lib.fs import atomic_write

import argparse
import difflib
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, date
from pathlib import Path

TODAY = date.today()

EXPIRY_THRESHOLDS = {
    'semantic': 90,
    'episodic': 30,
    'procedural': 90,
    'lesson': 90,
    '': 90,
}
DEFAULT_THRESHOLD = 90
SIMILARITY_THRESHOLD = 0.70
QUARANTINE_DAYS = 90
FULL_PAIRWISE_LIMIT = 300   # detail 文件数 ≤300 全量两两比较，>300 只比同 type

DB_TABLE_SQL = (
    'CREATE TABLE IF NOT EXISTS review_log ('
    'ts TEXT, action TEXT, slug TEXT, reason TEXT)'
)
DB_INSERT_SQL = 'INSERT INTO review_log (ts, action, slug, reason) VALUES (?,?,?,?)'


def line_ending(ln: str) -> str:
    if ln.endswith('\r\n'):
        return '\r\n'
    if ln.endswith('\n'):
        return '\n'
    if ln.endswith('\r'):
        return '\r'
    return ''


def is_protected(p: Path) -> bool:
    """保护路径：scripts/、pending/、MEMORY.md、USER.md 一律不碰。"""
    parts = [x.lower() for x in Path(p).parts]
    if any(x in ('scripts', 'pending') for x in parts):
        return True
    name = parts[-1] if parts else ''
    return name in ('memory.md', 'user.md')


def clean_value(s) -> str:
    """极简 YAML 标量清理：去首尾空白、去尾注释、剥引号。"""
    s = (s or '').strip()
    idx = s.find(' #')
    if idx != -1:
        s = s[:idx].strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        s = s[1:-1]
    return s.strip()


def parse_frontmatter(text: str):
    """极简解析 YAML frontmatter。返回 (fields: dict|None, body: str)。

    无 frontmatter 或格式损坏 → (None, 全文)。body 为正文（去掉首尾空白）。
    """
    if text.startswith('﻿'):
        text = text[1:]
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip('\r\n').strip() != '---':
        return None, text
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].rstrip('\r\n').strip() == '---':
            end_idx = i
            break
    if end_idx is None:
        return None, text
    fields = {}
    for ln in lines[1:end_idx]:
        s = ln.rstrip('\r\n').strip()
        if not s or s.startswith('#') or ':' not in s:
            continue
        key, _, val = s.partition(':')
        fields[key.strip()] = clean_value(val)
    body = ''.join(lines[end_idx + 1:]).strip()
    return fields, body


def parse_date_value(s):
    s = (s or '').strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, '%Y-%m-%d').date()
    except ValueError:
        return None


def days_since(d):
    """距今天数；解析失败/缺失返回 None（调用方视为超期）。未来日期按 0 天。"""
    if d is None:
        return None
    delta = (TODAY - d).days
    return max(0, delta) if delta >= 0 else 0


def days_display(days) -> str:
    return str(days) if days is not None else '未知(视为超期)'


def expiry_threshold(mtype) -> int:
    return EXPIRY_THRESHOLDS.get((mtype or '').strip().lower(), DEFAULT_THRESHOLD)


def rewrite_status(raw: str, new_status: str, refresh_lv: bool = False):
    """替换 frontmatter 里的 status 行；refresh_lv=True 时同时刷新 last_verified=今天
    （mark_dormant 用：降级后给足观察期，避免「active→dormant→archive」两天走完）。"""
    bom = ''
    if raw.startswith('\ufeff'):
        bom = '\ufeff'
        raw = raw[len(bom):]
    lines = raw.splitlines(keepends=True)
    in_fm = False
    today = date.today().isoformat()
    for i, ln in enumerate(lines):
        stripped = ln.rstrip('\r\n').strip()
        if stripped == '---':
            in_fm = not in_fm
            continue
        if in_fm:
            indent = ln[:len(ln) - len(ln.lstrip())]
            ending = line_ending(ln)
            if stripped.startswith('status:'):
                lines[i] = f"{indent}status: {new_status}{ending}"
            elif refresh_lv and stripped.startswith('last_verified:'):
                lines[i] = f"{indent}last_verified: {today}{ending}"
    return bom + ''.join(lines)




def safe_filename(name: str) -> str:
    name = (name or 'unnamed').strip()
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, '_')
    name = ''.join(c if ord(c) >= 32 else '_' for c in name)
    name = name.strip(' .')
    return name or 'unnamed'


def unique_target(archive_dir: Path, rel_path) -> Path:
    """归档目标：已存在则加时间戳后缀，保证文件名唯一。"""
    target = archive_dir / rel_path
    if not target.exists():
        return target
    parent = target.parent
    stem, suffix = target.stem, target.suffix
    ts = datetime.now().strftime('%Y%m%d%H%M%S')
    candidate = parent / f"{stem}-{ts}{suffix}"
    n = 1
    while candidate.exists():
        candidate = parent / f"{stem}-{ts}-{n}{suffix}"
        n += 1
    return candidate


def scan_detail(detail_dir: Path):
    """扫描 detail 目录，返回 (actions, files, failures)。仅读取，不做改动。"""
    actions, files, failures = [], [], 0
    if not detail_dir.is_dir():
        print(f"  ⚠️ detail 目录不存在，跳过: {detail_dir}")
        return actions, files, failures
    md_files = sorted(p for p in detail_dir.iterdir()
                      if p.is_file() and p.suffix.lower() == '.md')
    for p in md_files:
        if is_protected(p):
            continue
        try:
            with p.open('r', encoding='utf-8', newline='') as f:
                raw = f.read()
        except Exception as e:
            print(f"  ⚠️ 读取失败 {p.name}: {e}")
            failures += 1
            continue
        fields, body = parse_frontmatter(raw)
        if fields is None:
            print(f"  ⚠️ frontmatter 缺失/损坏，跳过: {p.name}")
            failures += 1
            continue
        mtype = fields.get('type', '')
        status = clean_value(fields.get('status', '')).lower()
        lv = parse_date_value(fields.get('last_verified'))
        days = days_since(lv)
        threshold = expiry_threshold(mtype)
        overdue = lv is None or (days is not None and days > threshold)
        slug = fields.get('name') or p.stem
        if lv is None:
            reason = f"last_verified 缺失/解析失败（阈值 {threshold} 天，视为超期）"
        else:
            reason = f"超期 {days} 天（阈值 {threshold} 天）"
        rec = {
            'path': p, 'slug': slug, 'type': mtype or 'unknown',
            'status': status or 'unknown', 'last_verified': fields.get('last_verified') or '',
            'days': days, 'threshold': threshold, 'overdue': overdue,
            'body': body, 'raw': raw, 'action': None,
        }
        if status == 'active' and overdue:
            rec['action'] = ('mark_dormant', reason)
        elif status == 'dormant' and overdue:
            rec['action'] = ('archive', reason)
        files.append(rec)
        if rec['action']:
            actions.append(rec)
    return actions, files, failures


def apply_detail_actions(actions, archive_dir: Path, dry_run: bool) -> int:
    failures = 0
    for rec in actions:
        act, reason = rec['action']
        p = rec['path']
        try:
            if act == 'mark_dormant':
                if dry_run:
                    print(f"  [mark_dormant] {rec['slug']} → status 改为 dormant（{reason}）")
                    continue
                new_content = rewrite_status(rec['raw'], 'dormant', refresh_lv=True)
                if new_content is None:
                    raise RuntimeError('frontmatter 中未找到 status 行')
                atomic_write(p, new_content)
                print(f"  [mark_dormant] {rec['slug']} status→dormant（{reason}）")
            elif act == 'archive':
                target = unique_target(archive_dir, safe_filename(rec['slug']) + '.md')
                if dry_run:
                    print(f"  [archive] {rec['slug']} → {target}（{reason}）")
                    continue
                archive_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(p), str(target))
                print(f"  [archive] {rec['slug']} → {target}（{reason}）")
        except Exception as e:
            print(f"  ⚠️ {act} 失败 {rec['slug']}: {e}")
            failures += 1
    return failures


def find_merge_candidates(files):
    """正文两两比较（difflib ratio≥0.7 且 slug 不同）。detail>300 文件只比同 type。"""
    candidates = []
    if len(files) <= FULL_PAIRWISE_LIMIT:
        groups = [files]
    else:
        by_type = {}
        for f in files:
            by_type.setdefault(f['type'], []).append(f)
        groups = list(by_type.values())
    seen = set()
    for group in groups:
        n = len(group)
        for i in range(n):
            a = group[i]
            for j in range(i + 1, n):
                b = group[j]
                if a['slug'] == b['slug']:
                    continue
                if not a['body'] or not b['body']:
                    continue
                if not a['path'].exists() or not b['path'].exists():
                    continue  # 本轮已被归档/移动的条目不参与合并候选
                key = tuple(sorted((a['slug'], b['slug'])))
                if key in seen:
                    continue
                seen.add(key)
                ratio = difflib.SequenceMatcher(None, a['body'], b['body']).ratio()
                if ratio >= SIMILARITY_THRESHOLD:
                    candidates.append({'a': a['slug'], 'b': b['slug'], 'ratio': ratio})
    candidates.sort(key=lambda c: (-c['ratio'], c['a'], c['b']))
    return candidates


def scan_quarantine(quarantine_dir: Path):
    """扫描 quarantine 下所有文件（含日期子目录），mtime>90 天 → 清理候选。"""
    actions, failures = [], 0
    if not quarantine_dir.is_dir():
        print(f"  ⚠️ quarantine 目录不存在，跳过: {quarantine_dir}")
        return actions, failures
    for p in sorted(quarantine_dir.rglob('*.md')):
        if not p.is_file() or is_protected(p):
            continue
        days = None
        try:
            rel = p.relative_to(quarantine_dir)
        except ValueError:
            rel = Path(p.name)
        # 优先用日期子目录名（YYYY-MM-DD）算天数——git 快照恢复会重置 mtime，目录名更可靠
        try:
            d0 = datetime.strptime(rel.parts[0], '%Y-%m-%d').date()
            days = max(0, (TODAY - d0).days)
        except (ValueError, IndexError):
            days = None
        if days is None:
            try:
                mtime = datetime.fromtimestamp(p.stat().st_mtime).date()
                days = max(0, (TODAY - mtime).days)
            except OSError as e:
                print(f"  ⚠️ stat 失败 {p}: {e}")
                failures += 1
                continue
        if days > QUARANTINE_DAYS:
            actions.append({'path': p, 'rel': rel, 'days': days})
    return actions, failures


def apply_quarantine_actions(actions, archive_dir: Path, dry_run: bool) -> int:
    failures = 0
    for rec in actions:
        p, rel, days = rec['path'], rec['rel'], rec['days']
        target = unique_target(archive_dir, rel)
        if dry_run:
            print(f"  [quarantine_cleanup] {rel}（mtime {days} 天）→ {target}")
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(target))
            print(f"  [quarantine_cleanup] {rel} → {target}（mtime {days} 天）")
        except Exception as e:
            print(f"  ⚠️ quarantine_cleanup 失败 {rel}: {e}")
            failures += 1
    return failures


def write_db(entries, db_path: Path) -> int:
    """写 review_log 表（CREATE TABLE IF NOT EXISTS）。返回失败数（0/1）。"""
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        try:
            conn.execute(DB_TABLE_SQL)
            if entries:
                conn.executemany(DB_INSERT_SQL, entries)
            conn.commit()
        finally:
            conn.close()
        return 0
    except Exception as e:
        print(f"  ⚠️ 数据库写入失败 {db_path}: {e}")
        return 1


def cell(s) -> str:
    return str(s).replace('|', '/').replace('\n', ' ')


def build_report(date_str, detail_actions, candidates, q_actions, files, failures) -> str:
    lines = [
        f"# 记忆生态复核门日志 - {date_str}",
        "",
        f"- 生成时间: {datetime.now().isoformat(timespec='seconds')}",
        f"- 扫描 detail 条数: {len(files)}",
        f"- 失败项: {failures}",
        "",
        "## 一、过期复核清单",
        "",
        "| slug | type | status | last_verified | 超期天数 | 动作 | 原因 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in detail_actions:
        act, reason = r['action']
        lines.append(
            f"| {cell(r['slug'])} | {cell(r['type'])} | {cell(r['status'])} | "
            f"{cell(r['last_verified'])} | {days_display(r['days'])} | {act} | {cell(reason)} |"
        )
    lines += ["", "## 二、碎片合并候选", "", "| slug A | slug B | 相似度 |", "|---|---|---|"]
    for c in candidates:
        lines.append(f"| {cell(c['a'])} | {cell(c['b'])} | {c['ratio']:.3f} |")
    lines += ["", "## 三、quarantine 清理清单", "", "| 文件 | 超期天数 | 动作 |", "|---|---|---|"]
    for r in q_actions:
        lines.append(f"| {cell(str(r['rel']))} | {r['days']} | quarantine_cleanup |")
    lines.append("")
    return '\n'.join(lines)


def write_gate_log(logdir: Path, date_str: str, content: str) -> Path:
    logdir.mkdir(parents=True, exist_ok=True)
    path = logdir / f"review-{date_str}.md"
    with path.open('w', encoding='utf-8', newline='') as f:
        f.write(content)
    return path


def resolve_paths(args):
    script_dir = Path(__file__).resolve().parent
    root = script_dir.parent  # hermes 根目录
    mem_override = args.detail or args.quarantine or args.archive
    if mem_override:
        d = Path(mem_override)
        # 覆盖项形如 <root>/memories/{detail,quarantine,archive} → 重定位根目录
        root = d.parent.parent if d.name in ('detail', 'quarantine', 'archive') else d.parent
    detail = Path(args.detail) if args.detail else root / 'memories' / 'detail'
    quarantine = Path(args.quarantine) if args.quarantine else root / 'memories' / 'quarantine'
    archive = Path(args.archive) if args.archive else root / 'memories' / 'archive'
    db = Path(args.db) if args.db else root / 'eco.db'
    logdir = Path(args.logdir) if args.logdir else root / 'memories' / 'gate_log'
    return detail, quarantine, archive, db, logdir


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description='记忆生态复核门：过期复核/降级/归档/合并候选/quarantine 清理')
    p.add_argument('--dry-run', action='store_true',
                   help='只打印将执行的动作，不修改/移动任何文件、不写日志')
    p.add_argument('--detail', help='L2 详情层目录（覆盖默认）')
    p.add_argument('--quarantine', help='quarantine 目录（覆盖默认）')
    p.add_argument('--archive', help='归档目录（覆盖默认）')
    p.add_argument('--db', help='eco.db 路径（覆盖默认）')
    p.add_argument('--logdir', help='gate_log 清单目录（覆盖默认）')
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    detail_dir, quarantine_dir, archive_dir, db_path, logdir = resolve_paths(args)
    dry = args.dry_run
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

    print("=== 记忆生态复核门 ===")
    print(f"模式: {'DRY-RUN（仅打印，不写任何文件/数据库）' if dry else '实际执行'}")
    print(f"detail     : {detail_dir}")
    print(f"quarantine : {quarantine_dir}")
    print(f"archive    : {archive_dir}")
    print(f"db         : {db_path}")
    print(f"logdir     : {logdir}")
    print()

    total_failures = 0
    now_ts = datetime.now().isoformat(timespec='seconds')

    print("—— 功能1 过期复核 ——")
    detail_actions, files, f1 = scan_detail(detail_dir)
    total_failures += f1
    total_failures += apply_detail_actions(detail_actions, archive_dir, dry)
    mark_count = sum(1 for r in detail_actions if r['action'][0] == 'mark_dormant')
    arch_count = sum(1 for r in detail_actions if r['action'][0] == 'archive')
    print(f"  过期复核：扫描 {len(files)} 条，降级 {mark_count} 条，归档 {arch_count} 条")
    print()

    print("—— 功能2 碎片合并候选 ——")
    candidates = find_merge_candidates(files)
    for c in candidates:
        print(f"  [merge_candidate] {c['a']} <-> {c['b']}（相似度 {c['ratio']:.2f}）")
    print(f"  合并候选：{len(candidates)} 对")
    print()

    print("—— 功能3 quarantine 清理 ——")
    q_actions, f3 = scan_quarantine(quarantine_dir)
    total_failures += f3
    total_failures += apply_quarantine_actions(q_actions, archive_dir, dry)
    print(f"  quarantine 清理：{len(q_actions)} 条")
    print()

    log_entries = []
    for r in detail_actions:
        act, reason = r['action']
        log_entries.append((now_ts, act, r['slug'], reason))
    for c in candidates:
        log_entries.append(
            (now_ts, 'merge_candidate', f"{c['a']}|{c['b']}", f"similarity={c['ratio']:.3f}"))
    for r in q_actions:
        log_entries.append(
            (now_ts, 'quarantine_cleanup', str(r['rel']), f"mtime={r['days']}天"))

    if not dry:
        total_failures += write_db(log_entries, db_path)
        try:
            gate_path = write_gate_log(logdir, TODAY.strftime('%Y-%m-%d'),
                                       build_report(TODAY.strftime('%Y-%m-%d'),
                                                    detail_actions, candidates,
                                                    q_actions, files, total_failures))
            print(f"清单输出: {gate_path}")
        except Exception as e:
            print(f"  ⚠️ 清单写入失败: {e}")
            total_failures += 1
    else:
        print("[dry-run] 未写入 eco.db，未生成 gate_log 清单")

    print()
    print(f"失败项: {total_failures}")
    return 1 if total_failures > 0 else 0


if __name__ == '__main__':
    sys.exit(main())
