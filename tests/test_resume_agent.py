"""ResumeCustomiserAgent：弹药库×JD×累积意见 → 结构化内容 → 渲染 HTML；单轮；红线拦截；
意见持久累积（不因某次不传就消失）。"""
import json

import pytest

from joblander import resume_agent as ra
from joblander.config import Config

CONTENT = {
    "tagline": "AI Engineer",
    "summary": ["定制后的 Summary，对准 JD 的 agent 系统设计。"],
    "experience": [{"company": "ExCo", "position": "AI Engineer", "when": "Singapore · 2020-2024",
                    "note": "", "bullets": ["<strong>FCR +10.1pp</strong>，对准 JD 的 LLM serving 要求"]}],
    "skills": [{"label": "LLM", "value": "Prompt engineering, RAG"}],
    "education": [{"degree": "Ph.D.", "institution": "STUD", "when": "2015-2020", "sub": ""}],
    "publications": [],
    "service": "",
    "changes": ["FCR 战绩前置到第一条，对准 JD 的 LLM serving 要求", "压缩了与 JD 无关的推荐系统段"],
}


class L:
    def __init__(self, content=None):
        self.content = CONTENT if content is None else content
        self.prompts: list[str] = []

    def generate(self, prompt, system=None, json_mode=False):
        self.prompts.append(prompt)
        return json.dumps(self.content, ensure_ascii=False)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    (ws / "03-materials").mkdir(parents=True)
    (ws / "03-materials" / "profile.json").write_text(json.dumps({
        "name": "Alex Doe",
        "contact": [{"text": "alex@example.com",
                    "href": "mailto:alex@example.com"}]}), encoding="utf-8")
    (ws / "03-materials" / "achievement-bank.md").write_text(
        "# 战绩素材库\n## ExCo\n- FCR +10.1pp\n## ⚠️ 素材使用注意\n1. 内部代号转译\n",
        encoding="utf-8")
    (ws / "09-projections").mkdir()
    (ws / "09-projections" / "tracker.json").write_text(json.dumps({"rows": [
        {"Company": "Acme", "Position": "AI Eng"}]}))
    monkeypatch.setattr(ra, "_to_pdf", lambda p: None)     # 测试不真调 Chrome
    return Config(raw={
        "workspace_dir": str(ws),
        "sentinel": {"rules": [{"id": "c", "type": "pattern", "action": "block",
                                "patterns": ["ProjectX"], "why": "internal"}]},
    }, path=tmp_path / "c.yaml")


def test_customise_first_version_and_accumulates_feedback(cfg, tmp_path):
    from joblander import company as cf
    cf.save_upload(cfg, "Acme", "jd.txt", b"need LLM serving and agents", kind="jd")
    llm = L()
    out = ra.customise(cfg, llm, "Acme")
    assert out["version"]["v"] == 1 and out["pdf"] == ""
    p = llm.prompts[0]
    assert "need LLM serving" in p             # JD 进上下文
    assert "FCR +10.1pp" in p                  # 弹药库进上下文
    assert "素材使用注意" in p                  # 内部纪律随库进入
    assert "母版" not in p and "上一版" not in p    # 不再参考母版/上一版 HTML

    saved = (tmp_path / "ws" / "18-companies" / "Acme" / "resume"
             / "resume-v1.html").read_text()
    assert "CUSTOMISATION" not in saved
    assert "alex@example.com" in saved        # 身份固定渲染，定制官管不到
    assert "<strong>FCR +10.1pp</strong>" in saved     # 内容套进固定骨架

    st = ra.resume_state(cfg, "Acme")
    assert st["current"] == "resume-v1" and st["versions"][0]["sentinel"] == "pass"
    assert st["versions"][0]["changes"] == CONTENT["changes"]

    out2 = ra.customise(cfg, llm, "Acme", feedback="agent 一节放最前")
    assert out2["version"]["v"] == 2
    assert "agent 一节放最前" in llm.prompts[1]
    assert ra.resume_state(cfg, "Acme")["current"] == "resume-v2"
    assert ra.resume_state(cfg, "Acme")["feedback"] == ["agent 一节放最前"]
    assert ra.resume_state(cfg, "Acme")["versions"][1]["note"] == "agent 一节放最前"

    out3 = ra.customise(cfg, llm, "Acme")              # 不传新意见，累积集合依然全量带上
    assert out3["version"]["v"] == 3
    assert "agent 一节放最前" in llm.prompts[2]
    assert "基于累积意见重新生成" in ra.resume_state(cfg, "Acme")["versions"][2]["note"]
    assert ra.resume_state(cfg, "Acme")["feedback"] == ["agent 一节放最前"]   # 没有重复


