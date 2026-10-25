#!/usr/bin/env python3
"""生态写入整合门 write_gate（版本迭代策略 v2 门①）。

把会话提取管道产出的 pending 候选整合进 L2 详情层（memories/detail/）：
1) 类型分型 episodic/semantic/procedural/lesson（LLM 初判 + 规则兜底）
2) 与已有 detail 条目相似比对（difflib，规范化后 ratio）
3) 四动作决策 ADD/UPDATE/NOOP/CONFLICT（相似命中才批量调 LLM；无命中规则直判 ADD）
4) 事件钟字段 valid_time/transaction_time（Graphiti 双时态轻量版）
5) 矛盾旧条目标 superseded 后移入 quarantine（永不物理删除）

设计：独立 gate 脚本（保护区外，不改提取管道）；只读 pending、只写
detail/quarantine/gate_log/eco.db；幂等（fingerprint 防重 + 每动作即时 commit +
并发锁）；原子写（临时文件+os.replace）；--dry-run 只报告不动手。

用法: python write_gate.py [--dry-run] [--pending DIR] [--detail DIR]
      [--db PATH] [--logdir DIR]   （cron 每日 13:05 no_agent）
"""
import argparse
import datetime
import difflib
import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

from lib.config import hermes_root
from lib.cli_base import bootstrap as _bootstrap   # v2.5.0：统一编码兜底
from lib.fs import atomic_write, norm, slug_of
from lib import safeio
from lib import llm as _llm
from lib import pii
from lib import similarity
from lib.gatekit import acquire_lock_auto, release_lock, connect_db
from lib.memstore import (parse_frontmatter, dump_frontmatter, parse_candidates,
                          mark_consumed, load_detail, is_pending_active)  # PORT_SPEC §C：口径/解析单源

HERMES = hermes_root()
PENDING_DIR = HERMES / "memories" / "pending"
DETAIL_DIR = HERMES / "memories" / "detail"
QUARANTINE_DIR = HERMES / "memories" / "quarantine"
LOG_DIR = HERMES / "memories" / "gate_log"
DB = HERMES / "eco.db"
LOCK = HERMES / "scripts" / ".write_gate.lock"

SIM_NEAR = 0.80      # 近似重复（UPDATE/NOOP 候选）——difflib 口径默认值
SIM_SUSPECT = 0.50   # 疑似冲突窗口下界——difflib 口径默认值
# 注：实际取值经 similarity.gate_threshold() 按生效后端解析（换 embedding 时自动迁移，
# 见 lib/similarity.py::GATE_THRESHOLDS）；上面两常量仅为文档化的 difflib 基准。
# v2 修复（论文 8.4 缺口一，2026-10-11）：合并区间 [near, noop) 的高相似非精确更新
# 默认计数吸收（v1 行为）；置 1 后改为独立入库（ADD），新值保留在库，版本裁决交给检索与语义层。
SPLIT_UPDATES = os.environ.get("MEMORY_ECOLOGY_GATE1_SPLIT_UPDATES", "") == "1"

# L2 detail 正文上限（v2.5.6：解析收敛到 lib.config.gate_max_body）。
# 优先级：MEMORY_ECOLOGY_GATE_MAX_BODY > configs/eco.toml [gate] max_body > 200。
# 超限时正文尾追加 TRUNC_MARK —— 截断必须**可见**：审计实测 22 篇正文恰好 200 字且
# 断在词中间（`…HMAC-SHA256(secret,`），词面检索下后半句的关键词根本不存在，
# 而用户从落盘内容上看不出被截过。
from lib.config import TRUNC_MARK, gate_max_body

MAX_BODY = gate_max_body(200)
MAX_OUTPUT = 8       # 单次最多处理候选条数
# v2.5.5：DISPUTE = 「疑似同槽位冲突但规则层无权销毁」
# 新条目照常入库、旧条目只标 disputed_by 留在 detail，两条都能被检索到。
ACTIONS = ("ADD", "UPDATE", "NOOP", "CONFLICT", "DISPUTE")

PROMPT = """你是记忆整合器。以下是提取出的候选记忆（可能附带相似已有条目）。
对每条候选给出 type 和 action：
type: semantic(长期事实/偏好/属性) | episodic(一次性事件) | procedural(流程/做法) | lesson(教训)
action: ADD(新增) | UPDATE(与已有条目近似重复/补充佐证,应合并计数) | NOOP(无记忆价值) | CONFLICT(与已有条目同主题但事实相反/矛盾,旧条目应失效)
规则: 一次性临时内容即使新增也标 episodic; 近似重复优先 UPDATE; 同主题相反事实必须 CONFLICT; 琐碎无价值 NOOP。
输出 JSON 数组, 每项 {"idx": 数字, "type": "...", "action": "...", "target": "相似条目slug或空串", "note": "一句话理由", "reason": "仅 action=CONFLICT 时必填:一句话说明新旧冲突点(旧条目为什么失效,如'之前用X,现已迁移到Y')"}。只输出 JSON。

候选与相似条目:
{context}
"""




def llm_decision(context: str) -> list[dict]:
    content = _llm.complete(PROMPT.replace("{context}", context[:4000]),
                            max_tokens=800, temperature=0.1)
    m = re.search(r"\[.*\]", content, re.S)
    if not m:
        return []
    items = json.loads(m.group(0))
    return [i for i in items if isinstance(i, dict) and "idx" in i]


def rule_type(text: str) -> str:
    """LLM 不可用时的规则兜底：类型初判。"""
    if re.search(r"流程|步骤|先.{0,6}再|做法是|习惯是|方法[:：]", text):
        return "procedural"
    if re.search(r"教训|以后.*要|别再|不要.*了|犯了|踩坑", text):
        return "lesson"
    if re.search(r"(今天|昨天|上周|\d{4}-\d{2}-\d{2}|已完成|已提交|已发|办完|做完)", text):
        return "episodic"
    return "semantic"


# ── v2.5.4（F-2）：规则层的同槽位冲突检测 ───────────────────────────────
# 盲测发现（2026-10-22）：规则兜底只有 UPDATE/NOOP/ADD，没有 CONFLICT。
# LLM 一挂，事实变更就退化成两种坏结果：
#   · SPLIT_UPDATES=False（默认）：判 UPDATE → 新值被当重复计数吸收，
#     "端口改成 6432" 的 6432 一个字没进库（**静默丢事实**）
#   · SPLIT_UPDATES=True：判 ADD → 两条矛盾事实同时 active，quarantine=0
#
# 设计原则：规则层无法理解语义，所以**只在高度确定的形态上判冲突**——
# 同一"槽位"（去掉数字后的骨架相似）出现了不同的数字/版本/IP。
# 判不准就退回原逻辑，绝不在模糊情形下销毁内容（对齐「宁可重复，不可丢内容」）。
#
# 已知局限（有意保守）：中文专名变化（"值班人张伟"→"张强"）**不判冲突**，
# 因为规则层无法知道两个中文名是不是同一槽位的不同值。盲测中上一版判据
# 正是在这里把两条不同记忆判成合并销毁。

