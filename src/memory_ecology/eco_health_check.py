#!/usr/bin/env python3
"""生态体检报告生成器 v1.1（正式版只读工具，设计稿 v0.8 P2）。

变更记录：
- v2（一测 16 项修复）：4.2 过滤先于截断/执行摘要/健康评分/悬空引用/双口径/术语速览
- v3（二测 14 项修复）：备份标记按类型分组/记忆内容级重复/评分模型补全/硬编码清除
- v3.1（复查 4 项）：workflows 口径声明/动态体量/截断标注/cron 解包容错
- v1.0 正式版（三测准入评审 10 场景×10 轮）：related 含键口径/记忆长度虚报/版本标注/4.2 截断声明/USER 展示 + 行动闭环 + 阈值告警/扣分分级/趋势观测
- v1.1（正式版一测 20 场景×5 轮，30+ 项）：
  P0：空目录除零崩溃/阈值告警时区死代码（naive-aware 混减）/枢纽并列截断实现/表格 | 转义
  P1：趋势历史 append+model 字段/gateway 状态维度/悬空引用 optional-skills 自动核实/备份 .bak- 模式/字段类型容错/记忆指针化提示
  P2：footer 措辞修正/失效引注修正/批次算数动态/文案硬编码动态化/测试痕迹弱化/词频白名单披露

只读保证：不修改技能/记忆/cron/workflows 等生态文件；产物为报告文件 + 评分历史（报告目录区）。

用法: python eco_health_check.py
"""
import datetime
import json
import math
import os
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import yaml

from lib.config import hermes_root

HERMES_HOME = hermes_root()  # 生态根（lib.config 派生，跟随安装位置）
SKILLS_DIR = HERMES_HOME / "skills"
MEMORIES_DIR = HERMES_HOME / "memories"
CRON_JOBS = HERMES_HOME / "cron" / "jobs.json"
WORKFLOWS_DIR = Path.home() / ".memory-ecology" / "workflows"
DESIGNS_DIR = Path.home() / ".memory-ecology" / "designs"
DESKTOP = Path.home() / "Desktop"
SCORE_HISTORY = DESIGNS_DIR / "eco_score_history.json"
MEM_MAX_CHARS = 2550  # v2.1.1 ③ 口径对齐：管理线 2550（原 2200 为 v1 体检旧参，2200-2550 区间误报「超限」）

# v0.8 合法状态组合矩阵（status \ fate）——未显式声明的默认 active/retained
LEGAL_MATRIX = {
    ("active", "retained"): True,
    ("dormant", "retained"): True,
    ("dormant", "superseded"): True,
    ("dormant", "archived"): True,
    ("frozen", "retained"): True,
    ("frozen", "archived"): True,
}
REQUIRED_FIELDS = ["name", "description", "version", "status", "fate", "domain", "environment"]
CONDITIONAL_FIELDS = {
    "merged_into": lambda s: s["fate"] == "merged",
    "evolved_from": lambda s: False,
}
BACKUP_PATTERN = re.compile(r"(broken[_-]|备份|\.bak|\.old$|_backup|-\d{6,8}$)", re.I)
SOURCE_EXTS = {".py", ".js", ".ts", ".sh", ".ps1", ".mjs", ".cjs"}
# 词频模式（v2：仅标注"疑似"，须配合内容级检测）
KEYWORD_PATTERNS = [
    ("python", "语言"), ("data", "数据"), ("report", "文档"),
    ("model", "模型"), ("file", "文件"), ("user", "用户"),
]
# 内容级重复检测：跨文件公共片段最小长度（v3 下调，真实重复多为 10-40 字符短语）
CONTENT_DUP_MIN_LEN = 10
# 备份异常大小比（max/min 超过此值标记疑似空库/截断）
BACKUP_SIZE_RATIO_ALERT = 20


def _as_list(v):
    """v1.1：字段类型容错（requires/related_skills/tags 非列表时归一）。"""
    if isinstance(v, list):
        return v
    if v is None:
        return []
    return [v]


def parse_frontmatter(path: Path):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {}, "", f"读取失败: {e}"
    if not text.startswith("---"):
        return {}, "", "无 frontmatter 分隔符"
    end = text.find("\n---", 3)
    if end == -1:
        return {}, "", "frontmatter 未闭合"
    fm = text[3:end].strip()
    try:
        meta = yaml.safe_load(fm) or {}
        if not isinstance(meta, dict):
            return {}, "", "frontmatter 非映射"
        return meta, text[end + 4:], None
    except Exception as e:
        return {}, "", f"YAML 解析失败: {e}"


