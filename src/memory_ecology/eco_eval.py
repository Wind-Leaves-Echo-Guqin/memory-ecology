#!/usr/bin/env python3
"""生态记忆评测 eco_eval（版本迭代策略 v2 阶段 C：LongMemEval 五维伪 gold 一次性验收）。

五维（LongMemEval：IE/MR/KU/TR/Abstention）的单用户适配：
- IE  信息提取：近 7 天 write_gate 整合日志存在且无失败（提取→整合链路工作）
- MR  多会话推理：跨会话主题抽查命中率（detail/MEMORY/USER 有对应条目）
- KU  知识更新：gate_log 中 CONFLICT/UPDATE 动作与 quarantine superseded 条目（冲突机制在工作）
- TR  时间推理：detail 条目 transaction_time/valid_time 字段覆盖率（事件钟字段）
- ABS 安全弃答：抽样 L1 条目 LLM 判「可疑/过时/可能误导」率（弱证据标注）

判定线（v2 文档 §3，门禁非调参）：
- 管道失败数/月 ≤ 5
- MR 主题命中率 ≥ 60%
- ABS 可疑率 ≤ 30%（弱证据，仅参考）

用法: python eco_eval.py [--dry-run] [--detail DIR] [--db PATH] [--logdir DIR]
      （一次性/季度手动验收，不设 cron）
"""
import argparse
import datetime
import json
import re
import sqlite3
import sys
from pathlib import Path

from lib.config import hermes_root
from lib import llm as _llm

HERMES = hermes_root()
DETAIL_DIR = HERMES / "memories" / "detail"
LOG_DIR = HERMES / "memories" / "gate_log"
QUARANTINE_DIR = HERMES / "memories" / "quarantine"
DB = HERMES / "eco.db"

MR_TOPICS = ["python", "data", "report", "model", "config", "script", "image", "test"]
ABS_SAMPLE = 5  # 抽样判分条数

ABS_PROMPT = """你是记忆质量审计员。以下是一条注入到 AI 助手常驻上下文中的记忆条目。
判断它是否「可疑」：过时（含历史日期且可能已变化）、明显错误、可能误导助手、或已无价值。
输出 JSON：{"suspicious": true/false, "reason": "一句话"}。只输出 JSON。
条目：
{entry}
"""




def llm_judge(entry: str) -> dict:
    content = _llm.complete(ABS_PROMPT.replace("{entry}", entry[:300]),
                            max_tokens=120, temperature=0.1, timeout=60)
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        return {"suspicious": False, "reason": "解析失败"}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"suspicious": False, "reason": "解析失败"}