# 硬标记：数字、版本号、IPv4
_RE_NUM = re.compile(r"\d+(?:\.\d+)*")


def _skeleton(text: str) -> str:
    """去掉硬标记后的骨架，用于判断"是否同一槽位"。"""
    s = _RE_NUM.sub("§", norm(text))
    # 去掉常见的变更动词，让"是/改成/换成/升级到"落到同一骨架
    s = re.sub(r"(改成|改为|换成|变成|升级到|调整为|设为|设置为|更新为|变成|是|为|现在|已经|了)", "", s)
    s = re.sub(r"[\s\W_]+", "", s)
    return s


# ★ 2026-10-22 深夜修正（独立盲验证发现：初版误伤率 43%）
#   盲验证用 92 组判据 + 24 组端到端 A/B（改前版从 git 取原文件实跑）得出：
#     现状          TP25 / FP27  精确率 48%  误伤率 43%
#     骨架0.80→0.95  TP25 / FP16  精确率 60%  误伤率 25%
#     +包含占比0.85  TP19 / FP12  精确率 65%  误伤率 19%
#   典型误伤（改前完全正常、改后被销毁）：
#     · 订单A 100元 → 订单B 200元（不同实体，只差一个字）
#     · 30秒 → 30000毫秒（同一个值的两种写法）
#     · 主库5432+从库5433 → 主库5432（核心值没变）
#     · 温度上限 → 温度下限（第二维变化）
#   代价权衡：提门槛会漏掉一些真变更（TP 25→19），但**漏判只退回旧行为
#   （UPDATE 吸收），误判却是不可逆销毁**。按"宁可重复，不可丢内容"取严。
# ★ 门槛最终取宽松值（0.80 / 0.50），理由是**语义变了**：
#   子代理的网格搜索（建议收 0.95/0.85）是在 CONFLICT（会销毁）语义下做的，
#   那时误判不可逆，必须严防。
#   改为 DISPUTE（只标记、不销毁、两条并存）后，误判代价≈0——
#   最坏情况是"多打了一个 disputed 标记"，没有任何内容损失。
#   所以应该反过来：门槛放宽松，尽量多抓真变更（TP 16→25），
#   让 LLM 在线时再做人读复核。
_MIN_SKELETON_RATIO = 0.80      # 骨架相似度（DISPUTE 语义下可宽）
_MIN_CONTAIN_SHARE = 0.50       # 包含关系时短骨架占比（覆盖省略写法）


def _hard_tokens(text: str) -> set[str]:
    """抽取硬标记（数字/IP/版本），用于判断"值是否变了"。"""
    return set(_RE_NUM.findall(text))


def _is_generational_change(text: str, old_body: str) -> bool:
    """候选与目标的硬标记（数字/版本/IP）不同 → 事实换代，不是完全重复。

    v2.5.7（审计 Finding1）：NOOP 语义是「完全重复」，只允许在值没变时使用。
    旧实现只看整句相似度（Windows 11 → Windows 10 相似度 ≥0.95），
    把换代当重复吞掉，新值永远进不了库（不可逆）。两边都有硬标记且集合不同，
    即判换代；真正重复（标记相同，或双方无标记）仍返 False → 维持 NOOP。
    """
    new_tok, old_tok = _hard_tokens(text), _hard_tokens(old_body)
    return bool(new_tok) and bool(old_tok) and new_tok != old_tok


def escalate_noop_on_new_value(decision: dict, text: str, sims: list[dict]) -> dict:
    """NOOP/UPDATE 决策终检：否定翻转或硬标记换代 → 升级 DISPUTE（否则原样返回）。

    v2.5.7（审计 Finding1）：NOOP 语义是「完全重复」。LLM 直接判 NOOP、或
    UPDATE→NOOP 同文钳制时，若候选带来新的数字/版本/IP（Windows 11 → 10），
    说明是新旧值换代。转 DISPUTE：新值照常 ADD 入库、旧条目只标 disputed，
    两条都留在 detail（可检索、可审计）。真正重复仍走 NOOP。

    v2.5.8（红队 round3）：先复用 `escalate_polarity_flip`——否定/极性翻转不只
    NOOP 会吞，UPDATE 也会（旧条目计数 +1、正文不变），CONFLICT 还会把旧条目
    移出 detail；直接调用本函数时同样按该函数的口径升级。`_run` 里另有显式调用，
    二者幂等。
    """
    decision = escalate_polarity_flip(decision, text, sims)
    if decision.get("action") != "NOOP" or not sims:
        return decision
    old_body = (sims[0].get("detail") or {}).get("body", "")
    if not _is_generational_change(text, old_body):
        return decision
    return dict(
        decision, action="DISPUTE",
        note=decision.get("note", "") + "（硬标记不同→NOOP 升级 DISPUTE，新值入库）",
        reason=(f"事实换代：旧「{old_body[:40]}」→ 新「{text[:40]}」"
                f"（硬标记不同，不判重复）"))


def is_slot_conflict(old_body: str, new_text: str, min_skeleton_ratio: float | None = None,
                     min_contain_share: float | None = None) -> bool:
    """同一槽位出现不同硬标记值 → 判为事实冲突。

    保守四条件同时满足才返回 True：
      1. 两边都有硬标记（数字/版本/IP）
      2. 硬标记集合**不同**（值确实变了）
      3. 骨架"算同一槽位"——满足其一即可：
         a. 骨架相似度 ≥ min_skeleton_ratio（默认 0.95，从严）
         b. **短骨架完整包含于长骨架**，且长度占比 ≥ min_contain_share（默认 0.85）
            （覆盖"数据库主库端口"vs"主库端口"这类省略写法：相似度只有 0.73，
              但后者是前者的子串，语义上确实是同一槽位）
    """
    old_tok = _hard_tokens(old_body)
    new_tok = _hard_tokens(new_text)
    if not old_tok or not new_tok:
        return False                       # 没有硬标记 → 无从判断
    if old_tok == new_tok:
        return False                       # 值没变 → 不是冲突
    sk_old, sk_new = _skeleton(old_body), _skeleton(new_text)
    if not sk_old or not sk_new:
        return False
    _sr = _MIN_SKELETON_RATIO if min_skeleton_ratio is None else min_skeleton_ratio
    _cs = _MIN_CONTAIN_SHARE if min_contain_share is None else min_contain_share
    # 骨架相似度：用 difflib（与全项目同一内核，避免引入第二套口径）
    ratio = difflib.SequenceMatcher(None, sk_old, sk_new).ratio()
    if ratio >= _sr:
        return True
    # 包含关系兜底（省略写法）：短骨架完整出现在长骨架里且占比够
    short, long_ = (sk_old, sk_new) if len(sk_old) <= len(sk_new) else (sk_new, sk_old)
    if short in long_ and len(long_) > 0 and len(short) / len(long_) >= _cs:
        return True
    return False


