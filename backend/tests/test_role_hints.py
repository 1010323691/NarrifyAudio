from copy import deepcopy

import pytest

from backend.core.role_hints import suggest_role_hints


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
