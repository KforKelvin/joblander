"""Sourcing 主动侦察（W1 扩展）：偏好、MCF 映射、抓取→评分→提案闭环。全合成，不触网。"""

import json

import pytest

from joblander import sourcing
from joblander.config import Config


@pytest.fixture
def cfg(tmp_path):
    proj = tmp_path / "09-projections"
    proj.mkdir(parents=True)
    (proj / "tracker.json").write_text(json.dumps({"rows": [
        {"Company": "Acme AI", "notion_page_id": "aaa111", "Status": "Applied"}]}))
    c = Config(raw={"workspace_dir": str(tmp_path)}, path=tmp_path / "c.yaml")
    sourcing.save_prefs(c, {"intent": "ML systems", "keywords": ["AI Engineer"],
                            "locations": ["Singapore"], "exclude": ["intern"]})
    return c


MCF_JOB = {
    "uuid": "abc123def456", "title": "AI Engineer",
    "postedCompany": {"name": "Fresh Startup"},
    "salary": {"minimum": 8000, "maximum": 12000, "type": {"salaryType": "Monthly"}},
    "metadata": {"newPostingDate": "2099-01-01",
                 "jobDetailsUrl": "https://example.test/job/abc123def456"},
}


class FitLLM:
    def generate(self, prompt, system=None, json_mode=False):
        assert "ML systems" in prompt          # 偏好进入了评分上下文
        return json.dumps({"fit": 4, "why": "方向对口", "flags": []})


DETAIL = {"description": "<p>Requires <b>8 years</b> of ML and fluent Thai</p>",
          "skills": [{"skill": "LLM"}, {"skill": "RAG"}],
          "minimumYearsExperience": 8}


def test_prefs_roundtrip_and_links(cfg):
    prefs = sourcing.load_prefs(cfg)
    assert prefs["keywords"] == ["AI Engineer"]
    links = sourcing.search_links(prefs)
    assert any("linkedin.com/jobs" in l["url"] for l in links)
    assert any("mycareersfuture" in l["url"] for l in links)


def test_mcf_to_lead_mapping():
    lead = sourcing.mcf_to_lead(MCF_JOB)
    assert lead["company"] == "Fresh Startup" and lead["position"] == "AI Engineer"
    assert "8000–12000" in lead["comp_mentions"][0]
    assert lead["urls"] == ["https://example.test/job/abc123def456"]
    rich = sourcing.mcf_to_lead(MCF_JOB, DETAIL)
    assert "Requires 8 years of ML" in rich["jd_excerpt"]     # HTML 已剥
    assert rich["skills"] == ["LLM", "RAG"] and rich["min_yoe"] == 8


def test_assess_fit_screens_requirements(cfg):
    """初筛：JD requirements 对照弹药库（履历 SoT），不符项以 req_gaps 返回。"""
    (cfg.workspace_dir / "03-materials").mkdir(exist_ok=True)
    (cfg.workspace_dir / "03-materials" / "achievement-bank.md").write_text(
        "# 战绩素材库\n## X Corp —— MLE\n- 6 years ML engineering, English/Chinese",
        encoding="utf-8")
    (cfg.workspace_dir / "01-profile").mkdir()
    (cfg.workspace_dir / "01-profile" / "extra.md").write_text("补充材料", encoding="utf-8")
    cfg.raw["profile"] = {"files": ["01-profile/extra.md"]}

    class ScreenLLM:
        def generate(self, prompt, system=None, json_mode=False):
            assert "6 years ML engineering" in prompt        # 弹药库=评分对照面主源
            assert "补充材料" in prompt                       # profile.files 仅补充
            assert "fluent Thai" in prompt                   # JD 正文进入上下文
            return json.dumps({"fit": 2, "why": "语言硬伤",
                               "flags": [],
                               "req_gaps": [{"req": "fluent Thai", "verdict": "不符",
                                             "note": "履历语言为中英"},
                                            {"req": "8 years ML", "verdict": "存疑",
                                             "note": "简历约 6 年"}]})
    lead = sourcing.mcf_to_lead(MCF_JOB, DETAIL)
    fit = sourcing.assess_fit(cfg, ScreenLLM(), lead, jd_text=lead["jd_excerpt"])
    assert fit["fit"] == 2 and len(fit["req_gaps"]) == 2
    assert fit["req_gaps"][0]["verdict"] == "不符"


