# AGENTS.md — 给 AI Agent 的仓库指南

本仓库是 **Memory Ecology**：一套 agent 记忆生命周期治理工具集。代码在 `src/memory_ecology/`（9 个脚本 + `lib/` 公共模块），方法论文档在 `docs/`，Agent 能力声明见 `A2A.md`。

## 仓库结构

```
src/memory_ecology/     # 核心代码（9 个治理脚本 + lib/）
├── lib/                # 公共模块：config（路径/配置）、fs（原子写/slug/norm）、llm（LLM 调用纯函数）
├── write_gate.py       # 门① 写入整合：类型分型/四动作决策/事件钟/quarantine
├── eco_quota.py        # 门② 巩固/配额：提升/挤出/高价值保护
├── distill_stage.py    # 门③ 画像蒸馏：规则判稳/观察期/同义替换
├── eco_review.py       # 门④ 复核：遗忘曲线/dormant/archive
├── eco_eval.py         # 五维评测（LongMemEval 适配）
├── eco_breed.py        # 技能繁殖（血缘/候选区）
├── eco_search.py       # 生态检索（两级路由）
├── eco_state.py        # 两轴状态机（活性×存续）
└── eco_health_check.py # 只读体检报告生成器
tests/                  # 干净房测试（unittest，零依赖，mock LLM）
scripts/scan_sensitive.py  # 发布前敏感词扫描（pre-push hook 引用）
config.example.yaml     # 配置示例（缺省用默认值即可运行）
```

## 运行与测试

```bash
# 干净房测试（不依赖 LLM key/个人数据）
python -m unittest discover tests

# 运行单个治理脚本（--help 查看参数）
python src/memory_ecology/eco_quota.py --help
```

路径默认跟随安装位置派生（`lib/config.py`），可用环境变量 `MEMORY_ECOLOGY_ROOT` 覆盖数据根（测试/多实例）。

## 设计契约（改动前必读）

1. **删除在代码层禁用**：只有 `superseded` 标记 + quarantine 移动，一切可回滚
2. **规则驱动**：判定用确定性规则（计数/时间/类型），LLM 只做措辞与合并建议
3. **零外部依赖**：纯 stdlib（LLM 用 urllib），不引入 pip 依赖
4. **保护区**：`eco_extract`/`eco_health_alert`（Hermes 集成层）不在本仓库——本仓库是通用治理核心
5. **本仓库是生成物**：生产为唯一真源（`publish_opensource.py` 生成），**不手编**——贡献请回灌生产再发布

## 贡献指南

- 先跑 `python -m unittest discover tests`（必须全绿）
- 发布前跑 `python scripts/scan_sensitive.py`（pre-push hook 会自动执行，个人标识会拦截推送）
- 个人标识脱敏规则在 `publish_opensource.py` 的 `sanitize()`（生产保留个人词，发布物替换为通用词）