def test_position_discipline_warns_against_bank_team_tags():
    """position 是对外头衔，不是弹药库「团队：...」这类内部标注——防止照抄内部缩写当职位。"""
    assert "不是弹药库段落元信息里的内部团队/角色标注" in ra.RESUME_SYSTEM
    assert "照抄或展开成 position" in ra.RESUME_SYSTEM


def test_render_html_allows_link_in_education_sub():
    """edu.sub 跟 bullets/summary/note 同类——允许 <a href> 等行内标签，不能被转义成字面文本
    （2026-08-14 真实生成里出过：转义后 PDF 上直接印出 &lt;a href=...&gt; 这种破损文本）。"""
    content = dict(CONTENT, education=[{"degree": "Ph.D.", "institution": "STUD", "when": "x",
                                        "sub": 'Cited by <a href="https://x.com">575</a>'}])
    html = ra._render_html(content, {"name": "X", "contact": []})
    assert '<a href="https://x.com">575</a>' in html
    assert "&lt;a href" not in html


def test_customise_omits_company_intel_when_none_exists(cfg):
    """没有尽调/没有通话记录时，情报段整段不出现——不往 prompt 里塞空标签。"""
    llm = L()
    ra.customise(cfg, llm, "Acme")
    assert "该公司情报" not in llm.prompts[0]


def test_customise_pulls_company_intel_from_dossier_and_calls(cfg, tmp_path):
    """尽调摘要 + 时间线「通话」条目摘录进 prompt，且标注"不是事实来源"。"""
    from joblander import company as cf

    (cfg.workspace_dir / "14-dossiers").mkdir(parents=True, exist_ok=True)
    (cfg.workspace_dir / "14-dossiers" / "Acme.json").write_text(json.dumps({
        "summary_md": "该岗位实际看重 applied research 与 harness 能力，尽调独有信号。"},
        ensure_ascii=False), encoding="utf-8")
    cf.timeline_add(cfg, "Acme", kind="call", title="与 HM 通话",
                    content_md="HM 提到团队做 Meta-Harness 项目，偏 applied research。")

    llm = L()
    ra.customise(cfg, llm, "Acme")
    p = llm.prompts[0]
    assert "尽调独有信号" in p and "Meta-Harness" in p
    assert "不是简历事实来源" in p                    # prompt 里的段落标注


def test_customise_sentinel_blocks(cfg, tmp_path):
    bad = dict(CONTENT, experience=[{"company": "ExCo", "position": "x", "when": "x",
                                     "note": "", "bullets": ["we built ProjectX"]}])
    with pytest.raises(ValueError, match="红线拦截"):
        ra.customise(cfg, L(bad), "Acme")
    assert not (tmp_path / "ws" / "18-companies" / "Acme" / "resume").exists()


def test_customise_guards(cfg):
    with pytest.raises(ValueError, match="不是合法定制内容"):
        ra.customise(cfg, L({"tagline": "x"}), "Acme")    # 没 experience
    (cfg.workspace_dir / "03-materials" / "achievement-bank.md").write_text("")
    with pytest.raises(ValueError, match="弹药库为空"):
        ra.customise(cfg, L(), "Acme")


def test_customise_requires_profile(cfg):
    (cfg.workspace_dir / "03-materials" / "profile.json").unlink()
    with pytest.raises(ValueError, match="身份信息不存在"):
        ra.customise(cfg, L(), "Acme")


