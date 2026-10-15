"""LinkedIn 公开职位：解析（真实页面结构的精简样本）+ 与 MCF 同一条查重/评分/入池管线。"""

import json

from joblander import sourcing as S
from joblander.config import Config
from joblander.llm import MockLLM

CARD = """<li>
 <div class="base-card job-search-card" data-entity-urn="urn:li:jobPosting:4420482236">
  <a class="base-card__full-link absolute" href="https://sg.linkedin.com/jobs/view/software-developer-at-acme-4420482236?position=1&amp;pageNum=0">
  <h3 class="base-search-card__title">
        Software Developer
  </h3>
  <h4 class="base-search-card__subtitle">
    <a class="hidden-nested-link" href="x">  Acme &amp; Co  </a>
  </h4>
  <span class="job-search-card__location">  Singapore, Singapore  </span>
  <time class="job-search-card__listdate--new" datetime="2099-10-04">1 hour ago</time>
 </div></li>"""

DETAIL = """<div class="show-more-less-html__markup relative">We need <strong>5+ years</strong> Python.</div>
<h3 class="description__job-criteria-subheader">
 Seniority level
</h3>
<span class="description__job-criteria-text">
 Mid-Senior level
</span>"""


def test_parse_cards_and_detail():
    [c] = S.parse_linkedin_cards("<ul>" + CARD + "</ul>")
    assert c == {"id": "4420482236", "title": "Software Developer", "company": "Acme & Co",
                 "location": "Singapore, Singapore", "posted": "2099-10-04",
                 "url": "https://sg.linkedin.com/jobs/view/software-developer-at-acme-4420482236"}
    d = S.parse_linkedin_detail(DETAIL)
    assert d["description"] == "We need 5+ years Python."
    assert d["criteria"] == {"Seniority level": "Mid-Senior level"}
    lead = S.linkedin_to_lead(c, d)
    assert lead["jd_excerpt"].startswith("We need") and "Mid-Senior" in lead["highlight"]


def _cfg(tmp_path, linkedin=True):
    ws = tmp_path / "ws"
    (ws / "09-projections").mkdir(parents=True)
    (ws / "09-projections" / "tracker.json").write_text(json.dumps(
        {"rows": [{"Company": "Tracked Ltd", "Status": "Applied"}]}), encoding="utf-8")
    (ws / "02-targets").mkdir()
    (ws / "02-targets" / "sourcing-prefs.yaml").write_text(
        "keywords: [software engineer]\nlocations: [Singapore]\nexclude: [intern]\n", encoding="utf-8")
    return Config(raw={"workspace_dir": str(ws), "features": {"linkedin": linkedin}},
                  path=tmp_path / "c.yaml")