def test_mcf_off_by_default_sends_nothing(cfg, monkeypatch):
    """MCF 开关默认关：搜岗、目标公司退路、贴入链接、薪酬调研、录入补 band 都不发 MCF 请求。"""
    from joblander.compensation import build_comp_report
    from joblander.scout import resolve_intake_text
    from joblander.targets import search_fallback
    from joblander.wizard import features

    def boom(*a, **k):
        raise AssertionError("MCF 关着却发了请求")
    monkeypatch.setattr(sourcing, "mcf_search", boom)
    monkeypatch.setattr(sourcing, "mcf_job_detail", boom)
    cfg.raw["features"] = {"linkedin": False}
    assert features(cfg)["mcf"] is False
    assert sourcing.source_mcf(cfg, FitLLM(), days=36500) == []
    assert search_fallback(cfg, "Acme AI", "Singapore") == []
    with pytest.raises(ValueError, match="MyCareersFuture"):
        resolve_intake_text(cfg, "https://www.mycareersfuture.gov.sg/job/x-" + "a" * 32)
    with pytest.raises(ValueError, match="MyCareersFuture"):
        build_comp_report(cfg, FitLLM(), keywords=["AI Engineer"])


def test_source_mcf_proposes_scores_and_dedupes(cfg, monkeypatch):
    cfg.raw["features"] = {**(cfg.raw.get("features") or {}), "mcf": True}
    monkeypatch.setattr(sourcing, "mcf_search", lambda kw, limit=20, page=0: [MCF_JOB])
    monkeypatch.setattr(sourcing, "mcf_job_detail", lambda uuid: DETAIL)
    outs = sourcing.source_mcf(cfg, FitLLM(), days=36500)
    assert len(outs) == 1
    p = json.loads(outs[0].read_text(encoding="utf-8"))
    assert p["kind"] == "lead.intake" and p["fit"]["fit"] == 4
    assert p["source_hint"] == "mcf" and p["approved"] is None
    # 幂等：uuid 已 seen，第二轮零提案
    assert sourcing.source_mcf(cfg, FitLLM(), days=36500) == []


def test_source_mcf_exclude_and_already_tracked(cfg, monkeypatch):
    """排除词丢弃；已在库公司（含法人名变体）不入池——你已经在打的仗，挂牌岗不是新信息。"""
    cfg.raw["features"] = {**(cfg.raw.get("features") or {}), "mcf": True}
    intern = dict(MCF_JOB, uuid="intern0001", title="AI Engineer Intern")
    tracked = dict(MCF_JOB, uuid="dup0000001",
                   postedCompany={"name": "ACME AI SINGAPORE PTE. LTD."},
                   title="Senior AI Engineer")
    monkeypatch.setattr(sourcing, "mcf_search",
                        lambda kw, limit=20, page=0: [intern, tracked])
    monkeypatch.setattr(sourcing, "mcf_job_detail", lambda uuid: DETAIL)
    outs = sourcing.source_mcf(cfg, FitLLM(), days=36500)
    assert outs == []                           # 一条排除词、一条已在库，全不入池
    log = (cfg.workspace_dir / "08-events" / "event-log.jsonl").read_text()
    assert "exclude_keyword" in log and "already_tracked" in log


def test_market_band(monkeypatch):
    jobs = [{"salary": {"minimum": 5000 + i * 100, "maximum": 9000 + i * 100,
                        "type": {"salaryType": "Monthly"}}} for i in range(8)]
    monkeypatch.setattr(sourcing, "mcf_search", lambda t, limit=20: jobs)
    s = sourcing.market_band("AI Engineer")
    assert "市场参考" in s and "SGD/月" in s and "8 条" in s
    monkeypatch.setattr(sourcing, "mcf_search", lambda t, limit=20: jobs[:3])
    assert sourcing.market_band("AI Engineer") == ""    # 样本不足宁缺毋错


