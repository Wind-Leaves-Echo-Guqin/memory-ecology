# Memory Ecology（记忆生态）

**Agent 记忆的生命周期方法：记忆不是库存，是生态。**

`MIT` · `Python 3.10+` · `核心零依赖` · `纯本地` · `v3.1.0`

当前业界的 Agent 记忆系统大多是「引擎」——聚焦写入、召回、上下文卸载（TencentDB Agent Memory、mem0、Zep、Letta、MemOS……）。本项目持不同立场：真正的难点不是「存得更多」，而是**治理**——在单用户、低维护环境里，让记忆有生、有活、有死、有进化。

## 30 秒了解

- **是什么**——四道生命周期门（写入 → 巩固 → 蒸馏 → 复核）让 agent 记忆**有界、鲜活、全程可逆**。规则驱动、零维护；LLM 只参与门①相似/矛盾终审与门③画像措辞；物理删除在代码层禁用。
- **本仓库**——开放的**治理核心层**：方法论文档 + 9 个门脚本与其运行所依赖的共享 `lib/`。
- **要接宿主？**——[memory-ecology-compat](https://github.com/Wind-Leaves-Echo-Guqin/memory-ecology-compat) 是多宿主衍生线（dsh 适配、检索消费端、经验笔记本、本地 GUI）。两条线版本号对齐（当前均为 **v3.1.0**）；本仓库核心定期从 compat 生产树镜像。

## 差异化定位

| 维度 | 典型记忆引擎 | 本项目（Memory Ecology） |
|---|---|---|
| 核心问题 | 如何存更多、召回更准？ | 如何让记忆**长期有界且鲜活**？ |
| 生命周期 | 无——只增不减 | 两轴状态机：活性轴（active↔dormant↔frozen）× 存续轴（retained↔superseded↔archived） |
| 进化 | 手动或 roadmap | 规则驱动自动化：共现 → 变体 → 评估 → 取代/共存/归档 |
| 删除 | 常常物理删除 | **永不删除**——`superseded` 标记 + quarantine + git 快照（一切可回滚） |
| 维护 | 运营者人工打理 | **用户维护负担 = 零**——自动化默认，阈值自动生效，用户只在结果处可选把关 |
| 校准 | 统计校准，需要数据 | **规则驱动 + 保守阈值 + 可逆兜底**——零统计样本即可运行 |
| 评测 | 公开数据集跑分 | 适配单用户现实的五维健康评测（提取 / 多会话推理 / 知识更新 / 时间推理 / 安全弃答） |

## 架构：记忆生命周期的四道门

```
                 ┌──────────────────────────────────────────────┐
  会话提取候选 ──▶│ 门① 写入整合门（每日）                        │
                 │ 类型分型 + 相似比对 +                          │
                 │ ADD / UPDATE / NOOP / CONFLICT 决策           │
                 │ + 事件钟（valid_time / transaction_time）     │
                 └──────────────────────┬───────────────────────┘
                                        ▼
  L2 详情层（markdown，frontmatter 元数据）◀── 降级 ──┐
    ▲                                             │
    │ 蒸馏（稳定 semantic 事实）                     │
    │                        ┌────────────────────┴──────────────┐
    │                        │ 门② 巩固/配额门（每日）             │
    │                        │ 提升：occurrences≥N 且跨会话 ⇒ L1   │
    │                        │ 挤出：L1 > 85% 配额 ⇒ L2            │
    │                        │ （高价值条目受保护）                 │
    └────────────────────────└──────────────┬─────────────────────┘
                                               ▼
  L1 常驻（MEMORY.md / USER.md，体积有界，每轮注入）
    │
    └──▶ 门③ 画像蒸馏门：稳定 semantic → USER 画像特质
         30 天观察期；同义才替换，绝不盲目覆盖

  ┌──────────────────────────────────────────────┐
  │ 门④ 复核门（月度）                            │
  │ last_verified 超期 → dormant → archived      │
  │ 碎片合并候选、quarantine 保留期管理            │
  └──────────────────────────────────────────────┘
```

每道门都是**保护区外的独立脚本**；每个动作都**记录日志且可回滚**（记忆树每日 git 快照）；物理删除在**代码层禁用**。

## 快速开始

```bash
git clone https://github.com/Wind-Leaves-Echo-Guqin/memory-ecology.git
cd memory-ecology

# 1) 干净房测试套件——mock LLM、零依赖、任何机器可跑
python -m unittest discover tests

# 2) 摸一个门（只读检索记忆库）
cd src/memory_ecology
python eco_search.py --help

# 3) 接线之前先读方法论
#    docs/philosophy.md → docs/architecture.md
```

说明：

- 数据根默认跟随安装位置；多实例/自定义位置设 `MEMORY_ECOLOGY_ROOT` 环境变量。阈值自带保守默认值，见 `config.example.yaml`。
- 门①/门③ 调用 LLM（DeepSeek 兼容端点；key 从 `MEMORY_ECOLOGY_API_KEY` 或数据根 `.env` 读取，base URL 可配）。其余环节纯规则、离线可用。
- 宿主专属接线（会话历史提取、注入 hook）属**集成层**——参考适配见 compat 仓库的 `integrations/`。

## 设计原则（摘要）

1. 进化是目的，管理是手段。
2. 用户维护负担 = **零**（年度确认可跳过、报告可不看）。
3. 规则驱动 + 保守阈值 + 可逆兜底；自动化 ≠ 统计校准。
4. 一切可回滚：git 快照 + 操作日志 + quarantine。
5. 机制优先于协议：凡被证明不可靠的环节一律机制化。
6. 组合优于堆叠；生态优于囤积。
7. 保护区三级（核心/缓冲/实验）：免疫结构动作，不免疫内容更新。

完整版见 [`docs/philosophy.md`](docs/philosophy.md)

## 仓库内容

| 路径 | 内容 |
|---|---|
| [`docs/philosophy.md`](docs/philosophy.md) | 九条设计原则与推导 |
| [`docs/architecture.md`](docs/architecture.md) | 四道门、两轴状态机、保护区、评测体系 |
| [`docs/decisions.md`](docs/decisions.md) | 关键设计决策——选了什么、否了什么、为什么 |
| [`docs/lessons.md`](docs/lessons.md) | 单用户生产环境跑出来的实战教训 |
| `src/memory_ecology/` | 9 个治理脚本（四道门、检索、健康检查、评测、状态）+ 共享 `lib/`（config / fs / gatekit / llm / memstore / metrics / pii / similarity / cli_base / safeio / inject_gate） |
| [`tests/test_cleanroom.py`](tests/test_cleanroom.py) | 零依赖干净房测试套件（mock LLM、fixture 隔离） |
| [`config.example.yaml`](config.example.yaml) | 带保守默认值的配置模板 |
| [`CHANGELOG.md`](CHANGELOG.md) | 版本历史 |

## 两仓一线

| | 本仓库（`memory-ecology`） | [`memory-ecology-compat`](https://github.com/Wind-Leaves-Echo-Guqin/memory-ecology-compat) |
|---|---|---|
| 角色 | 治理核心层——方法论 + 四道门参考实现 | 多宿主衍生线——可移植核心 + 宿主适配层 |
| 多出的面 | — | 检索消费端（`memory_query`）、经验笔记本、备份、本地 GUI、dsh / Hermes 适配器 |
| 版本 | 自 v2.1.2 起与 compat 对齐（当前 **v3.1.0**） | 生产树；本仓库核心定期自其镜像 |

## 里程碑

- **2026-08 · 方法论**——哲学、架构、决策记录、实战教训以开放文档公开。
- **2026-09-01 · v2.0.0**——完整实现开源；验收标准 = 装下来就能用（clone → 测试 → 全绿）。
- **2026-09-06 · v2.2.0**——安全写路径加固（`lib/safeio.py`）+ 评测门禁线（INJ / GOLD）。
- **2026-10-25 · v3.1.0**——核心同步自 compat 生产树：入口纪律（`lib/cli_base.py`）、并发与 Windows 原子写加固、账本脱敏（`lib/pii.py`）、相似度单源、版本单源。

完整历史见 [`CHANGELOG.md`](CHANGELOG.md)

## 致谢与参考

- 设计讨论参考了：[TencentDB Agent Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory)、mem0、Zep/Graphiti、Letta (MemGPT)、LangMem、Hindsight、OpenViking、Karpathy 的 LLM Wiki。
- 构建与验证平台：[Hermes Agent](https://hermes-agent.nousresearch.com/)（Nous Research）。

## License

MIT © Wind-Leaves-Echo-Guqin