def test_source_linkedin_pipeline(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cards = [
        {"id": "1", "title": "Backend Engineer", "company": "NewCo", "location": "SG",
         "posted": "2099-01-01", "url": "u1"},
        {"id": "2", "title": "Backend Engineer", "company": "NewCo", "location": "SG",
         "posted": "2099-01-01", "url": "u2"},                       # 同轮重复挂牌
        {"id": "3", "title": "Software Intern", "company": "X", "location": "SG",
         "posted": "2099-01-01", "url": "u3"},                       # 排除词
        {"id": "4", "title": "SWE", "company": "Tracked Ltd", "location": "SG",
         "posted": "2099-01-01", "url": "u4"},                       # 已在库
    ]
    calls = []
    monkeypatch.setattr(S, "linkedin_search", lambda kw, loc, hours=48, start=0, remote=False:
                        calls.append((kw, loc, hours)) or cards)
    monkeypatch.setattr(S, "_li_get", lambda url, timeout=20: DETAIL)
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(S, "assess_fit", lambda cfg, llm, lead, jd_text="": {"fit": 4, "jd": jd_text})
    outs = S.source_linkedin(cfg, MockLLM([]))
    assert calls == [("software engineer", "Singapore", 48)]
    assert len(outs) == 1
    prop = json.loads(outs[0].read_text(encoding="utf-8"))
    assert prop["source_hint"] == "linkedin" and prop["lead"]["company"] == "NewCo"
    assert prop["fit"]["jd"].startswith("We need")                  # 详情喂给了初筛
    assert S.source_linkedin(cfg, MockLLM([])) == []                 # 幂等：见过的不再提


def test_parse_location():
    assert S.parse_location("Houston") == ("Houston", False)
    assert S.parse_location("Remote United States") == ("United States", True)
    assert S.parse_location("remote (US)") == ("US", True)
    assert S.parse_location("Remote") == ("Worldwide", True)
    assert S.parse_location("Remoteville") == ("Remoteville", False)


def test_source_linkedin_every_location_and_remote(tmp_path, monkeypatch):
    """每个地点都搜（不只第一个）；「Remote …」走远程筛选；跨地点同一岗位只提一次。"""
    cfg = _cfg(tmp_path)
    S.save_prefs(cfg, {"keywords": ["data engineer"],
                       "locations": ["Houston", "Austin", "Remote United States"]})
    calls = []

    def search(kw, loc, hours=48, start=0, remote=False):
        calls.append((loc, remote))
        return [{"id": "same", "title": "Data Engineer", "company": "NewCo", "location": loc,
                 "posted": "2099-01-01", "url": "u"},
                {"id": loc, "title": f"Data Engineer {loc}", "company": f"Co {loc}",
                 "location": loc, "posted": "2099-01-01", "url": "u"}]
    monkeypatch.setattr(S, "linkedin_search", search)
    monkeypatch.setattr(S, "_li_get", lambda url, timeout=20: DETAIL)
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(S, "assess_fit", lambda cfg, llm, lead, jd_text="": {"fit": 4})
    outs = S.source_linkedin(cfg, MockLLM([]))
    assert calls == [("Houston", False), ("Austin", False), ("United States", True)]
    assert len(outs) == 4                                            # 3 个各地岗 + 共有岗只算一次


def test_remote_query_param(monkeypatch):
    urls = []
    monkeypatch.setattr(S, "_li_get", lambda url, timeout=20: urls.append(url) or "")
    S.linkedin_search("data engineer", "United States", remote=True)
    S.linkedin_search("data engineer", "Houston")
    assert "f_WT=2" in urls[0] and "f_WT" not in urls[1]


def test_target_location_filter_understands_remote():
    from joblander.targets import in_locations
    locs = ["Houston", "Remote United States"]
    assert in_locations({"location": "Houston, TX"}, locs)
    assert in_locations({"location": "Remote - US"}, locs)
    assert not in_locations({"location": "Chicago, IL"}, locs)


def test_stop_mid_search_keeps_bookkeeping(tmp_path, monkeypatch):
    """网页「停止」：停在两条岗位之间；已提的照留、见过的照记（下轮不重复提），后面的搜索不再发。"""
    import threading
    from joblander import cancel
    cfg = _cfg(tmp_path)
    S.save_prefs(cfg, {"keywords": ["a", "b"], "locations": ["Houston", "Austin"]})
    stop = threading.Event()
    calls = []
    cards = [{"id": "1", "title": "Data Engineer", "company": "One", "location": "Houston",
              "posted": "2099-01-01", "url": "u1"},
             {"id": "2", "title": "Data Engineer", "company": "Two", "location": "Houston",
              "posted": "2099-01-01", "url": "u2"}]
    monkeypatch.setattr(S, "linkedin_search", lambda kw, loc, hours=48, start=0, remote=False:
                        calls.append((kw, loc)) or cards)
    monkeypatch.setattr(S, "_li_get", lambda url, timeout=20: DETAIL)
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(S, "assess_fit", lambda cfg, llm, lead, jd_text="": stop.set() or {"fit": 4})
    cancel.bind(stop)
    try:
        outs = S.source_linkedin(cfg, MockLLM([]))
        assert S.source_all(cfg, MockLLM([])) == {}                  # 停了：一个渠道都不再开跑
    finally:
        cancel.bind(None)
    assert len(outs) == 1 and calls == [("a", "Houston")]
    seen = json.loads((cfg.workspace_dir / "19-sourcing" / "linkedin-seen.json").read_text())
    assert "1" in seen and "2" not in seen                          # 没看的留给下一轮


def test_linkedin_off_or_blocked_never_raises(tmp_path, monkeypatch):
    assert S.source_linkedin(_cfg(tmp_path / "a", linkedin=False), MockLLM([])) == []
    cfg = _cfg(tmp_path / "b")
    def blocked(*a, **k):
        raise OSError("429 Too Many Requests")
    monkeypatch.setattr(S, "linkedin_search", blocked)
    monkeypatch.setattr(S, "source_mcf", lambda cfg, llm, days=2: [])
    assert S.source_all(cfg, MockLLM([])) == {"mcf": 0, "linkedin": 0, "targets": 0}
