"""文件系统公共函数（memory-ecology lib）：原子写 / slug / 规范化。

零行为改动：实现取自各脚本已有函数（mkstemp 原子写为最稳版本，
最终产物与各脚本现行为等价——os.replace 后临时文件消失）。
"""
import hashlib
import os
import re
import tempfile
import time
from pathlib import Path


# v2.5.0: Windows 上 os.replace 遇目标文件被其他进程/线程打开会抛
# PermissionError(WinError 5) / WinError 32; POSIX 允许 rename 覆盖已打开文件,
# Windows 不允许 -- 同一份代码在 Linux 上原子, Windows 上会随机失败.
_REPLACE_RETRIES = 8
_REPLACE_BACKOFF = 0.02   # 秒; 20ms 起步, 指数退避


def atomic_write(path: Path, text: str) -> None:
    """临时文件 + os.replace 原子写回（失败清理临时文件并重抛）。

    Windows 兼容：目标文件被读者占用时 os.replace 会失败（WinError 5/32）。
    POSIX 允许 rename 覆盖已打开文件，Windows 不允许 —— 同一份代码两端行为不同。

    应对（三级）：
      1. 短退避重试（8 次，约 5s）：目标是绝大多数真实现场（读者持有时间为一次 read）
      2. 长退避（6 次，约 3s）：读者是持续轮询的场景
      3. **DeleteFile 兜底**：Windows 允许删除"已打开但以共享删除方式打开"的文件；
         删掉目标后立刻 replace，把"被占用"变成"短暂不存在"。
         若目标正被以**不允许删除**的方式打开，本步也会失败，此时才抛出。

    ★ 绝不降级为原地覆写：实测那样会让读者读到半截内容（418882 次读中 95 次坏）。
      "写慢"可以接受，"写坏"不可以。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".eco_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            # v2.5.6（审计 P2-2 并集）：fsync 原先只在生产 10-07 版里，
            # 重试/挪开兜底只在 dev/compat 里 —— 两个正确属性从未同时存在于同一份代码。
            # os.replace 只保证"改名原子"，不保证"数据已落盘"；不 fsync 时断电/崩溃会留
            # "文件名可见、内容为空或半截"的记忆条目。
            f.flush()
            os.fsync(f.fileno())
        # mkstemp 建的是 0600；恢复与旧实现（write_text）一致的 0644
        os.chmod(tmp, 0o644)
        last = None
        for attempt in range(_REPLACE_RETRIES):
            try:
                os.replace(tmp, str(path))
                return
            except PermissionError as e:      # WinError 5：目标被占用
                last = e
                time.sleep(_REPLACE_BACKOFF * (2 ** attempt))
            except OSError as e:
                # Windows 上另一种表现是 WinError 32（sharing violation）
                if getattr(e, "winerror", None) in (5, 32):
                    last = e
                    time.sleep(_REPLACE_BACKOFF * (2 ** attempt))
                else:
                    raise
        # 第 2 级：长退避
        for attempt in range(6):
            time.sleep(0.05 * (2 ** attempt))     # 50ms → 1.6s，累计约 3s
            try:
                os.replace(tmp, str(path))
                return
            except OSError as e:
                if getattr(e, "winerror", None) not in (5, 32):
                    raise
                last = e
        # 第 3 级：挪开目标再替换（可回滚）。
        # ★ 绝不"先 unlink 目标再 replace"：若 unlink 成功而 replace 失败，
        #   外层 except 会删掉 tmp → 新旧内容双双消失，用户数据彻底丢失。
        #   这里改成 rename 到备份名（原子），替换失败就把备份挪回来。
        bak = path.with_name(path.name + ".eco-delbak")
        try:
            if path.exists():
                # ★ v2.5.2 修复（盲实测驱动）：第 3 级兜底原先直接 os.replace，
                #   撞 WinError 5/32 就整体抛错。实测在读者持续高频读的压测下，
                #   前两级退避已争取约 8 秒仍未兜住，这里自己也会撞同一个错
                #   （约 6~8% 的写入失败率，26 次运行中 2 次）。
                #   注意：核心承诺 bad_reads==0 从未被打破——失败的是写入完成度，
                #   不是数据完整性。加上与第 2 级同样的退避即可消除。
                for _bak_attempt in range(6):
                    try:
                        os.replace(str(path), str(bak))
                        break
                    except OSError as e:
                        if getattr(e, "winerror", None) not in (5, 32):
                            raise
                        if _bak_attempt == 5:
                            raise
                        time.sleep(0.05 * (2 ** _bak_attempt))
            try:
                os.replace(tmp, str(path))
            except OSError as e:
                # 替换失败 → 回滚旧文件，保住用户数据
                if bak.exists():
                    try:
                        os.replace(str(bak), str(path))
                    except OSError:
                        pass          # 回滚都失败：至少留 bak 让用户手工恢复
                raise e               # 就地重抛，保留真实失败原因与栈（不重抛旧的 last）
            try:
                bak.unlink(missing_ok=True)
            except OSError:
                pass
            return
        except OSError as e:
            raise e
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def slug_of(text: str) -> str:
    """从文本生成文件名 slug（中文保留，截断+哈希防重）。"""
    s = re.sub(r"[\s\W_]+", "-", text.strip())[:40].strip("-")
    if not s:
        s = "mem"
    return f"{s}-{hashlib.md5(text.encode('utf-8')).hexdigest()[:6]}"


def norm(s: str) -> str:
    """规范化：去空白与标点（相似比较用；中文保留）。"""
    return re.sub(r"[\s\W_]+", "", s)
