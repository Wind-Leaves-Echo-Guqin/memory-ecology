"""统一度量与配额常量（PORT_SPEC §4-C 口径单源 + §C memstore 分层）。

L1 计数委托 lib/memstore（parse_l1/chars_of 的唯一实现在 memstore）；
本模块只保留配额常量与触发线。此前四种口径并存（P2-1）由此根治。
"""
from lib.memstore import parse_l1, chars_of, serialize_l1  # noqa: F401 委托 memstore 唯一实现

MEMORY_QUOTA = 3000
USER_QUOTA = 1500
RATIO = 0.85                                # 挤出触发线 = 配额 × 85%
MEMORY_TRIGGER = int(MEMORY_QUOTA * RATIO)  # 2550
USER_TRIGGER = int(USER_QUOTA * RATIO)      # 1275
