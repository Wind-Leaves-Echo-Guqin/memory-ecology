# Memory Ecology — A2A Agent Card

> **Agent2Agent 能力声明**（Google A2A 协议格式，2026-08-30）
> 本文档让任何 A2A 兼容的 agent 发现、理解并调用 Memory Ecology 的能力。
> 项目主页：https://github.com/Wind-Leaves-Echo-Guqin/memory-ecology

## 1. Agent Card（标准 JSON）

```json
{
  "name": "memory-ecology",
  "description": "Agent memory lifecycle governance toolkit. Governs an AI agent's memories and skills as an ecosystem: extraction, write integration, consolidation/quota, persona distillation, expiry review, quality evaluation, skill breeding, and ecosystem search. Philosophy: evolution is the goal, management is the means; zero user maintenance; rule-driven automation with reversible fallbacks; physical deletion disabled at code level.",
  "url": "https://github.com/Wind-Leaves-Echo-Guqin/memory-ecology",
  "version": "0.8.0",
  "documentationUrl": "https://github.com/Wind-Leaves-Echo-Guqin/memory-ecology/blob/main/docs/architecture.md",
  "provider": {
    "organization": "Wind-Leaves-Echo-Guqin",
    "url": "https://github.com/Wind-Leaves-Echo-Guqin"
  },
  "skills": [
    {
      "id": "memory_extract",
      "name": "会话记忆增量提取",
      "description": "从 agent 会话记录（SQLite）增量提取稳定事实/偏好/纠正，LLM 提炼为候选写入 pending 区；水位线分批推进防消息丢失，周 token 熔断",
      "tags": ["memory", "extraction", "llm"],
      "examples": ["提取今天会话中值得长期记住的事实"]
    },
    {
      "id": "memory_write_gate",
      "name": "记忆写入整合门",
      "description": "候选记忆整合进 L2 详情层：类型分型（semantic/episodic/procedural/lesson）+ 相似比对 + 四动作决策（ADD/UPDATE/NOOP/CONFLICT）+ 事件钟字段（valid_time/transaction_time）；矛盾旧条目标 superseded 移入 quarantine，永不物理删除",
      "tags": ["memory", "write", "conflict"],
      "examples": ["把候选事实写入记忆库，自动处理与已有记忆的重复和矛盾"]
    },
    {
      "id": "memory_consolidate",
      "name": "巩固/配额门",
      "description": "L1 常驻记忆（MEMORY.md/USER.md）的自动提升与挤出：跨会话重复达标才提升；超 85% 配额按必需度挤出（高价值条目保护、两段式防震荡）；修改前备份，信息零丢失",
      "tags": ["memory", "quota", "lifecycle"],
      "examples": ["记忆文件快满了，自动整理并保持有界"]
    },
    {
      "id": "persona_distill",
      "name": "画像蒸馏门",
      "description": "稳定 semantic 记忆蒸馏为 USER 画像特质：规则判稳（跨会话计数，LLM 只做措辞）+ 30 天观察期 + 同义才替换（防误伤互补规则）；USER 配额保护，原子写 + 备份",
      "tags": ["persona", "distillation", "llm"],
      "examples": ["从长期记忆中提炼用户稳定画像"]
    },
    {
      "id": "memory_review",
      "name": "复核门",
      "description": "遗忘曲线：last_verified 过期（semantic 90 天/episodic 30 天）→ dormant → archive（全程可逆）；碎片合并候选清单；quarantine 保留期管理",
      "tags": ["memory", "lifecycle", "expiry"],
      "examples": ["每月例行复核记忆健康度"]
    },
    {
      "id": "memory_evaluate",
      "name": "记忆质量评测",
      "description": "LongMemEval 五维健康验收（信息提取/多会话推理/知识更新/时间推理/安全弃答）；伪 gold + 判定线，门禁非调参",
      "tags": ["evaluation", "quality"],
      "examples": ["评测记忆注入质量是否达标"]
    },
    {
      "id": "skill_breed",
      "name": "技能繁殖",
      "description": "从现有技能创建变体（血缘 evolved_from、引用继承、候选区隔离）；进化自动化（共现检测→候选→评估→取代/共存/归档）由规则驱动",
      "tags": ["skills", "evolution"],
      "examples": ["基于两个常用技能繁殖出组合技能"]
    },
    {
      "id": "ecosystem_search",
      "name": "生态检索",
      "description": "两级路由检索记忆与技能：级联排序（活性→存续→版本→生态位重要度）、加载预算 top-N、已加载标注、缺口分析",
      "tags": ["search", "retrieval"],
      "examples": ["在记忆生态里检索相关技能与事实"]
    },
    {
      "id": "ecosystem_health",
      "name": "生态健康体检",
      "description": "只读体检报告生成器：版本覆盖率/引用健康度/状态机合法性/cron 健康行/记忆容量，健康双保险（cron 告警 + 每日健康行 + 产出探针）",
      "tags": ["health", "monitoring"],
      "examples": ["生成生态健康体检报告"]
    }
  ],
  "capabilities": {
    "streaming": false,
    "pushNotifications": true,
    "stateful": true
  },
  "security": {
    "authentication": "none",
    "localOnly": true
  },
  "defaultInputModes": ["text"],
  "defaultOutputModes": ["text", "json"]
}
```

