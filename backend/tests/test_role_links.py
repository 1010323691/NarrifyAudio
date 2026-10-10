import pytest

from backend.core import role_links as rl
from backend.core.role_hints import rematch_one, suggest_role_links


def _state(links, **extra):
    state = rl.new_state()
    state["links"] = {s: {"target": t, "basis": "x"} for s, t in links.items()}
    state.update(extra)
    return state


def test_links_carry_basis_and_respect_vetoes():
    names = ["林黛玉", "黛玉"]
    counts = {"林黛玉": 10, "黛玉": 3}
    assert suggest_role_links(names, {}, counts) == {"黛玉": ("林黛玉", "名字包含")}
    assert suggest_role_links(names, {}, counts, vetoes=[("黛玉", "林黛玉")]) == {}


def test_rematch_one_searches_larger_and_smaller_roles():
    assert rematch_one("林黛玉", ["黛玉", "王五"], {}) == ("黛玉", "名字包含")
    assert rematch_one("林黛玉", ["王五"], {}) is None


def test_upstream_and_downstream_are_cycle_safe():
    links = _state({"c": "a", "d": "c", "e": "d"})["links"]
    assert rl.upstream(links, "a") == {"c", "d", "e"}
    assert rl.downstream(links, "e") == ["d", "c", "a"]
    links["a"] = {"target": "e", "basis": "x"}
    assert rl.upstream(links, "a") == {"c", "d", "e"}
    assert rl.downstream(links, "a") == ["e", "d", "c"]


def test_merge_orphan_moves_with_its_upstream_and_is_flagged_new():
    # c -> a, d -> c, e -> d; merging only c orphans d. d is rematched onto a (name match).
    state = _state({"c": "a", "d": "c", "e": "d"})
    names = ["a", "b", "d", "e", "ad"]
    cfg = {}
    result = rl.apply_merge(state, "a", ["c"], {"c": {"lines": [1]}}, names, cfg)
    assert result["orphans"] == ["d"]
    assert "c" not in state["links"]
    assert state["links"]["e"]["target"] == "d"  # upstream chain moves with the orphan
    assert state["records"][0]["source"] == "c" and state["records"][0]["prior_link"]["target"] == "a"
    assert state["version"] == 1


def test_merge_orphan_without_match_loses_link_but_keeps_upstream():
    state = _state({"c": "a", "d": "c", "e": "d"})
    result = rl.apply_merge(state, "a", ["c"], None, ["a", "d", "e"], {})
    assert result["rematched"] == {"d": None}
    assert "d" not in state["links"] and state["links"]["e"]["target"] == "d"
    assert state["new_candidates"] == {}


def test_merge_both_selected_does_not_orphan_and_children_follow():
    state = _state({"c": "a", "d": "c"})
    rl.apply_merge(state, "a", ["c", "d"], None, ["a"], {})
    assert state["links"] == {}
    # merging a into t reparents a's record chain
    state2 = _state({"c": "a"})
    rl.apply_merge(state2, "a", ["c"], None, ["a", "t"], {})
    rl.apply_merge(state2, "t", ["a"], None, ["t"], {})
    assert [r["source"] for r in state2["records"]] == ["a"]
    assert [c["source"] for c in state2["records"][0]["children"]] == ["c"]


def test_rematch_never_points_at_own_upstream():
    # d is orphaned; its upstream e would otherwise be the best name match for d.
    state = _state({"c": "a", "d": "c", "小王": "d"})
    rl.apply_merge(state, "a", ["c"], None, ["a", "d", "小王", "王"], {})
    assert state["links"].get("d", {}).get("target") != "小王"


def test_rematched_orphan_becomes_new_candidate_for_whole_downstream():
    state = _state({"黛玉": "林黛玉姑娘", "林黛玉": "x"})
    names = ["黛玉x", "林黛玉", "x", "y"]
    # orphan 林黛玉 (its target 'x' merged) rematches to 黛玉x
    rl.apply_merge(state, "y", ["x"], None, ["黛玉x", "林黛玉", "y"], {})
    assert state["links"]["林黛玉"]["target"] == "黛玉x"
    assert "林黛玉" in state["new_candidates"]["黛玉x"]
    assert rl.mark_seen(state, "黛玉x") and "黛玉x" not in state["new_candidates"]
    assert names  # silence unused


def test_apply_merge_validates_input():
    with pytest.raises(ValueError):
        rl.apply_merge(_state({}), "a", ["a"], None, ["a"], {})
    with pytest.raises(ValueError):
        rl.apply_merge(_state({}), "a", ["b"], None, ["a", "b"], {})  # source still independent


def test_undo_restores_prior_link_and_children():
    state = _state({"c": "a", "w": "c"})
    rl.apply_merge(state, "a", ["c"], None, ["a", "w"], {})
    rid = state["records"][0]["id"]
    out = rl.undo_merge(state, rid, ["a", "c", "w"], {})
    assert out["source"] == "c" and state["records"] == []
    assert state["links"]["c"]["target"] == "a"


def test_undo_restores_nested_records():
    state = _state({"c": "a"})
    rl.apply_merge(state, "a", ["c"], None, ["a", "t"], {})
    rl.apply_merge(state, "t", ["a"], None, ["t"], {})
    rl.undo_merge(state, state["records"][0]["id"], ["t", "a"], {})
    assert [(r["source"], r["target"]) for r in state["records"]] == [("c", "a")]


def test_veto_rematches_and_remove_veto_restores():
    state = _state({"黛玉": "林黛玉"})
    names = ["林黛玉", "黛玉"]
    res = rl.add_veto(state, "黛玉", "林黛玉", names, {})
    assert res["rematched"] == {"黛玉": None} and "黛玉" not in state["links"]
    assert rl.remove_veto(state, "黛玉", "林黛玉", names, {}) is True
    assert state["links"]["黛玉"]["target"] == "林黛玉" and state["vetoes"] == []
    with pytest.raises(ValueError):
        rl.add_veto(state, "黛玉", "别人", names, {})


def test_remove_veto_keeps_new_link_if_role_already_rematched():
    state = _state({"黛玉": "林黛玉"})
    names = ["林黛玉", "黛玉", "黛玉儿"]
    rl.add_veto(state, "黛玉", "林黛玉", names, {})
    assert state["links"]["黛玉"]["target"] == "黛玉儿"
    assert rl.remove_veto(state, "黛玉", "林黛玉", names, {}) is False
    assert state["links"]["黛玉"]["target"] == "黛玉儿"


def test_ensure_state_rebuilds_only_on_fingerprint_change(tmp_path):
    path = tmp_path / "role_links" / "all.json"
    names, counts = ["林黛玉", "黛玉"], {"林黛玉": 5, "黛玉": 1}
    first = rl.ensure_state(path, names, {}, counts, {})
    assert first["links"]["黛玉"]["target"] == "林黛玉" and first["version"] == 1
    first["vetoes"].append({"source": "x", "target": "y", "basis": ""})
    rl.save_state(path, first)
    again = rl.ensure_state(path, names, {}, counts, {})
    assert again["version"] == 1
    changed = rl.ensure_state(path, names, {"黛玉": {"gender": "female"}}, counts, {})
    assert changed["version"] == 2 and changed["vetoes"]  # vetoes survive a rebuild


def test_load_state_ignores_corrupt_file(tmp_path):
    bad = tmp_path / "s.json"
    bad.write_text("{nope")
    assert rl.load_state(bad) is None
    assert rl.load_state(tmp_path / "missing.json") is None