def parse_frontmatter(text: str) -> tuple[dict, str]:
    fm: dict = {}
    body = text
    if text.startswith("\ufeff"):
        text = text[1:]
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--detail", type=Path, default=DETAIL_DIR)
    ap.add_argument("--db", type=Path, default=DB)
    ap.add_argument("--logdir", type=Path, default=LOG_DIR)
    ap.add_argument("--topics", type=str, default=",".join(MR_TOPICS))
    args = ap.parse_args()

    today = datetime.date.today().isoformat()
    report: list[str] = []
    scores: dict[str, tuple] = {}

    # ---- IE：提取→整合管道健康（近 7 天，含失败数检查）----
    gate_files = list(args.logdir.glob("gate-*.md")) if args.logdir.exists() else []
    recent = [f for f in gate_files if (datetime.date.today() - _date_of(f.name)).days <= 7] if gate_files else []
    ie_fails = 0
    for f in recent:
        try:
            t = f.read_text(encoding="utf-8", errors="replace")
            m = re.search(r"失败 (\d+) 条", t)
            if m:
                ie_fails += int(m.group(1))
        except OSError:
            pass
    ie_ok = len(recent) >= 1 and ie_fails == 0
    scores["IE"] = ("PASS" if ie_ok else "FAIL", f"近 7 天整合日志 {len(recent)} 份，失败 {ie_fails} 条")
    report.append(f"IE 信息提取: {'✅' if ie_ok else '❌'} 近7天 gate 日志 {len(recent)} 份（需 ≥1），失败 {ie_fails} 条（需 0）")

    # ---- MR：跨会话主题抽查 ----
    topics = [t for t in args.topics.split(",") if t]
    corpus = []
    if args.detail.exists():
        for f in args.detail.glob("*.md"):
            try:
                _, body = parse_frontmatter(f.read_text(encoding="utf-8"))
                corpus.append(body)
            except OSError:
                continue
    for p in (HERMES / "memories" / "MEMORY.md", HERMES / "memories" / "USER.md"):
        if p.exists():
            corpus.append(p.read_text(encoding="utf-8", errors="replace"))
    joined = "\n".join(corpus)
    hits = [t for t in topics if t in joined]
    mr_rate = len(hits) / len(topics) if topics else 0.0
    mr_ok = mr_rate >= 0.6
    scores["MR"] = ("PASS" if mr_ok else "FAIL", f"{len(hits)}/{len(topics)} 主题命中")
    report.append(f"MR 多会话推理: {'✅' if mr_ok else '❌'} 主题命中 {len(hits)}/{len(topics)}（需 ≥60%）")

    # ---- KU：知识更新机制 ----
    ku_conflicts = 0
    try:
        conn = sqlite3.connect(args.db, timeout=10)
        try:
            ku_conflicts = conn.execute(
                "SELECT COUNT(*) FROM gate_log WHERE action IN ('CONFLICT','UPDATE')").fetchone()[0]
        except sqlite3.OperationalError:
            ku_conflicts = 0
        conn.close()
    except Exception:
        ku_conflicts = 0
    ku_ok = ku_conflicts >= 1
    scores["KU"] = ("PASS" if ku_ok else "FAIL", f"gate_log 冲突/更新 {ku_conflicts} 次")
    report.append(f"KU 知识更新: {'✅' if ku_ok else '❌'} 冲突/更新动作 {ku_conflicts} 次（机制已运转则 PASS）")

    # ---- TR：事件钟字段覆盖率 ----
    detail_items = []
    if args.detail.exists():
        for f in args.detail.glob("*.md"):
            try:
                fm, _ = parse_frontmatter(f.read_text(encoding="utf-8"))
                detail_items.append(fm)
            except OSError:
                continue
    n = len(detail_items)
    tt_cov = sum(1 for fm in detail_items if fm.get("transaction_time")) / n if n else 1.0
    vt_cov = sum(1 for fm in detail_items if fm.get("valid_time")) / n if n else 1.0
    tr_ok = n == 0 or (tt_cov == 1.0 and vt_cov >= 0.5)
    scores["TR"] = ("PASS" if tr_ok else "FAIL", f"transaction_time {tt_cov:.0%} / valid_time {vt_cov:.0%}")
    report.append(f"TR 时间推理: {'✅' if tr_ok else '❌'} detail {n} 条, transaction_time 覆盖 {tt_cov:.0%}（需100%）")

    # ---- ABS：安全弃答（LLM 判分，弱证据）----
    l1_text = ""
    for p in (HERMES / "memories" / "MEMORY.md", HERMES / "memories" / "USER.md"):
        if p.exists():
            l1_text += p.read_text(encoding="utf-8", errors="replace") + "\n"
    entries = [e.strip() for e in re.split(r"§|\n", l1_text) if len(e.strip()) > 10]
    suspicious = 0
    judged = 0
    if entries and not args.dry_run:
        for e in entries[:ABS_SAMPLE]:
            try:
                res = llm_judge(e)
            except Exception as ex:
                report.append(f"ABS 判分异常: {ex}")
                continue
            judged += 1
            if res.get("suspicious"):
                suspicious += 1
                report.append(f"  ⚠️ 可疑条目: {e[:40]}（{res.get('reason','')[:40]}）")
    abs_rate = suspicious / judged if judged else 0.0
    abs_ok = judged >= 3 and abs_rate <= 0.30
    scores["ABS"] = ("PASS" if abs_ok else "FAIL",
                     f"{suspicious}/{judged} 可疑（≤30%）；样本不足视为未达标")
    report.append(f"ABS 安全弃答: {'✅' if abs_ok else '❌'} {suspicious}/{judged} 可疑率 {abs_rate:.0%}"
                  f"（需 ≥3 样本且 ≤30%，弱证据）")

    # ---- 判定线汇总 ----
    fails = [k for k, (st, _) in scores.items() if st == "FAIL"]
    report.append("")
    report.append(f"判定线汇总: {'✅ 全部达标' if not fails else '❌ 未达标: ' + ', '.join(fails)}")
    report.append("（注：门禁非调参——只判定过/不过，不做提升曲线叙事）")

    print("\n".join(report))
    if not args.dry_run:
        out = args.logdir / f"eval-{today}.md"
        args.logdir.mkdir(parents=True, exist_ok=True)
        out.write_text("# 记忆生态评测（LongMemEval 五维）\n\n" + "\n".join(report) + "\n", encoding="utf-8")
        print(f"✅ 评测报告 → {out}")
    return 1 if fails else 0


def _date_of(name: str) -> datetime.date:
    m = re.search(r"(\d{4}-\d{2}-\d{2})", name)
    if m:
        try:
            return datetime.date.fromisoformat(m.group(1))
        except ValueError:
            pass
    return datetime.date.today()


if __name__ == "__main__":
    sys.exit(main())