## 2. 项目是什么（给 agent 的一句话）

Memory Ecology 是一套 **agent 记忆生命周期治理工具集**：把 agent 的记忆与技能当作一个有生有死的生态来管理——有出生（提取/繁殖）、有巩固（提升/蒸馏）、有消亡（复核/归档）、有遗传（血缘/进化）、有健康监测。区别于主流记忆引擎（只解决「存得多、找得准」），它解决的是「记忆长期有界且鲜活」。

## 3. 能力与调用约定

| 能力 | 触发方式 | 输入 | 输出 | 写入面 |
|---|---|---|---|---|
| 会话记忆提取 | cron 每日 / CLI `eco_extract.py` | 会话库 | pending 候选 md | memories/pending/（候选区） |
| 写入整合 | cron 每日 / `write_gate.py` | pending 候选 | L2 detail 条目 + 日志 | memories/detail/、quarantine/ |
| 巩固/配额 | cron 每日 / `eco_quota.py` | L1 + L2 | 配额调整 + 备份 | MEMORY.md/USER.md（有备份） |
| 画像蒸馏 | cron 低频 / `distill_stage.py` | L2 semantic | USER 特质 + 候选 | USER.md（原子+备份） |
| 复核 | cron 月度 / `eco_review.py` | L2 frontmatter | dormant/archive 迁移 | memories/archive/ |
| 评测 | 手动/季度 / `eco_eval.py` | 全库 | 五维报告 | 只读 |
| 技能繁殖 | CLI `eco_breed.py` | 来源技能 | 变体候选 | 候选区 |
| 生态检索 | CLI `eco_search.py` | 查询词 | 两级结果 | 只读 |
| 健康体检 | CLI `eco_health_check.py` | 全库 | 体检报告 md | 只读（报告双写桌面/designs） |

## 4. 约束（调用方必须遵守）

1. **物理删除在代码层禁用**——只有 `superseded` 标记 + quarantine 移动，一切可回滚（git 基因库每日快照）
2. **保护区**：`memories/MEMORY.md`、`USER.md`、`pending/`、核心脚本（eco_extract.py/eco_health_alert.py）免疫结构动作；新组件必须放保护区外
3. **用户维护负担 = 零**——所有机制自动运行，阈值保守默认自动生效（拍板=覆盖），年度确认可跳过
4. **单用户本地环境**；LLM 调用走低成本模型（deepseek-v4-flash），有周 token 熔断
5. **规则驱动**：判定用确定性规则（计数/时间/类型），LLM 只做措辞与合并建议

## 5. 理念锚点（为什么这样设计）

- 进化是目的，管理是手段
- 记忆不是库存，是生态（有生有死、入口松出口紧）
- 自动化 ≠ 统计校准（零样本可运行）
- 正确性交给「可撤销」而非「阈值准」

## 6. 关联文档

- 设计哲学：`docs/philosophy.md`
- 架构（四道门/状态机/保护区/评测）：`docs/architecture.md`
- 关键决策记录：`docs/decisions.md`
- 实战教训：`docs/lessons.md`
