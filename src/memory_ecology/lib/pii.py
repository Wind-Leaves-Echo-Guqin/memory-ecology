"""pii — PII 识别 / 掩码 / 敏感度标记（隐私判据 PF1/PF2/PF4 的规则单源）。

分工（与 lib/redact.py）：
  · 本模块面向**机器可读**场景：落库 frontmatter 的 sensitivity、检索命中的
    sensitivity 字段、派生索引/日志的脱敏。返回分类标签与稳定掩码。
  · lib/redact.py 面向**人读/注入面**：把命中替换成「[手机号已脱敏]」这类占位。
    其正则与分类**复用本模块**，避免两套规则各自漂移。

覆盖：中国大陆手机号、邮箱、18 位身份证、16-19 位银行卡、中文住址关键词。
判别要点：
  · 18 位身份证与 16/19 位银行卡不互相吞：身份证要求「地区码 + 合法出生日期」
    （末位 X 直接按身份证），18 位纯数字则先看身份证校验位、再看银行卡 Luhn；
    16/17/19 位一律按银行卡。
  · 不误伤日期/版本号：`2026-10-23`、`2.5.7` 没有 16 位以上连续数字，天然不命中。

零依赖（stdlib only），纯函数，无 IO。
"""
from __future__ import annotations

import datetime
import re

# 稳定输出顺序（sensitivity_of 的标签顺序与 detect 返回顺序都按此表）
LABEL_ORDER = ("phone", "email", "id_card", "bank_card", "address")

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# 18 位：17 位数字 + 数字/X；边界约束避免从更长数字串中截一段
_ID_CAND = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
# 16-19 位连续数字；18 位是否算银行卡由 _classify_long 再判。
# 两侧排除 X/x：否则 18 位身份证（末位 X）会被银行卡规则截走前 17 位。
_BANK_CAND = re.compile(r"(?<![\dXx])\d{16,19}(?![\dXx])")
_MOBILE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")

# 住址：显式标签（地址：xxx）与常见口语（家住xxx）
_ADDR_LABEL = re.compile(
    r"(?P<label>(?:家庭住址|现住址|户籍地址|收货地址|居住地址|住址|地址)\s*[:：]\s*)"
    r"(?P<value>[^\n]{2,80})")
_ADDR_PLAIN = re.compile(
    r"(?P<prefix>家住|居住在|住在)(?P<value>[^\s，。；;、\n]{2,40})")

_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK = "10X98765432"


def _luhn(num: str) -> bool:
    """银行卡 Luhn 校验（纯数字）。"""
    if not num.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(num)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _valid_birth(s: str) -> bool:
    try:
        datetime.date(int(s[6:10]), int(s[10:12]), int(s[12:14]))
    except (ValueError, IndexError):
        return False
    return True


def _valid_id(s: str) -> bool:
    """18 位身份证：地区码非 0、出生日期合法、ISO 7064 校验位正确。

    末位 X/x 的候选即使校验位不符也按身份证处理（普通人手写的示例号常带 X，
    且 17 位数字 + X 不可能是银行卡）；纯 18 位数字才严格校验。
    """
    if not re.fullmatch(r"[1-9]\d{16}[\dXx]", s or ""):
        return False
    if not _valid_birth(s):
        return False
    if s[-1] in "Xx":
        return True
    check = _ID_CHECK[sum(int(c) * w for c, w in zip(s[:17], _ID_WEIGHTS)) % 11]
    return check == s[-1]


def _classify_long(s: str) -> str:
    """16-19 位连续数字 → "id_card" | "bank_card"。

    18 位是唯一可能歧义的长度；先按身份证（日期 + 校验位）判，再按银行卡
    Luhn 判，都不中时按"像不像身份证日期"兜底——宁可掩码过度，不放过 PII。
    """
    if len(s) != 18:
        return "bank_card"
    if s[-1] in "Xx":
        return "id_card"
    if _valid_id(s):
        return "id_card"
    if _luhn(s):
        return "bank_card"
    return "id_card" if _valid_birth(s) else "bank_card"