# 否定/极性翻转判定（红队 round3，2026-10-23；B3 补全词表与 LLM 钳制）：
# 否定的新事实与肯定旧事实只差一个"不/未/没有"，difflib ratio 天然 ≥ merge 阈值，
# 旧逻辑判 UPDATE/NOOP 会把新事实静默吸收。规则层无需理解语义，只需识别
# 「去掉极性标记后完全同形」这一高确定形态 → DISPUTE（新条目入库、旧条目留痕）。
#
# 词表（长词在前，避免"没有"被剥成"有"）：
#   不 / 没 / 没有 / 未 / 无 / 别 / 非 / 禁止 / 停止
#   以及成对极性：已↔未、能↔不能、是↔不是、有↔没有、允许↔禁止、运行/开始↔停止
# 判定保守（四条件全满足）：
#   1. 两侧剥完标记的骨架都非空且 ≥2 字；
#   2. 负标记出现次数的奇偶性不同（一正一负；双重否定回到正极）；
#   3. 剥掉极性对/否定/体标记后的骨架**完全相同**；
#   4. 原句字面相似度 ≥ 0.50（防止"非常"等词被剥离后过度拉平）。
# 这样普通差异（主语不同、谓词不同）不会被打成 DISPUTE；误报最多多一个
# disputed 标记，没有内容损失（对齐"宁可重复，不可丢内容"）。
_NEG_MARKERS = ("没有", "禁止", "停止", "无法", "无需", "未曾", "不曾", "不再",
                "不", "没", "未", "无", "别", "非", "勿", "莫")
_NEG_RE = re.compile("|".join(sorted(map(re.escape, _NEG_MARKERS), key=len, reverse=True)))
# 体标记（已/曾经/尚）与否定标记常成对出现（已备份 vs 未备份），比较前两边都剥掉
_ASPECT_RE = re.compile(r"(?:已经|曾经|已|曾|尚)")

# 极性对（正极, 负极）：比较前两边都剥掉各自成员，让"允许写入/禁止写入"
# 落到同一骨架；是否翻转由负标记奇偶性决定（不靠词表硬编码"反义"）。
_POLARITY_PAIRS = (
    ("有", "没有"),
    ("允许", "禁止"),
    ("运行", "停止"),
    ("开始", "停止"),
    ("启用", "禁用"),
    ("开启", "关闭"),
    ("能", "不能"),
    ("可以", "不能"),
    ("可以", "无法"),
    ("需要", "无需"),
    ("是", "不是"),
)
_PAIR_NEG_RE = re.compile("|".join(sorted({n for _, n in _POLARITY_PAIRS},
                                          key=len, reverse=True)))
_PAIR_POS_RE = re.compile("|".join(sorted({p for p, _ in _POLARITY_PAIRS},
                                          key=len, reverse=True)))

# 含否定字但**不是否定**的常用词：先换占位符，避免"非常/别人/无线/未来"误判。
# 只列高频词，保持判定保守；未列出的词仍可能误报，但代价只是多一个 disputed 标记。
_NEG_GUARDS = ("非常", "特别", "别的", "别人", "别处", "别致",
               "无线", "无数", "无比", "无论", "无疑", "无妨", "无奈", "无所谓",
               "差不多", "对不起", "了不起", "巴不得", "未来")
_GUARD_RE = re.compile("|".join(sorted(map(re.escape, _NEG_GUARDS), key=len, reverse=True)))
_GUARD_TOKEN = "\u25c7"  # ◇：不会与正文中文冲突；两边一致替换，不引入差异
_MIN_NEG_RATIO = 0.50     # 与 DISPUTE 的 suspect 门槛同口径


def _guard_negation(s: str) -> str:
    """norm 后把"含否定字但非否定"的常用词换成占位符。"""
    return _GUARD_RE.sub(_GUARD_TOKEN, norm(s))


def _negation_parity(s: str) -> int:
    """负标记出现次数的奇偶性（双重否定回到正极）。"""
    return len(_NEG_RE.findall(s)) % 2


def _without_polarity(s: str) -> str:
    """剥掉极性对与否定/体标记后的骨架（norm 已去空白/标点）。"""
    s = _PAIR_NEG_RE.sub("", s)
    s = _PAIR_POS_RE.sub("", s)
    s = _ASPECT_RE.sub("", s)
    s = _NEG_RE.sub("", s)
    return s


def is_negation_flip(old_body: str, new_text: str) -> bool:
    """否定/极性翻转：骨架相同但否定极性相反 → True（保守四条件见上）。"""
    if not old_body or not new_text:
        return False
    old_g, new_g = _guard_negation(old_body), _guard_negation(new_text)
    if not old_g or not new_g:
        return False
    if _negation_parity(old_g) == _negation_parity(new_g):
        return False
    old_c, new_c = _without_polarity(old_g), _without_polarity(new_g)
    if len(old_c) < 2 or len(new_c) < 2 or old_c != new_c:
        return False
    return difflib.SequenceMatcher(None, norm(old_body), norm(new_text)).ratio() >= _MIN_NEG_RATIO


# LLM 在线时可能把否定翻转判成 NOOP/UPDATE/CONFLICT（新事实被吸收 / 旧条目被
# supersede 移出 detail）。这三种动作都与 DISPUTE 语义冲突，在动作分派前统一钳制。
_POLARITY_CLAMP_ACTIONS = ("NOOP", "UPDATE", "CONFLICT")


def escalate_polarity_flip(decision: dict, text: str, sims: list[dict]) -> dict:
    """LLM/规则给出的 NOOP/UPDATE/CONFLICT 遇到否定翻转 → 升级 DISPUTE。

    · NOOP    ：新事实被当重复丢弃（数据丢失）；
    · UPDATE  ：旧条目 occurrences+1、正文不变，新事实丢失（数据丢失）；
    · CONFLICT：旧条目移 quarantine（检索不到），违反"只标记不销毁"。
    DISPUTE 路径：新值照常 ADD 入库，旧条目只打 disputed_by，两条都留在 detail。
    """
    if decision.get("action") not in _POLARITY_CLAMP_ACTIONS or not sims:
        return decision
    old_body = (sims[0].get("detail") or {}).get("body", "")
    if not is_negation_flip(old_body, text):
        return decision
    return dict(
        decision, action="DISPUTE",
        note=decision.get("note", "") + "（否定翻转→升级 DISPUTE，新值入库）",
        reason=(f"否定/极性翻转：旧「{old_body[:40]}」→ 新「{text[:40]}」"
                f"（否定极性相反，不判重复、不销毁旧条目）"))


