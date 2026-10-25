"""gatekit — 门骨架统一层（PORT_SPEC §C）。

四门（write_gate/quota/distill/review）共享的纪律：
  抢锁 → dry-run 守卫 → 写前备份 → 账本写入 → idle 心跳
新门继承骨架，不再各自实现（消灭 Q14/dry-run 建库类结构性 bug）。

零依赖（stdlib only）。与 zero-dep 核心原则一致。
"""
from __future__ import annotations
import os
import sqlite3
from pathlib import Path
from datetime import datetime


# ── 锁 ────────────────────────────────────────────────────────────────

def acquire_lock(lock_path: Path) -> bool:
    """O_EXCL 抢锁；已有实例在运行则返回 False。"""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    # v2.5.0：其余 OSError（EACCES/ENOSPC/EMFILE）不再伪装成"已有实例在运行"，
    # 否则磁盘满会被误报成并发跳过，门静默失效。
    try:
        os.write(fd, str(os.getpid()).encode())
    except OSError:
        try:
            lock_path.unlink(missing_ok=True)   # 回滚半成品锁
        except OSError:
            pass
        raise
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    return True


def release_lock(lock_path: Path) -> None:
    """释放锁（幂等——不存在时不报错）。

    ★ v2.5.0：只删**属于本进程**的锁。若运行期间锁已被别的实例重抢，
      无条件 unlink 会删掉对方正在持有的锁 → 两个写者同时进临界区。
    """
    pid = read_lock_pid(lock_path)
    if pid is not None and pid != os.getpid():
        return
    lock_path.unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool:
    """进程存活探测（零依赖）：Windows 走 OpenProcess 句柄探测，
    POSIX 走 os.kill(pid, 0)。探测失败按存活处理（宁可误判活也不误删锁）。"""
    if pid <= 0 or pid >= 2**31:
        # 范围防御（上收自 eco_note_backfill_runall）：超合理范围视为死（stale 锁可回收；
        # 锁文件被写坏的情形），R7 单源收编 2026-10-04
        return False
    if os.name == "nt":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            ERROR_ACCESS_DENIED = 5
            k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                # 拒绝访问 ≠ 不存在：进程在但无查询权限，按存活处理
                return k32.GetLastError() == ERROR_ACCESS_DENIED
            k32.CloseHandle(h)
            return True
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def pid_alive(pid: int) -> bool:
    """公开别名（跨模块使用走这里；_pid_alive 为历史名）。"""
    return _pid_alive(pid)


def read_lock_pid(lock_path: Path) -> int | None:
    """读锁文件里记录的 pid；缺失/空/非数字（损坏）统一返回 None——调用方保守持有。"""
    try:
        s = lock_path.read_text(encoding="utf-8", errors="replace").strip()
        return int(s) if s.isdigit() else None
    except OSError:
        return None


def acquire_lock_auto(lock_path: Path) -> tuple[bool, str]:
    """抢锁；发现死进程残留锁（pid 不存活或 pid=本进程）自动清理后重抢一次。

    返回 (是否成功, "fresh"|"stale-cleared"|"held")。
    pid 无法读取（空/损坏锁文件）时保守视为持有中——不误删可能活着的锁。
    """
    if acquire_lock(lock_path):
        return True, "fresh"
    pid = read_lock_pid(lock_path)
    # v2.5.0：本进程已持有 → 判 held（旧实现会当残留回收，导致同进程重入不再互斥）
    if pid == os.getpid():
        return False, "held"
    if pid is None or _pid_alive(pid):
        return False, "held"
    # 死锁残留（或本进程残留）：清理重抢
    try:
        lock_path.unlink()
    except OSError:
        return False, "held"
    if acquire_lock(lock_path):
        return True, "stale-cleared"
    return False, "held"


class LockContext:
    """with LockContext(path) as locked: — locked=True 表示成功获取锁。"""
    def __init__(self, lock_path: Path):
        self._path = lock_path
        self.locked = False
        self.how = "held"       # v2.5.0：Enter 前也要有值

    def __enter__(self):
        self.locked, self.how = acquire_lock_auto(self._path)
        return self.locked

    def __exit__(self, *exc):
        # v2.5.0：只在确实持有时释放（原来无条件 unlink 会删掉别人的锁）
        if self.locked:
            release_lock(self._path)


# ── 数据库连接 ────────────────────────────────────────────────────────

def connect_db(db_path: Path, dry_run: bool = False) -> sqlite3.Connection:
    """dry-run 且库不存在时用 :memory:（不留 0 字节文件）；
    库存在时正常连接（dry-run 仍可读真数据做指纹去重）。"""
    if dry_run and not db_path.is_file():
        return sqlite3.connect(":memory:")
    return sqlite3.connect(db_path, timeout=10)


# ── 账本写入 ──────────────────────────────────────────────────────────

def write_ledger(
    conn: sqlite3.Connection,
    table: str,
    entries: list[tuple],
    columns: list[str],
    dry: bool = False,
) -> int:
    """统一账本写入。entries 为 tuple 列表，columns 为表列名。dry 时不写。

    返回写入行数（dry=0）。表不存在时自动建表（非 dry 模式）。
    """
    if dry:
        return 0
    cols = ", ".join(columns)
    ph = ", ".join("?" * len(columns))
    if not _table_exists(conn, table):
        conn.execute(f"CREATE TABLE IF NOT EXISTS {table} ({cols})")
    conn.executemany(f"INSERT OR REPLACE INTO {table} VALUES ({ph})", entries)
    return len(entries)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        conn.execute(f"SELECT 1 FROM {table} LIMIT 1")
        return True
    except sqlite3.OperationalError:
        return False


# ── 写前备份 ──────────────────────────────────────────────────────────

def backup_file(path: Path, suffix: str = ".bak") -> Path | None:
    """写前备份：cp path → path.bak。文件不存在时返回 None。"""
    if not path.is_file():
        return None
    bak = path.with_suffix(path.suffix + suffix)
    bak.write_bytes(path.read_bytes())
    return bak
