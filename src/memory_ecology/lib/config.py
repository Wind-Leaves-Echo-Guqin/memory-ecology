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

# 配置门面（PORT_SPEC §4-D）：TOML 优先，缺失/损坏 fail-open 回默认
try:
    import tomllib
    _HAS_TOML = True
except ImportError:
    _HAS_TOML = False  # Python < 3.11

_CONFIG: dict = {}
_CONFIG_LOADED = False

def _read_toml(path: Path) -> dict | None:
    """读一份 TOML；缺失/损坏一律 None（fail-open，用代码默认值）。"""
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _load_config() -> dict:
    """加载 configs/eco.toml，fail-open 返回空 dict（用代码默认值）。

    R-8（v2.5.7）：配置路径必须与数据路径同源 —— 先读
    `hermes_root()/configs/eco.toml`（尊重 MEMORY_ECOLOGY_ROOT，多实例才完整），
    再以代码树 `_P3/configs/eco.toml` 作过渡 fallback。实例优先：
    同一 section 的键以实例为准，实例没写的 section/键保留代码树值。
    """
    global _CONFIG, _CONFIG_LOADED
    if _CONFIG_LOADED:
        return _CONFIG
    _CONFIG_LOADED = True
    if not _HAS_TOML:
        return _CONFIG
    paths = [_P3 / "configs" / "eco.toml", hermes_root() / "configs" / "eco.toml"]
    merged: dict = {}
    seen: set[Path] = set()
    for p in paths:
        try:
            key = p.resolve()
        except OSError:
            key = p
        if key in seen:          # 默认部署（hermes_root == _P3）只读一次
            continue
        seen.add(key)
        if not p.is_file():
            continue
        data = _read_toml(p)
        if not data:
            continue
        for section, value in data.items():
            if isinstance(value, dict) and isinstance(merged.get(section), dict):
                merged[section] = {**merged[section], **value}
            else:
                merged[section] = value
    _CONFIG = merged
    return _CONFIG


def cfg(section: str, key: str, default):
    """读配置项：configs/eco.toml [section] key → 值；缺失回 default。"""
    c = _load_config()
    return c.get(section, {}).get(key, default)


# ── 正文上限与截断标记（v2.5.6，审计 P0-2）──────────────────────────────────
# 为什么要单源：同一件事曾有两个互不知道的实现 —— write_gate 硬编码 200、
# eco_quota 硬编码 220，且两边都是"默默切掉"，落盘后没人能看出正文被截过。
TRUNC_MARK = "…[truncated]"


def gate_max_body(default: int = 200) -> int:
    """detail 正文上限的**唯一**解析入口。

    优先级：环境变量 MEMORY_ECOLOGY_GATE_MAX_BODY
          > configs/eco.toml [gate] max_body
          > default
    非法值（非整数 / < 1）一律回默认 —— 上限是安全阀，不能因配置手误变成 0。
    """
    for raw in (os.environ.get("MEMORY_ECOLOGY_GATE_MAX_BODY"),
                cfg("gate", "max_body", None)):
        if raw is None:
            continue
        text = str(raw).strip()
        if not text:
            continue
        try:
            value = int(text)
        except (TypeError, ValueError):
            continue
        if value >= 1:
            return value
    return default


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
    """从 hermes/.env 读取密钥（DEEPSEEK_API_KEY 等）。

    P1（2026-10-04，吸收 eco_note.read_key 的防御口径）：
    文件缺失 / 键行缺失 / 键值为空 三种情况分别给可操作的错误信息——
    旧版 .env 缺失时抛 FileNotFoundError，用户看到的是路径错误而非"缺 API key"。
    """
    env = hermes_root() / ".env"
    if not env.is_file():
        raise RuntimeError(
            f"配置错误：{env} 不存在（无法读取 {name}）。三种解法："
            f"①创建该文件（{name}=<key>）；"
            f"②设环境变量 MEMORY_ECOLOGY_API_KEY；"
            f"③若接第三方 OpenAI 兼容网关，还需同时设 "
            f"MEMORY_ECOLOGY_API_BASE / MEMORY_ECOLOGY_API_MODEL（v2.5.0 起支持）")
    for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith(name + "="):
            val = line.split("=", 1)[1].strip().strip('"').strip("'")
            if not val:
                raise RuntimeError(f"配置错误：{name} 为空（残留空值键，请检查 {env}）")
            return val
    raise RuntimeError(f"配置错误：{env} 中无 {name} 行")


