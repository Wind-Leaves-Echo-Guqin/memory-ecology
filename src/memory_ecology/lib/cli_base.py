"""cli_base — 所有 CLI 入口的统一基座（v2.5.0 新增）。

解决的问题（每条都有实测证据，见 ARCHITECTURE_TODO §六）：

  B2/B7-b  22 个脚本没有 `sys.stdout.reconfigure`，在中文 Windows（GBK 控制台）
           print 含 emoji/特殊符号时抛 `UnicodeEncodeError` 并中断。
           最恶劣的是 write_gate：崩在收尾 print 上，数据已全部落盘但退出码=1
           （"成功了报失败"）。更糟的是异常处理里的 print 自己也会崩，
           把真正的 FileNotFoundError 掩盖成编码错误 → 报错原文不可信。

  B3       eco_review 用 `Path(__file__).resolve().parent.parent` 自己算数据根，
           绕过 lib.config，导致设了 MEMORY_ECOLOGY_ROOT 后路径仍多算一层 src/，
           同一个生态被拆成两个数据根。

  B7       eco_quota 的锁文件放 `<root>/scripts/`，用 `os.open(O_CREAT)` 不建父目录
           → 新数据根必崩。而 README 明确教用户设 MEMORY_ECOLOGY_ROOT。

纪律（新脚本必须遵守，check_entry_discipline.py 会检查）：

  1. `main()` 第一行调 `bootstrap()`          —— 编码
  2. 数据根一律 `resolve_root()`              —— 禁止自算 __file__
  3. 写文件前 `ensure_parent()` / `ensure_dirs()` —— 禁止裸 open 不建目录
  4. 需要互斥时 `locked()`                    —— 统一走 gatekit（带 stale 回收）

跨平台：编码修复在 POSIX 上是 no-op（utf-8 默认），其余逻辑两端一致。
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path

__all__ = [
    "bootstrap",
    "resolve_root",
    "ensure_dirs",
    "ensure_parent",
    "locked",
    "fail",
    "banner",
]


# ── 1. 编码 ────────────────────────────────────────────────────────────────

def bootstrap(safe_stdio: bool = True) -> None:
    """每个 CLI 的 main() 第一行调用。

    把 stdout/stderr 强制成 UTF-8 + errors=replace：
      · 中文 Windows（GBK/cp936）下不再因 emoji 崩
      · errors="replace" 保证极端情况下也不会在 print 上崩
        —— 这正是 B7-b 的修法（异常处理里的 print 不再掩盖真错）

    POSIX 上通常已 UTF-8，reconfigure 是幂等的无害操作。
    某些被替换过的 stream（如 pytest capture、pythonw）没有 reconfigure，
    用 hasattr 防御。
    """
    if not safe_stdio:
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            # 已经关闭 / 被重定向到不支持重配的对象：忽略，不因修编码而崩
            pass


# ── 2. 数据根 ──────────────────────────────────────────────────────────────

def resolve_root(explicit: str | os.PathLike | None = None) -> Path:
    """数据根的唯一合法解析入口。

    优先级（与 ECO_CONTRACT §4 一致）：
      1. 显式参数（--root / --detail 等）
      2. 环境变量 MEMORY_ECOLOGY_ROOT
      3. lib.config.hermes_root()（跟随安装位置派生）

    ★ 禁止各脚本再用 `Path(__file__).resolve().parent.parent` 自算 —— 那是 B3 的根因。
    """
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get("MEMORY_ECOLOGY_ROOT")
    if env:
        return Path(env)
    from lib.config import hermes_root  # 延迟导入：避免循环依赖

    return Path(hermes_root())


# ── 3. 目录 ────────────────────────────────────────────────────────────────

def ensure_dirs(root: Path | str, *subdirs: str) -> None:
    """在数据根下确保若干子目录存在（幂等）。

    典型用法：
        ensure_dirs(root, "memories/detail", "scripts")   # scripts/ 是锁文件的家
    """
    base = Path(root)
    for sub in subdirs:
        (base / sub).mkdir(parents=True, exist_ok=True)


def ensure_parent(path: Path | str) -> Path:
    """确保某个文件的父目录存在，返回该路径（便于链式调用）。

    这是 B7 的最小修法：写任何文件前先 ensure_parent，不要裸 open。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


# ── 4. 互斥 ────────────────────────────────────────────────────────────────

@contextmanager
def locked(lock_path: Path | str, *, blocking: bool = False):
    """统一的互斥上下文（走 gatekit，带 stale 锁回收）。

        with locked(root / "scripts" / ".eco_x.lock") as ok:
            if not ok:
                return fail("另一个实例正在运行")
            ...

    退出时自动释放（幂等）。不 blocking 时抢不到立即返回 False，
    由调用方决定如何报告 —— 不静默跳过（静默是最坏的失败方式）。
    """
    from lib.gatekit import acquire_lock_auto, release_lock

    path = ensure_parent(lock_path)
    ok, how = acquire_lock_auto(path)
    try:
        yield ok
    finally:
        if ok:
            release_lock(path)


# ── 5. 门标识（v2.5.6）─────────────────────────────────────────────────────

def banner(name: str | None = None, *, file: str | os.PathLike | None = None) -> None:
    """门启动横幅：脚本名 + 版本 + 自身源码 SHA256（前 12 位）+ 数据根。

    为什么需要（审计 P0-1）：三棵树曾长期各跑各的，**没有任何地方能回答
    "此刻在生产跑的是哪一版"** —— 生产停在 2026-10-07 快照这件事，是靠人工比对
    文件大小/日期才发现的。横幅把版本与哈希打进 cron 日志，"部署是否生效"
    从此是可观测事实，不再是推断。

    哈希按 LF 归一计算：dev 工作树 CRLF、生产 LF，字节级哈希会把同一份代码
    算成两个值；归一后与 tools/eco_selfcheck.py 的算法一致。

    永不抛异常：横幅是观测设施，不能因为它自己坏掉而拖垮门。
    """
    import hashlib

    src = None
    try:
        src = Path(file if file is not None else sys.argv[0]).resolve()
        digest = hashlib.sha256(
            src.read_bytes().replace(b"\r\n", b"\n")).hexdigest()[:12]
    except (OSError, ValueError):
        digest = "?"
    try:
        from lib import __version__ as _ver
    except Exception:                      # 布局异常也不能中断门
        _ver = "?"
    try:
        from lib.config import hermes_root
        root = str(hermes_root())
    except Exception:
        root = "?"
    print(f"[gate] {name or (src.stem if src else '?')} v{_ver} srcsha={digest} "
          f"root={root} py={sys.version.split()[0]}")


# ── 6. 退出码 ──────────────────────────────────────────────────────────────

def fail(message: str, code: int = 1) -> int:
    """统一失败出口：打印到 stderr 并返回退出码（供 `return fail(...)` 用）。

    ★ 为什么走 stderr：stdout 可能被 `--format json` 的消费者解析，
      错误信息混进 JSON 会让解析器崩（P3 实测过 `--semantic` 的这类问题）。
    """
    print(f"错误: {message}", file=sys.stderr)
    return code
