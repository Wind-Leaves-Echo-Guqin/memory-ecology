"""记忆生态公共库（memory-ecology lib）。

零行为改动原则（2026-08-30 整理方案第 1 步）：
- 只抽「≥2 处重复且行为稳定」的纯函数（atomic_write/slug_of/norm/根路径/LLM 调用）
- 机械搬运，禁止顺手修 bug；不抽业务逻辑（阈值判定/评分规则留各脚本）
- parse_frontmatter 各脚本实现有差异（BOM/clean_value/容错），本轮不统一——已知差异，
  开源发布版（publish.py 阶段）统一并补契约测试
"""