def scan_skills():
    skills, bad = [], []
    for p in sorted(SKILLS_DIR.rglob("SKILL.md")):
        rel = p.relative_to(SKILLS_DIR)
        meta, body, err = parse_frontmatter(p)
        if err:
            bad.append((str(rel), err))
            continue
        hermes_meta = meta.get("metadata", {}).get("hermes", {}) if isinstance(meta.get("metadata"), dict) else {}
        skills.append({
            "path": str(rel),
            "name": meta.get("name", rel.parent.name),
            "description": str(meta.get("description", ""))[:200],
            "version": meta.get("version"),
            "status": meta.get("status"),
            "fate": meta.get("fate"),
            "domain": meta.get("domain"),
            "environment": meta.get("environment"),
            "merged_into": meta.get("merged_into"),
            "evolved_from": meta.get("evolved_from"),
            "requires": _as_list(meta.get("requires")),
            "related": _as_list(hermes_meta.get("related_skills")),
            # v1.0：记录 frontmatter 是否显式含 related_skills 键（修复「含键=全部」口径失实）
            "has_related_key": "related_skills" in hermes_meta,
            "tags": _as_list(hermes_meta.get("tags")),
            "has_tolerance": "tolerance" in meta,
            "file_size": p.stat().st_size,
            "mtime": p.stat().st_mtime,
        })
    return skills, bad


def state_machine_check(skills):
    undeclared, illegal = [], []
    for s in skills:
        st = s["status"] or "active"
        fa = s["fate"] or "retained"
        if s["status"] is None or s["fate"] is None:
            undeclared.append((s["name"], s["status"], s["fate"]))
        if (st, fa) not in LEGAL_MATRIX:
            illegal.append((s["name"], st, fa))
    return undeclared, illegal


def state_distribution(skills):
    dist = {}
    for s in skills:
        key = (s["status"] or "active(默认)", s["fate"] or "retained(默认)")
        dist[key] = dist.get(key, 0) + 1
    return dist


def completeness(skills):
    missing = {}
    for f in REQUIRED_FIELDS:
        cnt = sum(1 for s in skills if s.get(f) is None or s.get(f) == "")
        if cnt:
            missing[f] = cnt
    cond_missing = {}
    for f, cond in CONDITIONAL_FIELDS.items():
        cnt = sum(1 for s in skills if cond(s) and (s.get(f) is None or s.get(f) == ""))
        if cnt:
            cond_missing[f] = cnt
    cond_triggered = {f: sum(1 for s in skills if cond(s)) for f, cond in CONDITIONAL_FIELDS.items()}
    return missing, cond_missing, cond_triggered


def esc(s):
    """v1.1：表格单元格转义（防 | 拆裂 markdown 表格）。"""
    return str(s).replace("|", "\\|")


def overlap_candidates(skills, top_n=15):
    """非同名描述相似度候选（v1.1：长度预筛——描述长度比 >2.5 的对直接跳过，SequenceMatcher 对长度悬殊文本 ratio 必低）。"""
    pairs = []
    for i in range(len(skills)):
        a_len = len(skills[i]["description"])
        if a_len == 0:
            continue
        for j in range(i + 1, len(skills)):
            a, b = skills[i], skills[j]
            if a["name"] == b["name"]:
                continue
            b_len = len(b["description"])
            if b_len == 0 or max(a_len, b_len) / min(a_len, b_len) > 2.5:
                continue
            ratio = SequenceMatcher(None, a["description"].lower(), b["description"].lower()).ratio()
            if ratio >= 0.35:
                pairs.append((ratio, a["name"], b["name"], a["path"], b["path"]))
    pairs.sort(key=lambda x: (-x[0], x[1]))
    return pairs[:top_n], len(pairs)


def duplicate_instances(skills):
    by_name = {}
    for s in skills:
        by_name.setdefault(s["name"], []).append(s)
    return {k: v for k, v in by_name.items() if len(v) > 1}


def count_duplicate_files(dups):
    """目录级冗余计数（含辅助文件）：对每组副本，统计非主路径目录的全部文件数。"""
    total = 0
    for name, instances in dups.items():
        paths = [Path(s["path"]) for s in instances]
        # 主路径 = 顶层（不在 cc-switch 等子分类内）；其余视为冗余
        main = min(paths, key=lambda p: len(p.parts))
        for p in paths:
            if p != main:
                full = SKILLS_DIR / p.parent
                if full.is_dir():
                    total += sum(1 for f in full.rglob("*") if f.is_file())
                else:
                    total += 1
    return total


def scan_workflows():
    """扫描 workflows（v3.1：统计磁盘文件数与无 frontmatter 数，声明口径）。"""
    wfs = []
    disk_count, skipped = 0, 0
    if WORKFLOWS_DIR.is_dir():
        for p in sorted(WORKFLOWS_DIR.glob("*.md")):
            disk_count += 1
            meta, _, err = parse_frontmatter(p)
            if err:
                skipped += 1
                continue
            wfs.append({
                "name": meta.get("name", p.stem),
                "description": str(meta.get("description", ""))[:150],
                "tags": meta.get("tags") or [],
                "tools": meta.get("tools") or [],
            })
    return wfs, disk_count, skipped


