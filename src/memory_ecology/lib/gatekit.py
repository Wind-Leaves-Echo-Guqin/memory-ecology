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
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except OSError:
        return False


def release_lock(lock_path: Path) -> None:
    """释放锁（幂等——不存在时不报错）。"""
    lock_path.unlink(missing_ok=True)


class LockContext:
    """with LockContext(path) as locked: — locked=True 表示成功获取锁。"""
    def __init__(self, lock_path: Path):
        self._path = lock_path
        self.locked = False

    def __enter__(self):
        self.locked = acquire_lock(self._path)
        return self.locked

    def __exit__(self, *exc):
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
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {table} ({cols})" if not _table_exists(conn, table) else ""
    )
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
