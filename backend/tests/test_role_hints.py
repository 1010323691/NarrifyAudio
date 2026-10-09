from copy import deepcopy

import pytest

from backend.core.role_hints import collect_cooccurrence, suggest_role_hints


@pytest.mark.parametrize("first,second,expected", [
    ("幼女，女声", "幼女，女声", True),
    ("幼女，女声", "小女孩，女声", True),
    ("10岁，女声", "12岁，女声", True),
    ("幼女，女声", "老年女性", False),
    ("幼女，女声", "男童，男声", False),
    ("", "", False),
])
def test_shared_character_and_demographics_only(first, second, expected):
    config = {"贝尔蒙德": {"description": first}, "小贝贝": {"description": second}}
    original = deepcopy(config)
    hints = suggest_role_hints(list(config), config, {"贝尔蒙德": 200, "小贝贝": 5})
    assert (hints.get("小贝贝") == "贝尔蒙德") == expected
    assert config == original


def test_generic_attributes_without_name_overlap_do_not_match():
    config = {name: {"description": "幼女，女声"} for name in ["贝尔蒙德", "小莉莉"]}
    assert suggest_role_hints(list(config), config) == {}


def test_generic_prefixes_and_single_latin_letter_do_not_match():
    config = {name: {"description": "青年男性"} for name in ["小王", "小李", "熊猫A", "aＫ", "NARRATOR"]}
    assert suggest_role_hints(list(config), config) == {}


def test_explicit_gender_overrides_description_for_hints():
    config = {"贝尔蒙德": {"description": "幼女，女声", "gender": "male"},
              "小贝贝": {"description": "幼女，女声", "gender": "female"}}
    assert suggest_role_hints(list(config), config) == {}


def test_stronger_name_overlap_can_offer_weak_hint_when_profiles_unknown():
    config = {"熊猫": {}, "小熊猫": {}}
    assert suggest_role_hints(list(config), config, {"熊猫": 200, "小熊猫": 5}) == {"小熊猫": "熊猫"}


def _hints(names, counts=None, cooccur=None, config=None):
    return suggest_role_hints(names, config or {}, counts or {n: 100 - i for i, n in enumerate(names)}, cooccur)


def test_sibling_names_sharing_surname_and_one_char_do_not_match():
    assert _hints(["张大山", "张小山"]) == {}


def test_relational_names_are_never_aliases():
    assert _hints(["李明", "李明的父亲"]) == {}


def test_honorific_suffix_is_stripped_and_substring_matches():
    assert _hints(["林黛玉", "黛玉"]) == {"黛玉": "林黛玉"}
    assert _hints(["林黛玉", "黛玉姑娘"]) == {"黛玉姑娘": "林黛玉"}


def test_bare_surname_with_title_needs_matching_known_gender():
    config = {"林黛玉": {"gender": "female"}}
    assert _hints(["林黛玉", "林姑娘"], config=config) == {"林姑娘": "林黛玉"}
    assert _hints(["林黛玉", "林先生"], config=config) == {}
    assert _hints(["林黛玉", "林姑娘"]) == {}


def test_cooccurring_roles_are_not_offered_as_the_same_person():
    config = {name: {"description": "青年女性"} for name in ["林黛玉", "林雨"]}
    pair = ("林雨", "林黛玉")
    assert _hints(list(config), config=config) == {"林雨": "林黛玉"}
    assert _hints(list(config), cooccur={pair: 2}, config=config) == {}
    assert _hints(list(config), cooccur={pair: 1}, config=config) == {"林雨": "林黛玉"}


def test_collect_cooccurrence_counts_turn_taking_and_skips_narrator():
    pairs = {}
    collect_cooccurrence(["甲", "甲", "旁白", "乙", "甲", "乙", "乙", "丙"], pairs)
    assert pairs == {("乙", "甲"): 3, ("丙", "乙"): 1}


def test_latin_names_match_by_token_not_letter_pairs():
    assert _hints(["Mary", "Harry"]) == {}
    assert _hints(["Tom", "Thomas"]) == {}
    assert _hints(["John Smith", "Smith"]) == {"Smith": "John Smith"}
    assert _hints(["Katherine", "Catherine"]) == {"Catherine": "Katherine"}


def test_numbered_or_lettered_placeholder_roles_do_not_match():
    assert _hints(["士兵甲", "士兵乙", "路人1", "路人2", "Guard 1", "Guard 2", "Soldier A", "Soldier B"]) == {}


def test_relation_only_excludes_kinship_names_not_every_name_with_de():
    assert _hints(["白的卢", "的卢"]) == {"的卢": "白的卢"}
    assert _hints(["李明", "李明的母亲"]) == {}


def test_latin_nickname_prefix_matches():
    assert _hints(["Alexander", "Alex"]) == {"Alex": "Alexander"}
    assert _hints(["Tommy", "Tom"]) == {"Tom": "Tommy"}


def test_inconsistent_labels_survive_but_real_conversations_veto():
    names = ["林黛玉", "黛玉"]
    # Strong name overlap needs sustained alternation (not two parser label flips) to be vetoed.
    assert _hints(names, cooccur={("林黛玉", "黛玉"): 2}) == {"黛玉": "林黛玉"}
    assert _hints(names, cooccur={("林黛玉", "黛玉"): 4}) == {}
    assert _hints(["林黛玉", "林宝玉"], cooccur={("林宝玉", "林黛玉"): 2}) == {}


def test_cache_key_follows_vetoing_cooccurrence(tmp_path):
    from backend.core.role_hint_cache import cached_role_hints
    names = ["林黛玉", "黛玉"]
    counts = {"林黛玉": 10, "黛玉": 3}
    assert cached_role_hints(names, {}, counts, tmp_path) == {"黛玉": "林黛玉"}
    assert cached_role_hints(names, {}, counts, tmp_path, {("林黛玉", "黛玉"): 9}) == {}
    assert cached_role_hints(names, {}, counts, tmp_path, {("林黛玉", "黛玉"): 1}) == {"黛玉": "林黛玉"}


def test_relatives_sharing_a_surname_or_differing_only_by_title_do_not_match():
    assert _hints(["Mary Smith", "John Smith"]) == {}
    assert _hints(["Mr. Smith", "Mrs. Smith"]) == {}
    assert _hints(["王总", "王夫人"]) == {}
    assert _hints(["John Smith", "John Q Smith"]) == {"John Q Smith": "John Smith"}
    assert _hints(["Mr. Smith", "Smith"]) == {"Smith": "Mr. Smith"}