def reference_graph(skills, wfs):
    """in-degree/out-degree 双列 + 悬空引用校验。"""
    names = {s["name"] for s in skills}
    in_degree, out_degree = {}, {}
    for s in skills:
        in_degree.setdefault(s["name"], 0)
        out_degree[s["name"]] = len(s["requires"] + s["related"])
    for s in skills:
        for ref in s["requires"] + s["related"]:
            in_degree[ref] = in_degree.get(ref, 0) + 1
    hubs_all = sorted(in_degree.items(), key=lambda x: (-x[1], x[0]))
    if hubs_all:
        threshold = hubs_all[9][1] if len(hubs_all) >= 10 else hubs_all[-1][1]
        hubs = [h for h in hubs_all if h[1] >= threshold]
    else:
        hubs = []
    # v1.1：悬空引用自动核实（optional-skills 可选区存在性）
    opt = HERMES_HOME / "hermes-agent" / "optional-skills"
    opt_names = {p.name for p in opt.rglob("*") if p.is_dir()} if opt.is_dir() else set()
    dangling = [(ref, cnt, ref in opt_names) for ref, cnt in in_degree.items()
                if cnt > 0 and ref not in names]
    # workflows 提及（口径声明：frontmatter name/description/tags 文本匹配；v2 修复：单词边界防短名误命中）
    wf_text = "\n".join(w["name"] + " " + w["description"] + " " + " ".join(w["tags"]) for w in wfs)
    mentioned = []
    for s in skills:
        # 单词边界匹配：防 "do" 等短名被动词/介词误命中（一测发现的坑）
        n = len(re.findall(r"(?<![A-Za-z0-9_-])" + re.escape(s["name"]) + r"(?![A-Za-z0-9_-])", wf_text))
        if n > 0:
            mentioned.append((n, s["name"]))
    mentioned.sort(reverse=True)
    return hubs, mentioned, dangling, out_degree


def memory_check():
    """字节 + 字符双口径；词频标注；跨文件内容级重复检测。

    v3 修复（二测发现 0/4 假阴性）：阈值 40→10、取消 120 字符截断、
    容忍改写（滑动窗口完全匹配 + 最长匹配长度报告）。
    """
    out = []
    texts = {}
    for name in ("MEMORY.md", "USER.md"):
        p = MEMORIES_DIR / name
        if p.exists():
            raw = p.read_text(encoding="utf-8", errors="replace")
            texts[name] = raw
            out.append((name, p.stat().st_size, len(raw)))
    # 词频（疑似，需人工复核）
    keywords = {}
    all_text = "\n".join(texts.values())
    for kw, label in KEYWORD_PATTERNS:
        cnt = all_text.count(kw)
        if cnt >= 2:
            keywords[kw] = (cnt, label)
    # 内容级重复：跨文件去空白滑动窗口（10 字符片段互查，容忍语序/用词差异）
    content_dups = []
    if len(texts) == 2:
        segs_a = [re.sub(r"\s+", "", s) for s in texts["MEMORY.md"].split("§") if len(re.sub(r"\s+", "", s)) >= CONTENT_DUP_MIN_LEN]
        segs_b = [re.sub(r"\s+", "", s) for s in texts["USER.md"].split("§") if len(re.sub(r"\s+", "", s)) >= CONTENT_DUP_MIN_LEN]
        for sa in segs_a:
            best = 0
            for sb in segs_b:
                # 双向滑动窗口：sa 的每段 10 字符窗口是否出现在 sb 中
                for i in range(len(sa) - CONTENT_DUP_MIN_LEN + 1):
                    window = sa[i:i + CONTENT_DUP_MIN_LEN]
                    if window in sb:
                        # 扩展匹配长度（v1.0 修复：while 边界防 +1 虚报）
                        j = i + CONTENT_DUP_MIN_LEN
                        while j <= len(sa) and sa[i:j] in sb:
                            j += 1
                        best = max(best, j - 1 - i)
            if best >= CONTENT_DUP_MIN_LEN:
                content_dups.append((sa[:60], best))
    # 去重并截断展示
    seen, dedup = set(), []
    for frag, ln in content_dups:
        if frag not in seen:
            seen.add(frag)
            dedup.append((frag, ln))
    return out, keywords, dedup


def backup_census():
    """备份普查（v3：深度口径修复 + 按类型分组异常标记）。

    - 深度：目录深度 >2 时跳过（含文件扫描），修复 v2 深度 3 文件被误收录
    - 异常标记：按类型分组比较（同类型内 max/min > 20 倍才标记），修复 v2 全局比较标错对象
    """
    roots = [HERMES_HOME, Path.home() / ".hermes", DESKTOP]
    found = []
    for root in roots:
        if not root.is_dir():
            continue
        for dirpath, dirs, files in os.walk(root):
            depth = len(Path(dirpath).relative_to(root).parts)
            if depth > 2:
                dirs[:] = []
                continue
            for fn in files:
                # v1.1：排除 SQLite WAL 边车文件（0 字节正常，非备份本体）
                if fn.endswith(("-wal", "-shm")):
                    continue
                if BACKUP_PATTERN.search(fn) and Path(fn).suffix.lower() not in SOURCE_EXTS:
                    p = Path(dirpath) / fn
                    found.append({
                        "path": str(p),
                        "size": p.stat().st_size if p.exists() else 0,
                        "mtime": p.stat().st_mtime if p.exists() else 0,
                    })
    # 按类型分组的异常大小标记（同类型内 max/min > 20 倍 → 小者标记）
    def classify(p):
        if "state.db" in p:
            return "系统备份"
        if "project" in p:
            return "项目备份"
        if "妹居物语" in p:
            return "游戏存档"
        return "其他"
    by_type = {}
    for f in found:
        by_type.setdefault(classify(f["path"]), []).append(f)
    for typ, items in by_type.items():
        if len(items) < 2:
            continue
        sizes = sorted(i["size"] for i in items)
        if sizes[-1] > 0 and sizes[-1] / max(sizes[0], 1) > BACKUP_SIZE_RATIO_ALERT:
            for i in items:
                if i["size"] <= sizes[0] * 2:
                    i["flag"] = f"疑似异常（同类型内最大差 >{BACKUP_SIZE_RATIO_ALERT} 倍）"
    return found