def rule_fallback_action(text: str, sims: list[dict],
                         sim_near: float | None = None,
                         sim_noop: float | None = None,
                         sim_conflict: float | None = None) -> str:
    """规则兜底的动作用判定（可测的纯函数）。

    v2.5.4 起把动作判定从 main 里抽出来，并补上 CONFLICT 分支。
    返回 "ADD" / "UPDATE" / "NOOP" / "CONFLICT"。

    判定顺序（冲突优先，避免被 UPDATE 吸收）：
      1. 无相似命中            → ADD
      2. 同槽位不同值          → CONFLICT（新增，防静默丢事实）
      3. 相似度 ≥ noop 阈值    → NOOP（完全重复）
      4. 相似度 ≥ near 阈值    → UPDATE；若 SPLIT_UPDATES 且正文不同则 ADD
      5. 其他                  → ADD

    ★ 关于 CONFLICT 的相似度门槛（2026-10-22 深夜，独立盲验证修正）：
      初版先试 near(0.80) 发现端到端不触发，于是降到 suspect(0.50)。
      **盲验证证明这个降级是错的**：
        · 它以为的原因是"cN：前缀把 ratio 压到 0.74"，实测前缀后是 0.7586；
          真正原因是 `_skeleton` 的动词剥离把差异消光、骨架 ratio 变成 1.0。
        · 降门槛的代价：门槛扫描 0.80→TP16/FP16，0.50→TP25/FP27。
          多抓 9 个真变更，却多销毁 11 组本该并存的记忆。
      现在改回 **near(0.80)**，并配合收紧骨架判据（0.95 / 0.85）。
      原则：漏判只是退回旧行为（UPDATE 吸收，可补救），误判是不可逆销毁。

    ★★ 结构性修正（2026-10-22 深夜，第二轮盲验证驱动）★★
      盲验证用 92 组标注数据做门槛网格搜索，结论是**无论怎么调门槛，
      误伤率最低只到 19%**（精确率 66% / 召回 77%）。

      这说明问题不在参数，而在**判据思路**：
      规则层（difflib 字面相似 + 数字抽取）没有语义能力，
      **没资格销毁内容**。典型误伤（改前完全正常）：
        · 订单A 100元 → 订单B 200元（不同实体，只差一字）
        · 主库5432+从库5433 → 主库5432（核心值没变）
        · 温度上限 → 温度下限（第二维变化）
        · 30秒 → 30000毫秒（同一个值的两种写法）

      且实测确认：**「进 quarantine」≈「从记忆里消失」**——
      `memory_query` 不扫 quarantine，`inject_gate` 第一条规则就是
      `quarantined → drop`。所以误判 CONFLICT 等于真丢记忆。

      因此改为 **DISPUTE（标记争议）**：
        · 新条目照常 ADD 入库（**保证新值一定不丢** ← 原目标）
        · 旧条目不 supersede、不移 quarantine，只在 fm 里标
          `disputed_by: <新slug>` / `disputed_at` / `disputed_reason`
        · 两条都留在 detail，都还能被检索到
        · 写 gate_log 待 LLM 在线时复核
      这样误伤归零（没有内容被销毁），核心目标仍满足。
      真正需要"旧条目失效"的语义裁决，交给 LLM 在线时的 CONFLICT 路径。

    返回值："ADD" / "UPDATE" / "NOOP" / "CONFLICT" / "DISPUTE"
    """
    if not sims:
        return "ADD"
    near = sim_near if sim_near is not None else similarity.gate_threshold("merge", SIM_NEAR)
    noop = sim_noop if sim_noop is not None else similarity.gate_threshold("noop", 0.95)
    # ★ DISPUTE 用 suspect 级门槛（0.50），而**不是** near（0.80）。
    #   这正是结构性修正带来的好处：
    #     · CONFLICT 会销毁内容 → 必须严防误判 → 门槛要高（网格搜索证明 0.80 才勉强）
    #     · DISPUTE 只标记不销毁 → 误判代价≈0（两条并存，用户仍能检索到）→ 门槛可放宽
    #   放宽后能多抓真变更（TP 16→25），而"多标的"只是多一个 disputed 标记，
    #   没有内容损失。这也解释了为什么之前两版都不对：
    #   第一版用 near 卡住（抓不到），第二版降门槛但走 CONFLICT（销毁太多）。
    conflict_th = (sim_conflict if sim_conflict is not None
                   else similarity.gate_threshold("suspect", SIM_SUSPECT))

    top = sims[0]
    ratio = top.get("ratio", 0.0)
    old_body = (top.get("detail") or {}).get("body", "")

    # ① 疑似同槽位不同值 / 否定翻转 → DISPUTE（不销毁，只标记 + 新值照常入库）
    if ratio >= conflict_th and old_body and (
            is_slot_conflict(old_body, text) or is_negation_flip(old_body, text)):
        return "DISPUTE"

    if ratio >= near:
        if ratio >= noop:
            # v2.5.7（审计 Finding1）：完全重复才 NOOP；硬标记变了就是换代，
            # 转 DISPUTE（新增入库 + 旧条目留痕），不再静默丢弃新值。
            if old_body and _is_generational_change(text, old_body):
                return "DISPUTE"
            return "NOOP"
        action = "UPDATE"
        if SPLIT_UPDATES and norm(text) != norm(old_body):
            action = "ADD"          # v2：非精确更新独立入库，不吸收新值
        return action
    return "ADD"




def find_similar(text: str, detail: list[dict], threshold: float) -> list[dict]:
    """返回相似度 ≥ threshold 的 detail 条目（按相似度降序）。
    批 5：相似度内核走 lib/similarity 统一层（默认 difflib 行为锁定；
    MEMORY_ECOLOGY_SIM_BACKEND=ngram/embedding 可切换——切换前先跑 eco_sim_eval）。"""
    hits = []
    for d in detail:
        ratio = similarity.ratio(text, d["body"])
        if ratio >= threshold:
            hits.append({"detail": d, "ratio": ratio})
    hits.sort(key=lambda h: h["ratio"], reverse=True)
    return hits




def slug_for(text: str) -> str:
    """PII 安全 slug：先 mask 再生成文件名（同一段文本稳定、无随机）。

    为什么必须先 mask：slug 会被备份、FTS 索引与 git 历史复制；手机号/身份证原文
    一旦进文件名就无法靠"删正文"撤回（隐私判据 PF1）。
    """
    return slug_of(pii.mask(text))


