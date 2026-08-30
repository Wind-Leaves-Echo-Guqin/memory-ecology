# Architecture（架构）

## 存储分层

```
L1 常驻注入（每轮注入上下文，体积有界）
├── MEMORY.md（事实/环境/坑位，配额上限，85% 触发挤出）
└── USER.md  （画像特质，更小配额）
        ▲ 提升（跨会话重复达标）        ▲ 蒸馏（稳定 semantic → 画像）
        │                              │
L2 详情层（memories/detail/，一条一文件，YAML frontmatter + 正文）
        │ 降级（挤出/退役，内容不丢）    │ 提取（LLM 从会话增量提取）
        ▼                              ▼
L3 检索（全量会话库 FTS + 结构化索引）
旁路：quarantine（隔离区，可回滚）/ archive（终态归档）/ gate_log（操作日志）
```

**frontmatter 元数据**（L2 条目的「基因」）：

| 字段 | 含义 |
|---|---|
| `type` | semantic（长期事实）/ episodic（一次性事件）/ procedural（流程做法）/ lesson（教训） |
| `status` | active / dormant / superseded |
| `occurrences` / `session_count` | 出现次数 / 跨会话数（巩固判据） |
| `valid_time` / `transaction_time` | 事件钟：事实生效时点 / 写入时点（冲突判定依据） |
| `last_verified` | 最近复核日（遗忘曲线） |
| `origin_session_id` | 来源会话（溯源） |
| `superseded_by` | 取代链（可回滚的血缘） |

## 四道门（全部为保护区外独立脚本）

### 门① 写入整合门（每日，提取管道之后）

- 输入：会话提取候选（`- [类型] 文本` 行）
- 动作：
  1. **类型分型**：LLM 初判 + 规则兜底（含日期/完成态 → episodic；流程词 → procedural…）
  2. **相似比对**：规范化（去空白标点）后 LCS/SequenceMatcher；`≥0.8` 近似重复、`≥0.5` 疑似冲突
  3. **四动作决策**（借鉴 mem0）：`ADD` / `UPDATE`（近似重复，occurrences+1）/ `NOOP` / `CONFLICT`（同主题矛盾 → 旧条目标 `superseded` 移入 quarantine）
- 工程纪律：LLM 输出非法动作 → 回退规则判定（不静默丢弃）；fingerprint 幂等 + 每动作即时提交；O_EXCL 并发锁；原子写；LLM 指定 target 优先于相似度排序

### 门② 巩固/配额门（每日）

- **提升**（L2→L1）：规则判定——`type∈{semantic,procedural}` 且 `occurrences≥3` 且 `session_count≥2`；刚被挤出的条目 30 天冷却（防「提升↔挤出」震荡）
- **挤出**（L1→L2）：L1 占用 >85% 配额时两段式：
  1. 安全段：排除「同轮新提升 + 高价值」（其同源 L2 为达标提升候选）条目，按 ①detail 同源 → ②历史日期 → ③文件靠后 挤出
  2. 合规段：仍超限则撤销本轮提升、字面 ①②③ 强制合规
- **免写判定**（防静默丢失）：只有「L1 条目被某条 L2 **完整包含**」才允许挤出后不落盘；否则一律先写 L2 再挤
- 每次修改前备份 `.bak-配额前-<ts>`；原子写回

### 门③ 画像蒸馏门（低频）

- 输入：L2 中 `type=semantic` + 跨会话 ≥2 + 无矛盾
- **规则判稳，LLM 只做措辞**：稳定性由跨时段/跨会话计数判定（LLM 判「稳定 vs 瞬时」不可靠，双审共识）
- **观察期 30 天**：候选先入隔离区，到期复查源条目仍 active 才升 USER；源失效 → 候选作废
- **同义才替换**：新特质与 USER 现有条目「包含关系或相似度 ≥0.8」才整块替换（旧文入 quarantine）；相似但非同义 → 追加（防误伤互补的行为规则）
- USER 配额水印检查**前置**（>90% 整轮跳过）；写 USER 原子 + 备份

### 门④ 复核门（月度）

- 遗忘曲线：`last_verified` 超期（semantic 90 天 / episodic 30 天）→ active 标 dormant（同时刷新 last_verified 给足观察期）→ 下轮仍超期 → 移 archive
- 碎片合并候选：正文相似度 ≥0.7 的条目对 → 输出清单（不自动合并）
- quarantine 清理：按**日期子目录名**（非 mtime，防 git 恢复重置时间戳）算保留期，>90 天移 archive

## 两轴状态机

| status \ fate | retained | superseded | archived |
|---|---|---|---|
| active | ✓ | ✗ | ✗ |
| dormant | ✓ | ✓ | ✗ |
| frozen | ✓ | ✗ | ✓ |

- 活性轴（环境问题，可逆）：active ↔ dormant ↔ frozen
- 存续轴（价值问题，不可逆但可回溯）：retained ↔ superseded ↔ archived
- 非法组合 → 体检报警；`status` 为权威，物理目录只是组织视图

## 保护区三级

| 区 | 内容 | 结构动作 |
|---|---|---|
| 核心 | 用户钦点（人格/设计稿/生产链路/备份）、依赖枢纽、核心脚本 | 免疫（需显式批准） |
| 缓冲 | 一般技能、项目作品 | 需确认 |
| 实验 | 空置 | L1 标记即可 |

三通道：钦点 + 依赖枢纽（in-degree）+ 通用度（覆盖矩阵）。免疫结构动作，**不免疫内容更新**。

## 评测（五维健康验收）

适配单用户现实的 LongMemEval 五维：

| 维度 | 检查内容 | 判定线 |
|---|---|---|
| IE 信息提取 | 近 7 天提取→整合管道有日志且零失败 | ≥1 份日志、失败 0 |
| MR 多会话推理 | 跨会话主题在记忆库中的命中率 | ≥60% |
| KU 知识更新 | 冲突/更新动作计数（superseded 机制在转） | ≥1 |
| TR 时间推理 | 事件钟字段覆盖率 | transaction 100%、valid ≥50% |
| ABS 安全弃答 | 抽样常驻条目 LLM 判「可疑/过时」率（弱证据） | 样本 ≥3 且 ≤30% |

**门禁非调参**：评测只判定过/不过，不做「提升曲线」叙事——与「自动化 ≠ 统计校准」一致。