def cron_health():
    if not CRON_JOBS.exists():
        return None, ([], [])
    try:
        data = json.loads(CRON_JOBS.read_text(encoding="utf-8"))
    except Exception:
        return "corrupt", ([], [])
    jobs = data.get("jobs", [])
    errs = [j for j in jobs if j.get("last_status") == "error"]
    oks = [j for j in jobs if j.get("last_status") == "ok"]
    return jobs, (errs, oks)


def gateway_status():
    """v1.1：检测 cron gateway 是否运行（hermes cron status，超时 8s；失败返回未知）。"""
    try:
        import subprocess
        r = subprocess.run(["hermes", "cron", "status"], capture_output=True, text=True, timeout=8)
        out = (r.stdout or "") + (r.stderr or "")
        low = out.lower()
        if "not running" in low or "won't fire" in low or "will not fire" in low:
            return "⚠️ 未运行（cron 任务不会自动触发）"
        if r.returncode == 0 and ("running" in low or "scheduled" in low or "ok" in low):
            return "✅ 运行中"
        return f"状态未知（exit {r.returncode}）"
    except Exception as e:
        return f"状态未知（{type(e).__name__}）"


def health_score(skills, dups, dangling, ver_rate, missing, cron_errs, mem_over, backup_flags):
    """健康评分 0-100 + 状态灯（v3：补 cron/记忆/备份维度 + 分项构成）。"""
    items = []  # (扣分, 原因)
    if dups:
        items.append((15, f"重复副本 {len(dups)} 组"))
    if ver_rate < 0.9:
        items.append((10, f"版本覆盖率 {ver_rate * 100:.0f}% <90%"))
    field_miss = sum(missing.values()) if missing else 0
    if field_miss > 0:
        # v1.0：扣分分级（悬崖效应修正）——缺失 >100 扣 15，>10 扣 10，>0 扣 5
        if field_miss > 100:
            items.append((15, f"必需字段缺失 {field_miss} 项"))
        elif field_miss > 10:
            items.append((10, f"必需字段缺失 {field_miss} 项"))
        else:
            items.append((5, f"必需字段缺失 {field_miss} 项"))
    if dangling:
        items.append((10, f"悬空引用 {len(dangling)} 个"))
    if cron_errs:
        items.append((10, f"cron 报错 {len(cron_errs)} 个"))
    if mem_over:
        items.append((5, "MEMORY 超字符上限"))
    if backup_flags:
        items.append((5, f"备份异常 {len(backup_flags)} 个"))
    score = max(100 - sum(d for d, _ in items), 0)
    if score >= 80:
        light, verdict = "🟢 健康", "骨架健康"
    elif score >= 60:
        light, verdict = "🟡 亚健康", "可用但有欠账"
    else:
        light, verdict = "🔴 欠健康", "需优先治理"
    detail = "、".join(f"{r}(-{d})" for d, r in items) if items else "无"
    return score, light, verdict, detail


def fmt_size(n):
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f}MB"
    if n >= 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n}B"