def origin_from_sidecar(pending_dir: Path, fname: str) -> str:
    """候选来源溯源（v3.0.0a1，审计 P0-3）：读 eco_extract 落的
    `<候选文件>.md.sessions.json` sidecar，返回逗号分隔的 session_id 串。

    · 文件缺失/损坏/字段异常 → 空串（老候选没有 sidecar；fail-open，行为不变）
    · 保序去重；>8 个会话截断为前 8 + `|more:N`（窗口被多会话稀释时仍可读）
    · 这是从「捕获」到「入库」唯一能带出会话关联的通道：候选正文是 LLM 提炼，
      落库后无法反查 state.db——必须在门① 写入时随 fm 落 frontmatter。
    """
    p = pending_dir / f"{fname}.sessions.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        ids = data.get("session_ids")
        if not isinstance(ids, list):
            return ""
        seen: list[str] = []
        for s in ids:
            s = str(s or "").strip()
            if s and s not in seen:
                seen.append(s)
    except Exception:
        return ""
    if not seen:
        return ""
    if len(seen) > 8:
        return ",".join(seen[:8]) + f"|more:{len(seen) - 8}"
    return ",".join(seen)


def add_entry(detail_dir: Path, text: str, fm_extra: dict) -> Path:
    now = datetime.date.today().isoformat()
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    fm = {
        "type": fm_extra.get("type", "semantic"),
        "status": "active",
        "occurrences": "1",
        "session_count": "1",
        "first_seen": now,
        "last_seen": now,
        "last_hit": now,  # PORT_SPEC §6 last_hit 最小件：创建即首 hit（后续由 inject 遥测更新）
        "valid_time": fm_extra.get("valid_time") or now,
        "transaction_time": ts,
        "last_verified": now,
        "origin_session_id": fm_extra.get("origin_session_id", ""),
        "superseded_by": "",
        # v2.3.0 证据链：CONFLICT 换代时指向被取代的旧条目 slug（与旧条目 superseded_by 互指）；
        # 常规 ADD 时回退记 origin_session_id。逗号分隔，条目 slug 字符集内无逗号，可安全拆分。
        "evidence": str(fm_extra.get("evidence") or fm_extra.get("origin_session_id", "") or ""),
        # v3.0.1（联邦化数据地基，决策见 PLAN-v3.0.1 §1.4）：scope = 候选来源文件名
        # （指纹链已验证 127/127 可归因的硬证据；桶映射留给消费层，不提前承诺）。
        # 无来源（extrude 直写等）保持空串——诚实缺省，selfcheck A5 按生成日期分段判。
        "scope": str(fm_extra.get("scope", "") or ""),
        "distilled_at": "",
        # v2.5.8（隐私 R1/PF1）：机器可读敏感度标记。无 PII 时为空串（保持字段可枚举，
        # 下游可稳定读取而无需区分"缺字段"与"不敏感"）。
        "sensitivity": pii.sensitivity_of(text),
    }
    path = safeio.safe_entry_path(detail_dir, slug_for(text))
    if path.exists():
        # 已存在（如 pending 重建致 fingerprint 变化）：不覆盖，走 UPDATE 语义。
        # P1：读取/写入失败直接抛给上层记失败（候选不消费、下轮重试）——
        # 旧版 except OSError: pass 后直接 return，账本记了从未发生的 ADD（假成功）。
        old_fm, old_body = parse_frontmatter(path.read_text(encoding="utf-8"))
        try:
            old_fm["occurrences"] = str(int(old_fm.get("occurrences", "1")) + 1)
        except ValueError:
            old_fm["occurrences"] = "2"
        old_fm["last_seen"] = now
        old_fm["last_verified"] = now
        safeio.write_entry(path, dump_frontmatter(old_fm, old_body), kind="detail")
    else:
        body = text if len(text) <= MAX_BODY else text[:MAX_BODY] + TRUNC_MARK
        safeio.write_entry(path, dump_frontmatter(fm, body), kind="detail")
    return path


def _mark_disputed(d: dict, new_slug: str, reason: str = "") -> None:
    """给旧条目打"争议"标记，但**不销毁**（v2.5.5 DISPUTE 路径）。

    与 supersede_entry 的关键区别：
      · status 保持 active（仍可被检索、仍参与注入）
      · 不移动文件、不进 quarantine
      · 只写三个审计字段：disputed_by / disputed_at / disputed_reason

    为什么这样做（2026-10-22 第二轮盲验证）：
      实测「进 quarantine」≈「从记忆里消失」——memory_query 不扫 quarantine，
      inject_gate 第一条规则就是 quarantined→drop。
      而规则层的判据误伤率最低只能到 19%（92 组网格搜索），
      没有语义能力却要销毁内容，代价是真实记忆不可逆丢失。
      故改为"标记待复核"：新值生效、旧值可查、等人/LLM 裁决。
    """
    today = datetime.date.today().isoformat()
    try:
        fm, body = parse_frontmatter(d["path"].read_text(encoding="utf-8"))
    except OSError:
        fm, body = dict(d["fm"]), d["body"]
    fm["disputed_by"] = new_slug
    fm["disputed_at"] = today
    fm["disputed_reason"] = (reason or "")[:200]
    # 明确不碰 status：保持 active，两条并存可检索
    safeio.write_entry(d["path"], dump_frontmatter(fm, body), kind="detail")


def update_entry(d: dict) -> None:
    """近似重复：occurrences+1；跨天出现则 session_count+1；刷新 last_verified（防活跃记忆被误归档）。
    P1：同轮多条候选命中同一目标时，逐次从磁盘重读最新值再增补——入口快照会把
    两次 +1 都写在同一基线上，实际只涨 1（丢失更新）。"""
    today = datetime.date.today().isoformat()
    try:
        fm, body = parse_frontmatter(d["path"].read_text(encoding="utf-8"))
    except OSError:
        fm, body = dict(d["fm"]), d["body"]
    try:
        fm["occurrences"] = str(int(fm.get("occurrences", "1")) + 1)
    except ValueError:
        fm["occurrences"] = "2"
    if str(fm.get("last_seen", ""))[:10] != today:
        try:
            fm["session_count"] = str(int(fm.get("session_count", "1")) + 1)
        except ValueError:
            fm["session_count"] = "2"
    fm["last_seen"] = today
    fm["last_verified"] = today  # P1-1：活跃记忆持续复核，不被 eco_review 误归档
    safeio.write_entry(d["path"], dump_frontmatter(fm, body), kind="detail")


def supersede_entry(d: dict, new_slug: str, quarantine_dir: Path, reason: str = "") -> Path:
    """矛盾旧条目：标 superseded 后移入 quarantine（保留可回滚）。

    v2.3.0：失效带理由（deja-vu「rejected 随命中返回」同思想）——
    superseded_reason = 一句话冲突点（检索命中失效条目时原样返回），
    superseded_at = 失效日期（隔离区由此成为可查历史时间轴，而非垃圾箱）。"""
    fm = dict(d["fm"])
    fm["status"] = "superseded"
    fm["superseded_by"] = new_slug
    fm["superseded_at"] = datetime.date.today().isoformat()
    fm["superseded_reason"] = (reason or "").strip()
    fm["distilled_at"] = ""  # v2.3.0 证据链：源失效 → 蒸馏状态位清空（复活后可重蒸馏）
    date_dir = quarantine_dir / datetime.date.today().isoformat()
    date_dir.mkdir(parents=True, exist_ok=True)
    target = date_dir / d["path"].name
    if target.exists():
        target = date_dir / f"{d['path'].stem}-{hashlib.md5(str(target).encode()).hexdigest()[:6]}.md"
    safeio.write_entry(d["path"], dump_frontmatter(fm, d["body"]), kind="detail")
    os.replace(d["path"], target)
    try:
        if d["path"].exists():
            d["path"].unlink()
    except OSError as e:
        print(f"⚠️ 原文件删除失败（quarantine 副本已存在）: {d['path'].name}: {e}")
    return target