# ── Guard：schema 保持写入 + 配置不变量（v3.1.0，PLAN「回原文/Guard/加密」批）──
#
# 动机（2026-10-24 事故）：eco.toml 被整文件重写时 [gate].max_body=800 静默丢失
# （P0-2 修复被回退）。教训蒸馏成两件结构：
#   1. set_key()——**唯一受支持的配置改写方式**：行级手术（换值/补行/补段），
#      未知键、注释、原有段落全部保留；整文件替换从此没有正当理由。
#   2. REQUIRED_CONFIG——配置不变量表：selfcheck A6 据此断言「关键键在场」，
#      丢键当场红（而非等截断证据间接暴露）。

REQUIRED_CONFIG = {
    # (section, key): (期望默认, 说明) —— A6 断言「键在场」；值允许实例覆盖，不锁死
    ("quota", "memory_limit"): (3000, "L1 MEMORY 配额"),
    ("quota", "memory_trigger"): (2550, "L1 触发线"),
    ("gate", "max_body"): (800, "正文上限（审计 P0-2）"),
}


def set_key(section: str, key: str, value, root: Path | None = None) -> bool:
    """schema 保持地写入一个配置键（行级手术，写前备份）。

    · 键已存在 → 原位换值（保留行内注释之后的部分丢弃——值行不支持行内注释保真，
      但其余行/注释/未知段全部不动）
    · 键缺失但段存在 → 插到段尾
    · 段缺失 → 追加到文件尾
    · 修改成功后强制重载本模块配置缓存
    返回是否发生写盘。fail-open 之外唯一正当的 eco.toml 写入路径（Guard 语义）。
    """
    global _CONFIG, _CONFIG_LOADED
    import re as _re
    path = (Path(root) if root else hermes_root()) / "configs" / "eco.toml"
    if not path.is_file():
        return False
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    sec_re = _re.compile(r"^\s*\[(.+?)\]\s*$")
    key_re = _re.compile(rf"^(\s*){ _re.escape(key) }\s*=\s*.*$")
    sec_start = None
    sec_end = len(lines)
    for i, ln in enumerate(lines):
        m = sec_re.match(ln)
        if m:
            if sec_start is not None:
                sec_end = i
                break
            if m.group(1).strip() == section:
                sec_start = i
    if sec_start is None:
        lines += ["", f"[{section}]", f"{key} = {value}"]
    else:
        for i in range(sec_start + 1, sec_end):
            if key_re.match(lines[i]):
                lines[i] = f"{key} = {value}"
                break
        else:
            lines.insert(sec_end, f"{key} = {value}")
    backup = path.with_suffix(path.suffix + ".bak-setkey")
    try:
        import shutil as _shutil
        _shutil.copy2(path, backup)
    except OSError:
        pass
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    _CONFIG_LOADED = False
    _load_config()
    return True


def missing_required() -> list[str]:
    """A6 判据：REQUIRED_CONFIG 里**生效配置**中缺席的键。

    生效口径 = _load_config() 的实例+代码树合并（R-8）——任一文件提供即算在场；
    读侧 fail-open 兜底 ≠ 配置健康，这里只回答「配置面是否完整」。
    """
    data = _load_config()
    out = []
    for (sec, key), (default, why) in REQUIRED_CONFIG.items():
        if key not in (data.get(sec) or {}):
            out.append(f"[{sec}].{key}（{why}，默认 {default}）")
    return out