def build_report():
    now = datetime.datetime.now().astimezone()
    L = []
    L.append("# 生态体检报告（正式版 v1.1 · 只读）")
    L.append("")
    L.append(f"- 生成时间: {now.strftime('%Y-%m-%d %H:%M')}")
    L.append(f"- 采集时点: 本报告为**快照**——生成后生态可能变化（实测数分钟内新增技能），以生成时刻数据为准")
    L.append("- 范围: 技能生态 / 记忆 / workflows / 备份普查 / cron 健康")
    L.append("- 模式: **只读**——本报告不修改任何生态文件，所有动作为建议")
    L.append("")

    skills, bad = scan_skills()
    names = {s["name"] for s in skills}
    dups = duplicate_instances(skills)
    dup_files = count_duplicate_files(dups)
    ver_rate = (sum(1 for s in skills if s["version"]) / len(skills)) if skills else 0.0
    missing, cond_missing, cond_triggered = completeness(skills)
    wfs, wf_disk, wf_skipped = scan_workflows()
    hubs, mentioned, dangling, out_degree = reference_graph(skills, wfs)
    mem_out, mem_kw, mem_dups = memory_check()
    backs = backup_census()
    jobs, (cron_errs, cron_oks) = cron_health()
    mem_over = any(name == "MEMORY.md" and chars > MEM_MAX_CHARS for name, size, chars in mem_out)
    backup_flags = [b for b in backs if b.get("flag")]
    score, light, verdict, score_detail = health_score(skills, dups, dangling, ver_rate, missing,
                                                       cron_errs, mem_over, backup_flags)

    # ===== 0. 执行摘要 =====
    L.append("## 0. 执行摘要")
    L.append("")
    L.append(f"- 健康评分: **{score}/100**（{light}）——{verdict}（[v3 模型，跨版本不可比]）")
    # v1.1：趋势观测（读历史列表，同模型对比；append 保留 20 条）
    history = []
    if SCORE_HISTORY.exists():
        try:
            data = json.loads(SCORE_HISTORY.read_text(encoding="utf-8"))
            if isinstance(data, list):
                history = data
        except Exception:
            history = []
    prev_score = None
    for h in reversed(history):
        if isinstance(h, dict) and h.get("model") == "v3":
            prev_score = h
            break
    if prev_score:
        delta = score - prev_score["score"]
        L.append(f"- 上次评分: {prev_score.get('score')}（{prev_score.get('time', '?')}）→ 本次 {score}（Δ{delta:+d}）")
    else:
        L.append("- 上次评分: 无（首测基线，此后同模型内可对比趋势）")
    L.append(f"- 分项: {score_detail}")
    L.append(f"- 一句话: {len(skills)} 个 SKILL.md / {len(names)} 个唯一技能，冗余 {len(dups)} 组；元数据欠账为主")
    L.append(f"- **最优先行动**: {'重复副本去重（' + str(len(dups)) + ' 组，见 §4.1）' if dups else '无紧急项'}")
    L.append("- 今日行动: 看 §9 建议表 P0 行（需你动手：文件操作）；P1/P2 多为维护流程项，数字焦虑不必——本报告是体检不是病危通知")
    L.append("- 存档说明: 报告同时输出桌面（查看）与报告目录（版本存档），两份内容一致")
    L.append("- 术语速览: status=活性状态 / fate=存续命运 / domain=领域 / frontmatter=技能 YAML 头部 / mtime=文件修改时间 / md5=内容校验值 / in-degree=被引用数 / 自声明=技能自己的 related 声明 / 触发=按需字段需填写的场景 / 缺口=需填但未填的数量 / 指针化=记忆条目改为指向技能而非复制内容 / fallback 分类=元数据缺失时按目录兜底 / per-file 容错=坏文件单独跳过不阻断全库 / P0=立即 / P1=近期 / P2=顺带 / P4=frontmatter 补齐阶段 / L1=低风险标记建议 / L4=删除动作（禁用） / protected.md=保护区清单 / 口径=统计规则声明")
    L.append("")

    # ===== 1. 横截面 =====
    L.append("## 1. 横截面")
    L.append("")
    L.append(f"- SKILL.md 文件数: **{len(skills)}**（坏文件 {len(bad)} 个——坏文件走 per-file 容错，不参与后续判定）")
    L.append(f"- 唯一技能名: **{len(names)}**（重复副本 {len(dups)} 组）")
    L.append(f"- 冗余口径: SKILL.md 级 {sum(len(v) - 1 for v in dups.values())} 个文件 / **目录级 {dup_files} 个文件**（含辅助文件，口径修正）")
    L.append(f"- 版本覆盖率: **{sum(1 for s in skills if s['version'])}/{len(skills)}** ({ver_rate * 100:.0f}%)（目标 ≥90%）")
    L.append(f"- 状态声明率: status {sum(1 for s in skills if s['status'])}/{len(skills)}，fate {sum(1 for s in skills if s['fate'])}/{len(skills)}")
    dom = {}
    for s in skills:
        dom[s["domain"] or "未标注"] = dom.get(s["domain"] or "未标注", 0) + 1
    L.append(f"- domain 分布: {dict(sorted(dom.items(), key=lambda x: -x[1]))}")
    if not any(s["domain"] for s in skills):
        # v3：domain 全未标注时给目录级 fallback 分类（顶层分类目录的 SKILL.md 数）
        cat = {}
        for s in skills:
            top = s["path"].split("\\")[0] if "\\" in s["path"] else "(根)"
            cat[top] = cat.get(top, 0) + 1
        top_cats = dict(sorted(cat.items(), key=lambda x: -x[1])[:8])
        L.append(f"  - 目录级 fallback 分类（top8）: {top_cats}")
    L.append(f"- tolerance 标注: {sum(1 for s in skills if s['has_tolerance'])} 个（可选字段，写了才参与排序）")
    rel_declared = sum(1 for s in skills if s.get("has_related_key"))
    rel_nonempty = sum(1 for s in skills if s["related"])
    rel_empty = sum(1 for s in skills if s.get("has_related_key") and not s["related"])
    rel_missing = len(skills) - rel_declared
    L.append(f"- requires 声明: {sum(1 for s in skills if s['requires'])} 个技能 / related_skills 非空声明: {rel_nonempty} 个技能（口径：非空列表；frontmatter 含键 {rel_declared}，其中显式空列表 {rel_empty}，未声明 {rel_missing}）")
    if bad:
        L.append(f"- ⚠️ 坏文件清单: {bad[:5]}")
    L.append("")

    # ===== 2. 状态机 =====
    L.append("## 2. 状态机（活性轴 × 存续轴）")
    L.append("")
    L.append("合法组合矩阵（v0.8 §4.1）：")
    L.append("")
    L.append("| status \\ fate | retained | superseded | merged | archived |")
    L.append("|---|---|---|---|---|")
    L.append("| active | ✓ | ✗ | ✗ | ✗ |")
    L.append("| dormant | ✓ | ✓ | ✗ | ✓ |")
    L.append("| frozen | ✓ | ✗ | ✗ | ✓ |")
    L.append("")
    L.append("- merged 为**过渡态**（合并即转 dormant/archived，非驻留）；deleted 仅「失效 + 用户确认」（L4 未启用）")
    undeclared, illegal = state_machine_check(skills)
    L.append(f"- 未显式声明 status/fate: **{len(undeclared)}** 个（默认 active/retained 参与判定；P4 补齐）")
    L.append(f"- 非法组合: **{len(illegal)}** 个" + ("" if not illegal else f"（{illegal[:10]}）"))
    dist = state_distribution(skills)
    L.append(f"- 实际分布: {dict(sorted(dist.items(), key=lambda x: -x[1]))}")
    L.append("")

    # ===== 3. 完整度 =====
    L.append("## 3. frontmatter 完整度")
    L.append("")
    if missing:
        for f, cnt in missing.items():
            L.append(f"- 缺必需字段 `{f}`: {cnt} 个技能")
    else:
        L.append("- ✅ 必需字段全覆盖")
    L.append(f"- 按需字段: merged_into 触发 {cond_triggered['merged_into']} / 缺口 {cond_missing.get('merged_into', 0)}，evolved_from 触发 {cond_triggered['evolved_from']} / 缺口 {cond_missing.get('evolved_from', 0)}（v3：触发/缺口双列，防误读为漏检）")
    if cond_missing:
        for f, cnt in cond_missing.items():
            L.append(f"- ⚠️ 缺按需字段 `{f}`: {cnt} 个")
    L.append("")

    # ===== 4. 重叠与重复 =====
    L.append("## 4. 重叠与重复")
    L.append("")
    if dups:
        L.append("### 4.1 同名重复副本（确定的知识重叠）")
        L.append("")
        for k, v in sorted(dups.items()):
            main = min(v, key=lambda s: len(Path(s["path"]).parts))
            copies = [s for s in v if s != main]
            no_ver = all(c.get("version") is None for c in v)
            ver_note = "（均无 version 字段）" if no_ver else "（含 version 字段）"
            L.append(f"- **{esc(k)}**: 主={esc(main['path'])}，副本={', '.join(esc(c['path']) for c in copies)}{ver_note}")
        L.append("")
        L.append(f"> 判据（v0.8 §5）：同源知识重叠 → 合并/去重候选。**建议：保留顶层主副本，删除 cc-switch\\ 整目录（{dup_files} 文件，含辅助文件）**——cc-switch 为批量镜像（mtime 统一、md5 全一致），无独有内容；先移备份区验证 7 天再删。")
        L.append("")
    pairs, pair_total = overlap_candidates(skills)
    L.append(f"### 4.2 描述相似度候选（top 15，共 {pair_total} 对 ≥0.35，非同名；算法 = difflib SequenceMatcher，模板化描述可能虚高）")
    L.append("")
    if pairs:
        L.append("| 相似度 | 技能 A | 技能 B | 路径 A | 路径 B |")
        L.append("|---|---|---|---|---|")
        for ratio, a, b, pa, pb in pairs:
            L.append(f"| {ratio:.2f} | {esc(a)} | {esc(b)} | {esc(pa)} | {esc(pb)} |")
        # v3.1：体量对比动态计算（防硬编码过时）
        size_note = ""
        trio = {s["name"]: s.get("file_size") for s in skills if s["name"] in ("claude-code", "codex", "opencode")}
        if len(trio) == 3:
            size_note = f"（文件大小 {fmt_size(trio['claude-code'])}/{fmt_size(trio['codex'])}/{fmt_size(trio['opencode'])}，差异大）"
        L.append("")
        L.append(f"> 需人工复核：知识重叠（同源）→ 合并候选；功能冗余（异构）→ 保留。≥0.8 的 claude-code/codex/opencode 组为高优先复核对象——三者体量差异大{size_note}，实为功能异构，倾向保留。")
    else:
        L.append("- 无显著的非同名重叠")
    L.append("")

    # ===== 5. 引用图 =====
    L.append("## 5. 引用图")
    L.append("")
    L.append(f"- workflows 登记: {len(wfs)} 个项目（磁盘 {wf_disk} 个 md，其中 {wf_skipped} 个无 frontmatter 未纳入——口径已声明）")
    L.append("- **依赖枢纽候选**（in-degree 前 10；口径：related_skills 引用计数，requires 当前 0；并列按技能名序，截断不隐藏并列）:")
    for name, deg in hubs:
        if deg > 0:
            flag = " ⚠️悬空" if name in {d[0] for d in dangling} else ""
            L.append(f"  - {name}: 被引 {deg} / 自声明 {out_degree.get(name, 0)}{flag}")
    if dangling:
        in_opt_count = sum(1 for _, _, io in dangling if io)
        L.append(f"- ⚠️ **悬空引用 {len(dangling)} 个**（被引用但扫描范围内无此技能；其中 {in_opt_count} 个存在于 optional-skills 可选区）:")
        for ref, cnt, in_opt in dangling:
            opt_note = "（✅ 存在于 hermes-agent\\optional-skills\\，直接补技能即可）" if in_opt else "（⚠️ 无落点，需修复引用或新建）"
            L.append(f"  - {ref}: {cnt} 处引用{opt_note}")
    else:
        L.append("- ✅ 无悬空引用")
    L.append(f"- workflows 提及技能: 命中 {len(mentioned)} 条（口径：frontmatter name/description/tags 文本匹配）")
    for n, name in mentioned[:8]:
        L.append(f"  - {name}: {n} 处")
    L.append("")

    # ===== 6. 记忆体检 =====
    L.append("## 6. 记忆体检")
    L.append("")
    for name, size, chars in mem_out:
        L.append(f"- {name}: {size} 字节 / {chars} 字符（设计上限 {MEM_MAX_CHARS} 字符，决策口径 = 字符）")
    if mem_kw:
        L.append("- ⚠️ 词频标注（口径：预设关键词白名单（config 可调） 双文件合计计数，≥2 次才报，仅**疑似**，需人工复核——同一行内词频≠重复条目）:")
        for kw, (cnt, label) in mem_kw.items():
            L.append(f"  - 「{kw}」出现 {cnt} 次（{label}）")
    if mem_dups:
        L.append("- ⚠️ **跨文件内容级重复**（仅跨文件检测：MEMORY ↔ USER 最长公共片段，去空白后计算；v1.1 声明：N = 去空白后最长匹配字符数，非条目号）:")
        for frag, ln in mem_dups:
            L.append(f"  - MEMORY「{frag}…」↔ USER 最长匹配 {ln} 字符（去空白）")
    else:
        L.append("- 未发现跨文件内容级重复")
    L.append("")

    # ===== 7. 备份普查 =====
    L.append("## 7. 备份普查")
    L.append("")
    L.append(f"- 共发现备份形态: **{len(backs)}** 个")
    L.append("- 扫描白名单: HERMES_HOME / ~/.hermes / 桌面（深度 ≤2）；**跳过: F 盘（移动硬盘，可能离线）、E 盘（网盘下载目录，未纳入）**——'未扫=没有'的风险已声明")
    if backs:
        L.append("")
        L.append("| 路径 | 大小 | mtime | 类型 |")
        L.append("|---|---|---|---|")
        for b in backs:
            flag = b.get("flag", "")
            size = fmt_size(b["size"])
            mt = datetime.datetime.fromtimestamp(b["mtime"]).strftime("%Y-%m-%d") if b["mtime"] else "?"
            # 类型推断
            if "state.db" in b["path"]:
                typ = "系统备份"
            elif "project" in b["path"]:
                typ = "项目备份"
            elif "妹居物语" in b["path"]:
                typ = "游戏存档"
            else:
                typ = "其他"
            L.append(f"| {esc(b['path'])} | {size} | {mt} | {typ}{' ⚠️' + flag if flag else ''} |")
    L.append("")
    L.append("> 用途：备份 = 核心区绝对保护区，待圈定 protected.md 清单。")
    L.append("")

    # ===== 8. cron 健康 =====
    L.append("## 8. cron 健康")
    L.append("")
    if jobs is None or jobs == "corrupt":
        L.append("- jobs.json 不可读")
    else:
        L.append(f"- cron gateway: {gateway_status()}（v1.1 新增维度）")
        L.append(f"- 共 {len(jobs)} 个 cron: ✅ {len(cron_oks)} 正常 / ⚠️ {len(cron_errs)} 报错（频率/类型见下）")
        for e in cron_errs:
            note = ""
            nr = e.get("next_run_at")
            if nr:
                note = f"（下次运行 {str(nr)[:16]} 自动验证）"
            L.append(f"  - ⚠️ {e.get('name')} ({e.get('script')}) last_status=error @ {e.get('last_run_at')}{note}")
            if e.get("last_error"):
                err_txt = e["last_error"]
                trunc = "…(截断)" if len(err_txt) > 120 else ""
                L.append(f"    last_error: {err_txt[:120]}{trunc}")
            if e.get("next_run_at"):
                L.append(f"    next_run: {e['next_run_at']}")
        L.append("")
        L.append("| cron | 频率 | 类型 | last_run | next_run | last_status |")
        L.append("|---|---|---|---|---|---|")
        for j in jobs:
            freq = j.get("schedule", {}).get("display", "?")
            typ = "脚本(no_agent)" if j.get("no_agent") else "LLM"
            st = j.get("last_status", "?")
            # v1.0：>7 天未运行阈值告警（周任务漏跑检测）
            stale = ""
            lr = j.get("last_run_at")
            if lr:
                try:
                    lr_dt = datetime.datetime.fromisoformat(str(lr))
                    if (now - lr_dt).total_seconds() > 7 * 86400:
                        stale = " ⚠️>7天未运行"
                except Exception:
                    pass
            L.append(f"| {esc(j.get('name'))} | {freq} | {typ} | {str(j.get('last_run_at'))[:16]} | {str(j.get('next_run_at'))[:16]} | {st}{stale} |")
    L.append("")

    # ===== 9. 建议清单 =====
    L.append("## 9. 建议清单（L1 级，仅建议，未执行）")
    L.append("")
    L.append("| 优先级 | 建议 | 动作 | 验证方式 |")
    L.append("|---|---|---|---|")
    if dups:
        L.append(f"| P0 | 重复副本 {len(dups)} 组去重 | 保留顶层主副本；cc-switch\\ 移入备份区 `~/.memory-ecology/backup_staging/` 验证 7 天后删除 | 删除后重跑本报告，{len(skills)}→{len(names)}、冗余清零 |")
    if dangling:
        in_opt_count = sum(1 for _, _, io in dangling if io)
        if in_opt_count:
            L.append(f"| P1 | 悬空引用补技能（{in_opt_count} 个存在于 optional-skills） | 从可选区复制/软链入技能库 | 引用图无红标 |")
        if len(dangling) - in_opt_count:
            L.append(f"| P0 | 悬空引用排查（{len(dangling) - in_opt_count} 个无落点） | 修复引用或新建技能 | 引用图无红标 |")
    field_parts = "、".join(f"{f} {c}" for f, c in missing.items()) if missing else "无"
    batch_n = math.ceil(len(skills) / 4)
    tail_n = len(skills) - 3 * batch_n
    L.append("| P1 | 补齐 frontmatter（缺: " + field_parts + f"） | 按 4 批执行（每批 {batch_n} 个，尾批 {tail_n}，共 {len(skills)} 技能），每批 git 快照（试点区：副本保留区，不干扰 P0 删除路径） | 重跑 §1/§2/§3 指标改善 |")
    L.append("| P1 | 圈定 protected.md 核心区 | 备份清单 + 核心技能清单入档 | md/json 一致性校验 |")
    L.append("| P1 | cron 报错处置（error 任务下次运行自动复核；需 gateway 运行中） | 自动复核 last_status 转 ok；持续 error 则人工排查（含脚本路径解析核查） | 报告 §8 无 error |")
    if backup_flags:
        L.append(f"| P1 | 备份异常处置（{len(backup_flags)} 个疑似异常） | 核对内容完整性（如 json 可解析/库文件可打开，空库判定：0 行业务表），确认异常则补做有效备份 | 报告 §7 无 ⚠️ |")
    L.append("| P1 | 建立备份策略（记忆/索引/cron/配置当前零备份） | 定期备份 MEMORY.md/USER.md/jobs.json/index.db 到备份区，含保留期与校验 | 恢复演练通过 |")
    if mem_dups or mem_over:
        L.append("| P2 | 记忆去重/精简（MEMORY 超字符上限或跨文件重复） | 合并重复条目、指针化（候选：大条目→对应技能，约省 1000+ 字符）；清理前先备份 | MEMORY 字符数回落至上限内 |")
    L.append("")
    L.append("> 术语: P0=立即 / P1=近期 / P2=顺带；L1=低风险标记建议（未执行）")
    L.append("")
    L.append("---")
    L.append("*本报告由 eco_health_check.py v1.1 生成（正式版只读工具），设计稿 v0.8。变更记录：v2（一测 16 项修复）→ v3（二测 14 项修复）→ v3.1（复查 4 项）→ v1.0 正式版（三测准入评审 10 场景×10 轮，P0 五项 = 口径×3+标注+展示）→ v1.1（正式版一测 20 场景×5 轮，30+ 项修复：空目录除零/阈值告警时区死代码/并列截断/表格转义/趋势历史 append/gateway 维度/悬空自动核实/备份模式/字段容错）。*")
    # v1.1：趋势观测——append 记录本次评分（保留最近 20 条，同模型才对比）
    try:
        history.append({"score": score, "time": now.strftime("%Y-%m-%d %H:%M"), "model": "v3"})
        SCORE_HISTORY.write_text(json.dumps(history[-20:], ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return "\n".join(L)


if __name__ == "__main__":
    report = build_report()
    out1 = DESKTOP / "生态体检报告-v1.1.md"
    out2 = DESIGNS_DIR / "生态体检报告-v1.1.md"
    try:
        out1.write_text(report, encoding="utf-8")
        out2.write_text(report, encoding="utf-8")
        print(f"✅ 报告已生成: {out1}")
        print(f"✅ 报告已存档: {out2}")
    except OSError as e:
        # 写入失败容错（V2 修复）：至少输出到 stdout，不静默崩溃
        print(f"⚠️ 报告写入失败: {e}")
        print("--- 报告内容如下（stdout 兜底）---")
        print(report)
        sys.exit(1)
    skills, bad = scan_skills()
    names = {s["name"] for s in skills}
    print(f"   技能文件 {len(skills)} | 唯一技能 {len(names)} | 报告 {len(report)} 字符")
