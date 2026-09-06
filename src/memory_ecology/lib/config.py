"""路径与配置加载（memory-ecology lib）。

设计：默认 = 开箱即用值（跟随安装位置，__file__ 派生），环境变量可覆盖；
配置文件加载骨架预留（第 6 步阈值/模型配置化时填充）。零行为改动——生产路径
（hermes 根）由本模块统一派生，不再硬编码个人路径。
"""
import os
from pathlib import Path

# scripts/lib/config.py → 三级上级 = hermes 根（生产）
# src/memory_ecology/lib/config.py（开源 src 布局）→ 三级上级 = src，再上一级 = 仓库根
# （Q30 修复 2026-09-06：原派生在开源树落到 <repo>/src/，陌生用户首跑会在代码目录长数据）
_P3 = Path(__file__).resolve().parent.parent.parent
_HERMES = _P3.parent if _P3.name == "src" else _P3

# 环境变量覆盖（测试/多实例用）
_ENV_OVERRIDE = os.environ.get("MEMORY_ECOLOGY_ROOT")


def hermes_root() -> Path:
    """生态根目录（生产=hermes；环境变量可覆盖）。"""
    return Path(_ENV_OVERRIDE) if _ENV_OVERRIDE else _HERMES


def data_dir() -> Path:
    """记忆数据目录（memories/）。"""
    return hermes_root() / "memories"


def db_path() -> Path:
    """生态日志库（eco.db）。"""
    return hermes_root() / "eco.db"


def env_key(name: str) -> str:
    """从 hermes/.env 读取密钥（DEEPSEEK_API_KEY 等）。"""
    env = hermes_root() / ".env"
    for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError(f"{name} not found in {env}")