def _mask_mobile(m: re.Match) -> str:
    s = m.group(0)
    return s[:3] + "****" + s[-2:]


def _mask_email(m: re.Match) -> str:
    s = m.group(0)
    local, _, domain = s.partition("@")
    return (local[:1] or "*") + "***@" + domain


def _mask_id(m: re.Match) -> str:
    s = m.group(0)
    if len(s) <= 8:
        return "***"
    return s[:4] + "*" * (len(s) - 8) + s[-4:]


def _mask_bank(m: re.Match) -> str:
    s = m.group(0)
    return "***" if _classify_long(s) == "bank_card" else s


def _has_id(s: str) -> bool:
    return any(_classify_long(m.group(0)) == "id_card"
               for m in _ID_CAND.finditer(s))


def _has_bank(s: str) -> bool:
    return any(_classify_long(m.group(0)) == "bank_card"
               for m in _BANK_CAND.finditer(s))


def detect(text) -> list[str]:
    """返回命中的 PII 类别（按 LABEL_ORDER，去重）；无 PII → []。"""
    s = "" if text is None else str(text)
    hits = {
        "phone": bool(_MOBILE.search(s)),
        "email": bool(_EMAIL.search(s)),
        "id_card": _has_id(s),
        "bank_card": _has_bank(s),
        "address": bool(_ADDR_LABEL.search(s) or _ADDR_PLAIN.search(s)),
    }
    return [k for k in LABEL_ORDER if hits[k]]


def sensitivity_of(text) -> str:
    """敏感度标记：无 PII → ""；有 → "pii:phone,email"（稳定顺序）。"""
    labels = detect(text)
    return "pii:" + ",".join(labels) if labels else ""


def mask(text) -> str:
    """PII 掩码（稳定、无随机）：

      · 手机号保留首 3 尾 2：13812345678 → 138****78
      · 邮箱保留首字符与域名：admin@example.com → a***@example.com
      · 身份证保留首尾 4 位：11010119900307123X → 1101**********123X
      · 银行卡 / 住址 / 其余：整体替换为 ***（住址保留标签）
    """
    if text is None:
        return ""
    s = str(text)
    if not s:
        return s
    s = _EMAIL.sub(_mask_email, s)
    s = _ADDR_LABEL.sub(lambda m: m.group("label") + "***", s)
    s = _ADDR_PLAIN.sub(lambda m: m.group("prefix") + "***", s)
    # 先身份证后银行卡：身份证已掩码成含 * 的串，不会再被银行卡规则二次吞掉
    s = _ID_CAND.sub(
        lambda m: _mask_id(m) if _classify_long(m.group(0)) == "id_card" else m.group(0),
        s)
    s = _BANK_CAND.sub(_mask_bank, s)
    s = _MOBILE.sub(_mask_mobile, s)
    return s


def redact_by_kind(text, kind: str, replacement: str) -> str:
    """把某一类 PII 替换为给定占位串（lib/redact.py 的输出面用）。"""
    s = "" if text is None else str(text)
    if not s:
        return s
    if kind == "email":
        return _EMAIL.sub(replacement, s)
    if kind == "id_card":
        return _ID_CAND.sub(
            lambda m: replacement if _classify_long(m.group(0)) == "id_card"
            else m.group(0), s)
    if kind == "bank_card":
        return _BANK_CAND.sub(
            lambda m: replacement if _classify_long(m.group(0)) == "bank_card"
            else m.group(0), s)
    if kind == "phone":
        return _MOBILE.sub(replacement, s)
    if kind == "address":
        s = _ADDR_LABEL.sub(replacement, s)
        return _ADDR_PLAIN.sub(replacement, s)
    raise ValueError(f"未知 PII 类别: {kind!r}")
