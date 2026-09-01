"""Memory Ecology 干净房冒烟测试（unittest，零依赖：不依赖 pytest/LLM key/个人数据）。

设计：MEMORY_ECOLOGY_ROOT 环境变量指向临时目录（lib.config 支持），
mock LLM（monkeypatch lib.llm.complete），假数据 fixture——任何机器可跑。

覆盖四道门核心路径：
1. write_gate 写入整合（ADD 路径 → detail 条目创建）
2. eco_quota 巩固/配额（超限 L1 → 挤出落盘）
3. distill_stage 画像蒸馏（观察期候选生成，mock LLM）
4. eco_review 复核（过期 → dormant）

用法: python -m unittest discover tests
"""
import datetime
import os
import pathlib
import sys
import tempfile
import unittest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "src", "memory_ecology")
sys.path.insert(0, SCRIPTS)


def fake_complete(prompt, model=None, max_tokens=None, temperature=None):
    """假 LLM：按 prompt 内容返回不同响应。"""
    if "画像蒸馏器" in prompt:
        return '{"trait": "用户偏好使用开源工具"}'
    return '[{"idx": 0, "type": "semantic", "action": "ADD", "target": "", "note": "fake"}]'


def make_detail(d, name, type_, status="active", verified="2026-08-01", occ=2, sess=2):
    p = d / f"{name}.md"
    p.write_text(f"""---
name: {name}
type: {type_}
status: {status}
occurrences: {occ}
session_count: {sess}
first_seen: 2026-08-01
last_seen: 2026-08-20
valid_time: 
transaction_time: 2026-08-01T10:00:00
last_verified: {verified}
origin_session_id: test
superseded_by: 
---
用户偏好 {name} 相关内容
""", encoding="utf-8")
    return p


class CleanroomTest(unittest.TestCase):

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        os.environ["MEMORY_ECOLOGY_ROOT"] = str(self.tmp)
        # mock LLM（lib.llm.complete 替换）
        import lib.llm as llm
        self._orig_complete = llm.complete
        llm.complete = fake_complete

    def tearDown(self):
        import lib.llm as llm
        llm.complete = self._orig_complete
        os.environ.pop("MEMORY_ECOLOGY_ROOT", None)

    def test_write_gate_add(self):
        """write_gate：无相似候选 → ADD 创建 detail 条目。"""
        import write_gate as wg
        pending = self.tmp / "memories" / "pending"
        detail = self.tmp / "memories" / "detail"
        pending.mkdir(parents=True, exist_ok=True)
        detail.mkdir(parents=True, exist_ok=True)
        (pending / "2026-08-30.md").write_text(
            "- [陈述] 用户昨天完成了项目部署\n", encoding="utf-8")

        cands = wg.parse_candidates(pending)
        self.assertEqual(len(cands), 1)
        wg.add_entry(detail, cands[0]["text"], {"type": "semantic"})
        files = list(detail.glob("*.md"))
        self.assertEqual(len(files), 1)
        self.assertIn("项目部署", files[0].read_text(encoding="utf-8"))

    def test_quota_extrude(self):
        """eco_quota：L1 超 85% → 挤出并落盘 detail。"""
        import eco_quota as q
        detail = self.tmp / "memories" / "detail"
        detail.mkdir(parents=True, exist_ok=True)
        make_detail(detail, "existing", "semantic", occ=1, sess=1)
        l1 = self.tmp / "memories" / "MEMORY.md"
        l1.parent.mkdir(parents=True, exist_ok=True)
        filler = "填充条目内容用于撑大配额，模拟记忆积累的长期沉淀过程，包含环境细节与配置说明。填充条目内容用于撑大配额。"
        entries = ["用户有一个长期项目需要持续跟踪"] + [f"填充{i}: {filler}" for i in range(50)]
        l1.write_text("\n§\n".join(entries) + "\n", encoding="utf-8")

        items = q.load_detail(detail)
        prefixes = q.detail_prefixes(items)
        actions, final = q.process_file(l1, 3000, items, prefixes, set())
        self.assertTrue(actions, "应产生挤出动作")

    def test_distill_candidate(self):
        """distill_stage：达标 semantic 条目 → 观察期候选（mock LLM 措辞）。"""
        import distill_stage as ds
        detail = self.tmp / "memories" / "detail"
        cand = self.tmp / "memories" / "user_candidates"
        detail.mkdir(parents=True, exist_ok=True)
        cand.mkdir(parents=True, exist_ok=True)
        make_detail(detail, "stable", "semantic", occ=3, sess=2)

        eligible = []
        for d in ds.load_detail(detail):
            fm = d["fm"]
            if fm.get("type") == "semantic" and int(fm.get("occurrences", 0)) >= 2 \
                    and int(fm.get("session_count", 0)) >= 2:
                eligible.append(d)
        self.assertEqual(len(eligible), 1)
        trait = ds.llm_trait(eligible[0]["body"])
        self.assertIn("开源工具", trait)  # mock LLM 输出

    def test_review_dormant(self):
        """eco_review：last_verified 超期（>90 天）→ mark_dormant。"""
        import eco_review as rv
        detail = self.tmp / "memories" / "detail"
        detail.mkdir(parents=True, exist_ok=True)
        old = (datetime.date.today() - datetime.timedelta(days=100)).isoformat()
        make_detail(detail, "old-mem", "semantic", verified=old)

        actions, files, fails = rv.scan_detail(detail)
        self.assertTrue(any(a["action"][0] == "mark_dormant" for a in actions))


if __name__ == "__main__":
    unittest.main()