def _fingerprint_done(conn: sqlite3.Connection, key: str) -> bool:
    fp = hashlib.md5(key.encode("utf-8")).hexdigest()
    try:
        return conn.execute("SELECT 1 FROM gate_log WHERE fingerprint=?", (fp,)).fetchone() is not None
    except sqlite3.OperationalError:
        return False  # dry-run 无表时视为未处理


def log_gate(conn: sqlite3.Connection, fingerprint: str, action: str, target: str, note: str, dry: bool = False) -> None:
    if dry:
        return
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    # 派生副本（gate_log）不是真源：note 里可能带候选正文/冲突理由，落库前统一脱敏。
    conn.execute(
        "INSERT OR IGNORE INTO gate_log(ts, fingerprint, action, target, note) VALUES(?,?,?,?,?)",
        (ts, fingerprint, action, target, pii.mask(note)),
    )


def _match_target(decision: dict, sims: list) -> dict | None:
    """优先采用 LLM 指定的 target slug（P1-2），匹配不到才回退最相似条目。"""
    t = decision.get("target") or ""
    if t:
        for s in sims:
            if s["detail"]["name"] == t:
                return s["detail"]
    return sims[0]["detail"] if sims else None


def consumable_files(by_file: dict[str, list[dict]], terminal: set[str],
                     pending_dir: Path) -> set[str]:
    """P0-1：可消费文件判定。

    一个文件可消费 ⇔ 它解析出的**全部**候选都已终态（历史指纹已存在，或本轮成功处理）；
    无候选的文件照旧消费（与原行为一致）。被 MAX_OUTPUT 截断或处理失败的候选
    所在文件留在 pending，下轮续处理——不再被尾部统一改名静默吞掉。
    """
    out: set[str] = set()
    for fname, cs in by_file.items():
        if all(f"{c['file']}|{c['raw']}" in terminal for c in cs):
            out.add(fname)
    for p in pending_dir.glob("*.md"):
        if is_pending_active(p.name) and p.name not in by_file:
            out.add(p.name)
    return out


def main(argv=None) -> int:
    _bootstrap()   # v2.5.0：编码兜底先于一切
    from lib.cli_base import banner as _banner   # v2.5.6：部署可观测（哈希+版本+数据根）
    _banner("write_gate", file=__file__)
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--pending", type=Path, default=PENDING_DIR)
    ap.add_argument("--detail", type=Path, default=DETAIL_DIR)
    ap.add_argument("--db", type=Path, default=DB)
    ap.add_argument("--logdir", type=Path, default=LOG_DIR)
    args = ap.parse_args(argv)

    locked, how = acquire_lock_auto(LOCK)
    if not locked:
        print("⚠️ 已有 write_gate 实例在运行，本轮跳过")
        return 0
    if how == "stale-cleared":
        print("🧹 清理了失效残留锁（持有进程已不存在），本轮正常执行")
    try:
        return _run(args)
    finally:
        release_lock(LOCK)


