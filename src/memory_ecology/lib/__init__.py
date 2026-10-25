"""记忆生态公共库（memory-ecology lib）。

零行为改动原则（2026-08-30 整理方案第 1 步）：
- 只抽「≥2 处重复且行为稳定」的纯函数（atomic_write/slug_of/norm/根路径/LLM 调用）
- 机械搬运，禁止顺手修 bug；不抽业务逻辑（阈值判定/评分规则留各脚本）
- parse_frontmatter 各脚本实现有差异（BOM/clean_value/容错），本轮不统一——已知差异，
  开源发布版（publish.py 阶段）统一并补契约测试
"""

# 唯一版本真源（v3.0.0a1）：门横幅 / eco_version / 发布物一律取自这里。
# 为什么收在这里：审计 P2-4 实测四个版本号并存（pyproject 2.2.0 / VERSION.md v2.5.0 /
# hermes/VERSION.md v2.4.0 / git 提交文案 v2.5.5），"此刻跑的是哪一版"没有可判定答案。
# v3.0.0 线（PORT_SPEC 决策 21）：回填/单源化/部署机制/前后端重构均属 v3.0 组成；
# 正式 tag v3.0.0 = V3_BLUEPRINT §7 验收总清单全绿。
__version__ = "3.1.0"
