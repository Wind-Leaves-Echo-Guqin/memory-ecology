#!/usr/bin/env python3
"""scan_sensitive.py — 开源仓库敏感词扫描（发布/推送前把关）。

复用 publish_opensource 的扫描词表（独立副本，不依赖生产脚本）。
qy160 仅允许出现在 LICENSE/README 署名区；其余任何文件出现个人标识 → 退出码 1。

用法: python scripts/scan_sensitive.py [--repo DIR]
pre-push hook 引用：.git/hooks/pre-push → python <repo>/scripts/scan_sensitive.py || exit 1
"""
import argparse
import pathlib
import re
import sys

SCAN_WORDS = ["qy160", "风遗古琴声", "辰汐", "凯文", "卢米安", "吴旭仁", "柳文轩", "张艺轩",
              "咸宁", "黄石", "湖北师大", "湖北师范大学", "原神", "崩铁", "芙宁娜", "星穹铁道",
              "文体部", r"C:\\Users", r"C:/Users", r"D:/convlstm", r"D:\\convlstm"]
ALLOW_QUIET = ("LICENSE", "README.md", "README_CN.md")  # qy160 署名区白名单


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", type=pathlib.Path, default=pathlib.Path(__file__).resolve().parent.parent)
    args = ap.parse_args()

    issues = []
    for f in sorted(args.repo.rglob("*")):
        if not f.is_file():
            continue
        if f.name == "scan_sensitive.py":
            continue  # 词表定义文件必然含词，跳过自身
        if any(part in (".git", "__pycache__", "node_modules") for part in f.parts):
            continue
        if f.suffix.lower() not in (".py", ".md", ".yaml", ".yml", ".txt", ".json", ".toml"):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        allow_q = f.name in ALLOW_QUIET
        for w in SCAN_WORDS:
            if re.search(w, text, re.I):
                if w == "qy160" and allow_q:
                    continue
                rel = f.relative_to(args.repo)
                issues.append(f"{rel}: 含个人标识 [{w}]")

    if issues:
        print(f"❌ 扫描未通过（{len(issues)} 处）：")
        for i in issues[:20]:
            print(f"  {i}")
        return 1
    print("✅ 仓库干净：无个人标识（qy160 仅署名区）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