def test_dedupe_normalizes_legal_entity_names():
    """MCF 法人名 vs tracker 简名：GOOGLE ASIA PACIFIC PTE. LTD. 必须命中 Google。"""
    from joblander.scout import dedupe
    rows = [{"Company": "Google"}, {"Company": "Acme AI"}]
    assert dedupe(rows, "GOOGLE ASIA PACIFIC PTE. LTD.", [])["verdict"] == "duplicate"
    assert dedupe(rows, "THE ACME AI INTERNATIONAL (SINGAPORE) PTE. LTD.", [])["verdict"] == "duplicate"
    assert dedupe(rows, "Googolplex Robotics", [])["verdict"] == "new"   # 不误伤相似词


def test_nightly_switch_off_means_manual_only(cfg, monkeypatch):
    """设置里关「每晚自动搜」：夜扫到点也不搜（也不占当天的 done 戳，开回来当晚照常跑）。"""
    from datetime import datetime
    from joblander.daemon import Daemon

    calls = []
    monkeypatch.setattr("joblander.sourcing.source_all", lambda *a, **k: calls.append(1) or {})
    monkeypatch.setattr("joblander.daemon.datetime",
                        type("D", (), {"now": staticmethod(lambda tz=None: datetime(2099, 1, 1, 3, 0, tzinfo=tz))}))
    d = Daemon(cfg)
    monkeypatch.setattr(d, "_llm", lambda tier="flash": None)
    cfg.raw["features"] = {"nightly": False}
    d.job_nightly_sourcing()
    assert calls == [] and "done.sourcing" not in d.state
    cfg.raw["features"] = {"nightly": True}
    d.job_nightly_sourcing()
    assert calls == [1]


def test_extract_min_yoe():
    f = sourcing.extract_min_yoe
    assert f("7+ years of experience with SQL. 2+ years of experience in AWS") == 7
    assert f("Four to five years of experience") == 4
    assert f("One (1) year of experience") == 1
    assert f("2-6 years of professional experience") == 2
    assert f("At least one year of experience") == 1
    assert f("3+ years’ experience in data") == 3
    assert f("a company with over 25 years of experience") is None      # 公司历史不算
    assert f("5 years experience preferred") is None                     # 加分项不算
    assert f("3+ years of experience required; 6+ years of experience is a plus") == 3
    assert f("") is None


def test_max_yoe_pref_skips_before_scoring(cfg, monkeypatch):
    """年限上限：JD 读出超限的不评分直接丢（省 LLM）；正则读不出、评分器读出超限的也丢；不超的照常提。"""
    import json as _j
    sourcing.save_prefs(cfg, {"max_yoe": 3})
    scored = []

    def fit(cfg, llm, lead, jd_text=""):
        scored.append(lead["position"])
        return {"fit": 4, "min_yoe": 6 if "Lead" in lead["position"] else None}
    monkeypatch.setattr(sourcing, "assess_fit", fit)
    run = sourcing._Run(cfg, FitLLM(), "linkedin", days=36500)
    v = {"verdict": "new"}
    run.propose({"company": "A", "position": "Senior DE", "jd_excerpt": "5+ years of experience in Python"}, v, "kw", "u1")
    run.propose({"company": "B", "position": "Lead DE", "jd_excerpt": "seasoned engineer"}, v, "kw", "u2")
    run.propose({"company": "C", "position": "DE", "jd_excerpt": "2+ years of experience with SQL"}, v, "kw", "u3")
    assert scored == ["Lead DE", "DE"]                                 # 5 年的没花评分调用
    assert len(run.outs) == 1
    lead = _j.loads(run.outs[0].read_text(encoding="utf-8"))["lead"]
    assert lead["company"] == "C" and lead["min_yoe"] == 2
    assert "年限上限：只看最低年限要求 ≤ 3 年的岗位" in sourcing.prefs_text(sourcing.load_prefs(cfg))