def _run(args) -> int:
    if not args.dry_run:
        args.detail.mkdir(parents=True, exist_ok=True)
        args.logdir.mkdir(parents=True, exist_ok=True)

    # dry-run 且库不存在：connect_db 用 :memory: 顶替——不建库不留 0 字节文件；
    # 库已存在时照常连接读真指纹（dry-run 的去重报告行为与修复前完全一致）
    conn = connect_db(args.db, dry_run=args.dry_run)
    if not args.dry_run:
        conn.execute("""CREATE TABLE IF NOT EXISTS gate_log(
            ts TEXT, fingerprint TEXT PRIMARY KEY, action TEXT, target TEXT, note TEXT)""")

    cands_all = parse_candidates(args.pending)
    # 按文件分组（P0-1 消费判定用）；候选键 = "file|raw"（与指纹口径一致）
    by_file: dict[str, list[dict]] = {}
    for c in cands_all:
        by_file.setdefault(c["file"], []).append(c)
    terminal: set[str] = set()  # 终态 = 历史指纹已存在 或 本轮成功处理
    fresh = []
    for c in cands_all:
        key = f"{c['file']}|{c['raw']}"
        if _fingerprint_done(conn, key):
            terminal.add(key)
        else:
            fresh.append(c)
    # P1-9：先过滤已处理 fingerprint 再取前 MAX_OUTPUT（防候选饥饿）；
    # P0-1：被截断的候选其所在文件本轮不消费，留待下轮
    cands = fresh[:MAX_OUTPUT]
    detail = load_detail(args.detail)
    today = datetime.date.today().isoformat()
    report = []
    failures = 0
    # 阈值按生效后端解析一次（换 embedding 时自动迁移；缺模型回退 ngram 也自动跟随）
    sim_near = similarity.gate_threshold("merge", SIM_NEAR)
    sim_noop = similarity.gate_threshold("noop", 0.95)

    for c in cands:
        key = f"{c['file']}|{c['raw']}"
        fp = hashlib.md5(key.encode("utf-8")).hexdigest()
        text = c["text"]
        # v3.0.0a1（审计 P0-3）：本候选的来源会话（sidecar 缺失 → 空串，行为不变）。
        # 逐候选读一次（文件级粒度；同文件候选共享一个窗口，O(1) 小文件读，可接受）
        origin = origin_from_sidecar(args.pending, c["file"])
        if not norm(text):
            report.append(f"SKIP     {text[:40]}（空/纯标点，无信息量）")
            log_gate(conn, fp, "NOOP", "", "空内容", args.dry_run)
            terminal.add(key)
            continue
        sims = find_similar(text, detail, similarity.gate_threshold("suspect", SIM_SUSPECT))
        decision = None
        if sims:
            # 有相似命中：调 LLM 决策（上下文只带当前候选 + 相似条目，省 token）
            ctx_lines = [f"候选 {c['idx']}: [{c['ctype']}] {text}"]
            ctx_lines.append("相似条目:")
            for i, s in enumerate(sims[:3]):
                ctx_lines.append(f"  {i}: {s['detail']['name']} (ratio {s['ratio']:.2f}) {s['detail']['body'][:80]}")
            try:
                decisions = llm_decision("\n".join(ctx_lines))
                for dd in decisions:
                    if dd.get("idx") == c["idx"]:
                        decision = dd
                        break
            except Exception as e:
                print(f"⚠️ LLM 决策失败: {e}（回退规则判定）")
        # 规则兜底
        if decision is None:
            ctype = rule_type(text)
            # v2.5.4（F-2）：动作判定抽成 rule_fallback_action，并补上 CONFLICT 分支。
            # 原实现只有 UPDATE/NOOP/ADD，LLM 一挂时事实变更会被 UPDATE 吸收
            # （新值不落库）或产出两条同时 active 的矛盾事实。
            action = rule_fallback_action(text, sims, sim_near, sim_noop)
            note_tag = ""
            if action == "DISPUTE":
                note_tag = "（规则判疑似冲突→标记争议，不销毁）"
            elif action == "CONFLICT":
                note_tag = "（规则判同槽位冲突）"
            elif action == "ADD" and sims:
                note_tag = "（v2 拆分入库）"
            decision = {"type": ctype, "action": action,
                        "target": sims[0]["detail"]["name"] if sims else "",
                        "note": "规则兜底" + note_tag,
                        # CONFLICT 需要 reason，规则层给一个人可读的说明
                        "reason": (f"规则判为疑似同槽位事实变更：旧「{sims[0]['detail']['body'][:40]}」"
                                   f"→ 新「{text[:40]}」（仅标记争议，未销毁旧条目）"
                                   if action == "DISPUTE" and sims else "")}

        action = decision.get("action", "ADD")
        if action not in ACTIONS:
            # LLM 输出了非法动作：回退规则判定（不写 NOOP 指纹，不静默丢弃）
            ctype = rule_type(text)
            if sims and sims[0]["ratio"] >= sim_near:
                action = "NOOP" if sims[0]["ratio"] >= sim_noop else "UPDATE"
                if SPLIT_UPDATES and action == "UPDATE" and norm(text) != norm(sims[0]["detail"]["body"]):
                    action = "ADD"  # v2：同主路径
            else:
                action = "ADD"
            decision = {"type": ctype, "action": action,
                        "target": sims[0]["detail"]["name"] if sims else "", "note": "非法action回退规则"}
        # LLM 在线时也可能对完全同文（≥noop）判 UPDATE——与规则兜底同口径钳制为 NOOP，
        # 防 occurrences 被同一文本反复膨胀（P1）
        if decision.get("action") == "UPDATE" and sims and sims[0]["ratio"] >= sim_noop:
            decision = dict(decision, action="NOOP",
                            note=decision.get("note", "") + f"（≥{sim_noop:.2f} 同文钳制为 NOOP）")
        # v2.5.8（红队 round3）：否定/极性翻转不得被 NOOP/UPDATE/CONFLICT 吸收。
        # 先钳制极性（LLM 判 NOOP/UPDATE/CONFLICT → DISPUTE），再做 NOOP 硬标记终检。
        _before_action = decision.get("action")
        decision = escalate_polarity_flip(decision, text, sims)
        decision = escalate_noop_on_new_value(decision, text, sims)
        if decision.get("action") != _before_action:
            action = decision["action"]
        ctype = decision.get("type", rule_type(text))
        note = decision.get("note", "")
        target = ""
        try:
            if action == "ADD":
                if not args.dry_run:
                    p = add_entry(args.detail, text, {"type": ctype, "origin_session_id": origin,
                                                          "scope": c["file"]})
                    target = p.stem
                else:
                    target = f"(new:{slug_for(text)})"
                report.append(f"ADD      {text[:40]} → {target} [type={ctype}] {note}")
                log_gate(conn, fp, "ADD", target, note, args.dry_run)
            elif action == "UPDATE":
                hit = _match_target(decision, sims)
                if hit:
                    target = hit["name"]
                    if not args.dry_run:
                        update_entry(hit)
                    report.append(f"UPDATE   {text[:40]} → {target} (occurrences+1) {note}")
                    log_gate(conn, fp, "UPDATE", target, note, args.dry_run)
                else:
                    report.append(f"UPDATE   {text[:40]} → 无目标，按 ADD 处理")
                    if not args.dry_run:
                        p = add_entry(args.detail, text, {"type": ctype, "origin_session_id": origin,
                                                          "scope": c["file"]})
                        log_gate(conn, fp, "ADD", p.stem, note, args.dry_run)
            elif action == "CONFLICT":
                hit = _match_target(decision, sims)
                if hit:
                    target = hit["name"]
                    # v2.3.0：失效理由 = LLM 的 reason（缺失回退 note；规则兜底路径不产生 CONFLICT）
                    conflict_reason = (decision.get("reason") or decision.get("note") or "").strip()
                    if not args.dry_run:
                        # R10（v2.2.0 review）：先失效旧条目再写新条目；P1 补强——
                        # 写新失败时把旧条目从 quarantine 原位还原（fm 快照是 supersede 前的），
                        # 候选下轮可重试，不再留下「旧条目已废 + 新条目未落盘」且随 .done 丢失的半程态
                        qtarget = supersede_entry(hit, slug_for(text), QUARANTINE_DIR, reason=conflict_reason)
                        try:
                            # v2.3.0 证据链：新条目 evidence 指向被取代旧条目（与旧条目 superseded_by 互指，
                            # 从任一侧可追溯完整换代链：Hindsight「矛盾保留历史」/腾讯 L0 回落同思想）
                            # v3.0.0a1：origin_session_id 同步带上捕获侧溯源（evidence 保持指向旧条目）
                            p = add_entry(args.detail, text, {"type": ctype, "evidence": target,
                                                              "origin_session_id": origin,
                                                              "scope": c["file"]})
                        except Exception:
                            try:
                                os.replace(qtarget, hit["path"])
                                safeio.write_entry(hit["path"], dump_frontmatter(hit["fm"], hit["body"]),
                                                   kind="detail")
                            except Exception as e2:
                                print(f"⚠️ 旧条目还原失败（quarantine 副本仍在，可人工恢复）: {e2}")
                            raise
                        report.append(f"CONFLICT {text[:40]} → 新条目 {p.stem}，旧条目 {target} 已 superseded→quarantine "
                                      f"失效理由: {conflict_reason} {note}")
                        log_gate(conn, fp, "CONFLICT", target, f"new={p.stem} reason={conflict_reason} {note}", args.dry_run)
                    else:
                        report.append(f"CONFLICT {text[:40]} → (dry-run) 旧条目 {target} 将失效"
                                      f"（理由: {conflict_reason}）")
                        log_gate(conn, fp, "CONFLICT", target, note, args.dry_run)
                else:
                    report.append(f"CONFLICT {text[:40]} → 无目标条目，按 ADD 处理")
                    if not args.dry_run:
                        p = add_entry(args.detail, text, {"type": ctype, "origin_session_id": origin,
                                                          "scope": c["file"]})
                        log_gate(conn, fp, "ADD", p.stem, note, args.dry_run)
            elif action == "DISPUTE":
                # ★ v2.5.5（第二轮盲验证驱动）：规则层无权销毁内容。
                #   新条目照常 ADD 入库（保证新值一定不丢）；
                #   旧条目**不 supersede、不移 quarantine**，只打争议标记，
                #   两条都留在 detail 且都能被检索到。
                #   实测依据：「进 quarantine」≈「从记忆里消失」——
                #   memory_query 不扫 quarantine，inject_gate 第一条就是 quarantined→drop。
                hit = _match_target(decision, sims)
                dispute_reason = (decision.get("reason") or decision.get("note") or "").strip()
                if not args.dry_run:
                    p = add_entry(args.detail, text, {"type": ctype, "origin_session_id": origin,
                                                          "scope": c["file"]})
                    target = p.stem
                    if hit:
                        try:
                            _mark_disputed(hit, p.stem, dispute_reason)
                        except Exception as e:
                            print(f"⚠️ 争议标记写入失败（不影响入库）: {e}")
                    report.append(f"DISPUTE  {text[:40]} → 新条目 {target} 已入库；"
                                  f"旧条目 {(hit or {}).get('name', '(无)')} 仅标记 disputed（未销毁）"
                                  f" 理由: {dispute_reason} {note}")
                    log_gate(conn, fp, "DISPUTE",
                             (hit or {}).get("name", ""),
                             f"new={target} reason={dispute_reason} {note}", args.dry_run)
                else:
                    report.append(f"DISPUTE  {text[:40]} → (dry-run) 将新增条目并标记旧条目争议"
                                  f"（理由: {dispute_reason}）")
                    log_gate(conn, fp, "DISPUTE",
                             (hit or {}).get("name", ""), note, args.dry_run)
            else:  # NOOP
                target = decision.get("target", "")
                report.append(f"NOOP     {text[:40]} {note}")
                log_gate(conn, fp, "NOOP", target, note, args.dry_run)
        except Exception as e:
            failures += 1
            report.append(f"⚠️ 处理失败 {text[:40]}: {e}")
            print(f"⚠️ {text[:40]}: {e}")
        else:
            terminal.add(key)  # P0-1：成功处理才终态；失败候选留待下轮重试
        if not args.dry_run:
            conn.commit()  # P1-4：每个动作后立即落库，崩溃窗口不破坏幂等

    # 派生副本（gate_log/*.md 按轮清单 + 当日汇总）只是审计投影，不是真源：
    # 报告行里含候选正文前缀/冲突理由，写盘与打印前统一 mask（PF4）。
    report = [pii.mask(r) for r in report]

    # v0.3 对齐门③④：零新候选轮次写 idle 心跳（"门在跑"有据可查）；
    # fingerprint 按日哈希，gate_log 主键不跨日冲突
    if not args.dry_run and not fresh:
        log_gate(conn, hashlib.md5(f"idle|{today}".encode("utf-8")).hexdigest(),
                 "idle", "", "heartbeat 零新候选（门在跑）")
        conn.commit()

    conn.commit()
    conn.close()

    # Q22（2026-09-06）+ P0-1（2026-10-04）：只消费「全部候选已达终态」的文件——
    # MAX_OUTPUT 截断的、处理失败的历史上被尾部统一改名 .done.md 静默吞掉，现在留待下轮
    if not args.dry_run:
        consume = consumable_files(by_file, terminal, args.pending)
        deferred = sorted(set(by_file) - consume)
        if deferred:
            print(f"⏸ {len(deferred)} 个文件存在未终态候选，留待下轮: "
                  f"{', '.join(deferred[:5])}{'…' if len(deferred) > 5 else ''}")
        mark_consumed(args.pending, only=consume)

    if args.dry_run:
        print("== DRY-RUN（未修改任何文件）==")
    else:
        # ★ v2.5.2 修复（盲实测驱动）：原实现是 gate-<日期>.md + atomic_write，
        #   每天只保留**最后一轮**——再跑一轮，上一轮记录就消失；跑一次空轮，
        #   整个 md 被清成只剩标题 30 字节。而 SQLite 里历史仍在。
        #   结果："完整可读的审计链只活在 eco.db，md 不能当审计依据"。
        #   改为：每轮一个文件（互不覆盖）+ 当日汇总追加（保留全量，供人翻阅）。
        #
        # ★★ 二次修复（2026-10-22 独立盲验证发现两个口子）：
        #   口子1（严重）：汇总读取失败时原代码 `except OSError: prev = ""`
        #     然后直接拿空串覆盖 —— **一次瞬时读失败就把当日历史全清**，
        #     原病症换个触发条件又回来。改为：读失败就**放弃更新汇总**，
        #     但按轮文件已独立落盘，审计链不会丢。
        #   口子2：文件名只有秒精度（%H%M%S），单轮耗时约 180-220ms，
        #     连跑（cron 重试/手动补跑/批量回灌）必然同秒碰撞，后写的覆盖先写的。
        #     实测无间隔连跑 15 轮只落 3 个文件。改为加毫秒 + 碰撞自增序号。
        now = datetime.datetime.now()
        stamp = now.strftime("%H%M%S")
        ms = f"{now.microsecond // 1000:03d}"
        base_name = f"gate-{today}-{stamp}-{ms}"
        round_out = args.logdir / f"{base_name}.md"
        seq = 1
        while round_out.exists() and seq < 100:      # 同一毫秒内再碰撞则自增
            seq += 1
            round_out = args.logdir / f"{base_name}-{seq}.md"
        lines = [f"# 写入整合门 {today} {stamp}.{ms}", ""] + [f"- {r}" for r in report]
        atomic_write(round_out, "\n".join(lines) + "\n")

        daily_out = args.logdir / f"gate-{today}.md"
        agg_ok = True
        if daily_out.exists():
            try:
                prev = daily_out.read_text(encoding="utf-8")
            except OSError as _e:
                # ★ 绝不用空串覆盖既有汇总——那会把当日历史整条抹掉。
                #   本轮按轮文件已落盘，审计链完整；汇总留待下轮自然补齐。
                prev = None
                agg_ok = False
                print(f"⚠️ 当日汇总读取失败（{_e}），本轮不更新汇总；"
                      f"按轮日志已保存 → {round_out.name}")
        else:
            prev = f"# 写入整合门 {today}（当日汇总）\n"
        if agg_ok:
            if not (prev or "").strip():
                prev = f"# 写入整合门 {today}（当日汇总）\n"
            atomic_write(daily_out, prev.rstrip() + "\n\n" + "\n".join(lines) + "\n")
            print(f"✅ 整合日志 → {round_out}（当日汇总 → {daily_out}）")
        else:
            print(f"✅ 整合日志 → {round_out}（汇总本轮跳过）")
    for r in report:
        print(r)
    skipped = len(cands_all) - len(fresh)
    print(f"本轮处理 {len(cands)} 条候选（另有 {skipped} 条历史指纹跳过），"
          f"报告 {len(report)} 条，失败 {failures} 条")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
