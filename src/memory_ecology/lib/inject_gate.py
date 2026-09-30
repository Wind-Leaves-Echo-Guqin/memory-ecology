"""注入三态判定门（PORT_SPEC §6 批次 3 · lib/inject_gate.py）。

纯规则、本地 <10ms、零依赖（stdlib only）。
决定注入管线中的每条经验/笔记是注入（keep）、缩短（shorten）、还是跳过（drop）。

首版（决策 8）：shorten 降级为 keep（接口保留三态，等待 brief 机制）。
drop 灰度首月只对「occurrences==1 且 last_seen>60d」类生效。
记忆线字段允许 None（fail-open——缺数据不阻塞注入）。

影子模式：调用方在注入前调 decide()，将结果写入 .inject_decisions.jsonl，
实际注入行为不变——积累 shadow 数据后人工抽检（记分卡 D1-D5），达标才启用 drop。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class GateDecision:
    decision: str            # "keep" | "shorten" | "drop"
    confidence: str          # "high" | "medium" | "low"
    reason: str              # 人读判定理由
    signals: list = field(default_factory=list)  # 触发的信号列表


def decide(
    *,
    entry_type: str = "",
    age_days: int | None = None,
    occurrences: int | None = None,
    last_seen_days: int | None = None,
    status: str = "",
    is_quarantined: bool = False,
    hit_count_30d: int | None = None,  # 遥测上线前恒 None
    stale_days: int = 60,
) -> GateDecision:
    """三态判定。所有字段可缺省——缺数据时 fail-open 返回 keep。

    规则优先级（先到先停）：
    1. quarantined → drop（隔离区条目不注入）
    2. status == "verified" → keep（已验证条目永不 drop）
    3. hit_count_30d is not None and hit_count_30d >= 2 → keep（近30天有命中）
    4. occurrences == 1 and last_seen_days > stale_days → drop（从未复现+陈旧）
    5. age_days > 180 and status == "draft" → drop（陈旧未验证草稿）
    6. 其余 → keep（fail-open）
    """
    signals: list[str] = []

    # 1. 隔离区
    if is_quarantined:
        return GateDecision("drop", "high", "隔离区条目不注入", ["quarantined"])

    # 2. verified 永远 keep
    if status == "verified":
        return GateDecision("keep", "high", "verified 条目保持注入", ["verified"])

    # 3. 近期有命中（遥测上线后生效）
    if hit_count_30d is not None and hit_count_30d >= 2:
        return GateDecision("keep", "high", f"近30天命中 {hit_count_30d} 次", ["hot"])

    # 4. 从未复现 + 陈旧 → drop
    if occurrences is not None and occurrences <= 1:
        if last_seen_days is not None and last_seen_days > stale_days:
            signals.append("occ1_stale")
            return GateDecision(
                "drop", "high",
                f"仅出现 {occurrences} 次且 {last_seen_days} 天未复现（>{stale_days}d 阈值）",
                signals,
            )

    # 5. 陈旧草稿
    if age_days is not None and age_days > 180 and status == "draft":
        signals.append("stale_draft")
        return GateDecision("drop", "medium", f"草稿 {age_days} 天未验证", signals)

    # 6. fail-open keep
    if entry_type:
        signals.append(f"type:{entry_type}")
    return GateDecision("keep", "low", "无 drop 信号（fail-open）", signals)


def format_jsonl(decision: GateDecision, unit_id: str, ts: str | None = None) -> str:
    """序列化为 JSONL 行（影子记账用）。"""
    import json
    entry = {
        "ts": ts or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "unit_id": unit_id,
        "decision": decision.decision,
        "confidence": decision.confidence,
        "reason": decision.reason,
        "signals": decision.signals,
    }
    return json.dumps(entry, ensure_ascii=False)