def test_bank_tail_discipline_survives_prompt(cfg):
    """弹药库文末是「素材使用注意」红线段——大库进 prompt 不许截掉尾部纪律。"""
    ws = cfg.workspace_dir
    big = ("# 战绩素材库\n" + ("- 战绩填充行 x 一二三四五六七八九十\n" * 900)
           + "\n## ⚠️ 素材使用注意\n1. 内部代号转译\n")
    (ws / "03-materials" / "achievement-bank.md").write_text(big, encoding="utf-8")
    llm = L()
    ra.customise(cfg, llm, "Acme")
    assert "素材使用注意" in llm.prompts[0]


def test_reset_clears_versions_and_feedback(cfg, tmp_path):
    ra.customise(cfg, L(), "Acme", feedback="x 意见")
    assert ra.resume_state(cfg, "Acme")["feedback"] == ["x 意见"]
    out = ra.reset(cfg, "Acme")
    assert out["archived"] >= 1
    st = ra.resume_state(cfg, "Acme")
    assert st["current"] == "" and st["versions"] == [] and st.get("feedback") == []
    arch = [d for d in (tmp_path / "ws" / "18-companies" / "Acme" / "resume").iterdir()
            if d.is_dir() and d.name.startswith("archive-")]
    assert arch and any(f.name == "resume-v1.html" for f in arch[0].iterdir())


def _fake_pdf(monkeypatch, pages_seq):
    """假 Chrome：每次转 PDF 吐出 pages_seq 里的下一个 (页数, 末页词数)，记下每次的 HTML。"""
    seq, seen = list(pages_seq), []

    def to_pdf(p):
        seen.append(p.read_text(encoding="utf-8"))
        pdf = p.with_suffix(".pdf")
        pdf.write_bytes(b"%PDF")
        return pdf
    monkeypatch.setattr(ra, "_to_pdf", to_pdf)
    monkeypatch.setattr(ra, "_pdf_pages", lambda pdf: seq.pop(0) if len(seq) > 1 else seq[0])
    return seen


def test_one_page_by_tightening_layout_without_llm(cfg, monkeypatch):
    """超页先逐档收紧版式（零 LLM）；纸张跟 config 走；页数记进版本。"""
    cfg.raw["resume"] = {"paper": "Letter"}
    seen = _fake_pdf(monkeypatch, [(2, 40), (2, 15), (1, 0)])
    llm = L()
    out = ra.customise(cfg, llm, "Acme", jd="need agents")
    assert len(llm.prompts) == 1                                   # 没为一页多花 LLM
    assert out["version"]["pages"] == 1 and len(seen) == 3
    assert "size: letter" in seen[-1] and ra.DENSITY_CSS[2] in seen[-1]
    assert ra.DENSITY_CSS[2] in open(out["html"], encoding="utf-8").read()   # 盘上是能排进一页的那档


def test_layout_floor_overflow_asks_for_one_trim(cfg, monkeypatch):
    """压到最紧仍超页 → 带页数回去让定制官砍一次（只一次）；砍完还超就如实记页数，不报错。"""
    n = len(ra.DENSITY_CSS)
    _fake_pdf(monkeypatch, [(2, 60)] * n + [(1, 0)])
    llm = L()
    out = ra.customise(cfg, llm, "Acme", jd="need agents")
    assert len(llm.prompts) == 2 and "最紧凑的版式下仍然排到了 2 页" in llm.prompts[1]
    assert out["version"]["pages"] == 1

    _fake_pdf(monkeypatch, [(2, 60)])
    llm = L()
    out = ra.customise(cfg, llm, "Acme", jd="need agents")
    assert len(llm.prompts) == 2 and out["version"]["pages"] == 2


def test_paper_setting():
    assert ra._paper(Config(raw={}, path=None)) == "A4"
    assert ra._paper(Config(raw={"resume": {"paper": "letter"}}, path=None)) == "letter"
    assert ra._paper(Config(raw={"resume": {"paper": "Tabloid"}}, path=None)) == "A4"
