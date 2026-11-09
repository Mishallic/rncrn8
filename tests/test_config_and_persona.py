import pytest

from rncrn8.config import EngineConfig, SourceType
from rncrn8.engine import BetaCounts
from rncrn8.persona import TEMPLATES, Persona, list_personas, load_persona, scaffold_persona


def test_config_roundtrip_yaml_keeps_enum_keys():
    cfg = EngineConfig()
    text = cfg.to_yaml()
    assert "internet:" in text and "halflife_days" in text
    back = EngineConfig.from_yaml(text)
    assert back == cfg
    assert back.prior_for(SourceType.INTERNET).review_after_days == 180


def test_config_partial_yaml_fills_defaults():
    cfg = EngineConfig.from_yaml("prior_belief: 0.2\nsource_priors:\n  internet: {a: 1, b: 9}\n")
    assert cfg.prior_belief == 0.2
    assert cfg.prior_for(SourceType.INTERNET).counts.mean == pytest.approx(0.1)
    # classes left out of the file still resolve to defaults
    assert cfg.prior_for(SourceType.HUMAN).counts.mean == pytest.approx(0.8)


def test_persona_name_must_be_slug():
    with pytest.raises(ValueError):
        Persona(name="Not Valid", display_name="x", character="y")


def test_trust_profile_overrides_counts_but_keeps_halflife():
    cfg = EngineConfig()
    p = Persona(name="p", display_name="P", character="c", trust_profile={SourceType.INTERNET: BetaCounts(a=1, b=9)})
    prior = p.prior_for(SourceType.INTERNET, cfg)
    assert prior.counts.mean == pytest.approx(0.1)
    assert prior.halflife_days == cfg.prior_for(SourceType.INTERNET).halflife_days


def test_scaffold_from_template_and_reload(ws):
    p = scaffold_persona(ws, "sec", template="security", display_name="Sec")
    assert (ws.persona_dir("sec") / "persona.yaml").exists()
    assert (ws.persona_dir("sec") / "summary.md").exists()
    assert (ws.persona_dir("sec") / "memory.jsonl").exists()
    loaded = load_persona(ws, "sec")
    assert loaded.display_name == "Sec"
    assert loaded.loss == TEMPLATES["security"].loss
    assert loaded.trust_profile == TEMPLATES["security"].trust_profile
    assert {p.name for p in list_personas(ws)} == {"critic", "skimmer", "sec"}


def test_scaffold_refuses_duplicates_and_unknown_templates(ws):
    with pytest.raises(FileExistsError):
        scaffold_persona(ws, "critic")
    with pytest.raises(KeyError):
        scaffold_persona(ws, "x", template="nope")


def test_applies_to():
    assert TEMPLATES["security"].reviews("code")
    assert not TEMPLATES["security"].reviews("video")
    assert TEMPLATES["critic"].reviews("anything")


KINDS = {"*", "code", "document", "idea", "video"}


def test_every_template_is_complete_and_purposeful():
    from rncrn8.persona import GROUPS, PANELS

    assert len(TEMPLATES) == 13
    for name, p in TEMPLATES.items():
        assert p.name == name and p.display_name and len(p.character) > 150
        assert 3 <= len(p.rubric) <= 4 and all(r.endswith("?") for r in p.rubric)
        assert p.applies_to and set(p.applies_to) <= KINDS
        assert p.loss is not None and 0.2 <= p.loss.threshold <= 0.75
        assert all(isinstance(k, SourceType) for k in p.trust_profile)
    grouped = [n for _, names in GROUPS for n in names]
    assert sorted(grouped) == sorted(TEMPLATES) and len(grouped) == len(set(grouped))
    for names in PANELS.values():
        assert names and set(names) <= set(TEMPLATES) and len(names) == len(set(names))
    assert TEMPLATES["auditor"].reviews("code") and TEMPLATES["auditor"].reviews("video")
    assert TEMPLATES["tester"].reviews("code") and not TEMPLATES["tester"].reviews("idea")
    assert TEMPLATES["viewer"].reviews("video") and not TEMPLATES["viewer"].reviews("code")
    assert TEMPLATES["counsel"].reviews("code") and TEMPLATES["counsel"].reviews("idea")
    # what each fears: the auditor, the operator and the counsel speak early; the customer and the viewer only when sure
    assert all(TEMPLATES[n].loss.threshold <= 0.25 for n in ("auditor", "operator", "counsel", "security"))
    assert all(TEMPLATES[n].loss.threshold >= 0.6 for n in ("customer", "viewer", "skimmer"))


def test_scaffold_panel_creates_missing_personas_only(ws):
    from rncrn8.persona import PANELS, scaffold_panel

    created, existing = scaffold_panel(ws, "document")
    assert existing == ["critic", "skimmer"] and [p.name for p in created] == ["stranger", "auditor"]
    again_created, again_existing = scaffold_panel(ws, "document")
    assert again_created == [] and again_existing == PANELS["document"]
    assert {p.name for p in list_personas(ws)} == {"critic", "skimmer", "stranger", "auditor"}
    with pytest.raises(KeyError):
        scaffold_panel(ws, "nope")
