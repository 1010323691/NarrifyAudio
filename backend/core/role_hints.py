"""Cheap, display-only role suggestions from names and existing voice profiles."""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher


def _name(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())


def _profile(entry: dict) -> tuple[str, int | None]:
    text = str(entry.get("description") or "")
    gender = entry.get("gender") or ""
    if gender not in {"male", "female"}:
        female = bool(re.search(r"女性|女声|女孩|幼女|少女|妇人|老妇", text))
        male = bool(re.search(r"男性|男声|男孩|少年|男童|老翁", text))
        gender = "female" if female and not male else "male" if male and not female else ""
    groups = (
        r"幼女|幼童|男童|女童|儿童|小女孩|小男孩|孩童|child",
        r"少女|少年|青少年|teen",
        r"青年|年轻|成年|young|adult",
        r"中年|middle.?aged",
        r"老年|老人|老妇|老翁|老者|elder|senior",
    )
    ages = [i for i, pattern in enumerate(groups) if re.search(pattern, text, re.I)]
    years = re.search(r"(?<!\d)(\d{1,3})\s*(?:岁|years? old)", text, re.I)
    if years:
        value = int(years[1])
        ages = [0 if value < 13 else 1 if value < 18 else 2 if value < 40 else 3 if value < 60 else 4]
    return gender, ages[0] if len(ages) == 1 else None


# Title / kinship suffixes that only decorate a name ("林姑娘" is "林"); value = implied gender.
_SUFFIXES = {
    "先生": "male", "师傅": "", "老师": "", "医生": "", "大夫": "", "经理": "", "总": "",
    "小姐": "female", "女士": "female", "姑娘": "female", "夫人": "female", "太太": "female",
    "妹妹": "female", "姐姐": "female", "阿姨": "female", "奶奶": "female", "大姐": "female",
    "哥哥": "male", "弟弟": "male", "叔叔": "male", "爷爷": "male", "大哥": "male",
    "公子": "male", "少爷": "male", "大爷": "male",
}
_SUFFIX_ORDER = sorted(_SUFFIXES, key=len, reverse=True)
_PREFIXES = "小大老阿"
_GENERIC = frozenset("小大老阿先生小姐女士公子爷叔伯哥姐弟妹")
COOCCUR_VETO = 2  # one scene handoff proves nothing; repeated turn-taking means a conversation
COOCCUR_VETO_STRONG = 4  # near-identical names also alternate when the parser labels one person inconsistently
_NARRATOR = {"narrator", "旁白"}
# Latin honorifics: stripped from tokens, they imply a gender ("Mr. Smith" vs "Mrs. Smith" are spouses).
_HONORIFICS = {"mr": "male", "sir": "male", "lord": "male", "mrs": "female", "ms": "female",
               "miss": "female", "lady": "female", "madam": "female", "dr": "", "prof": ""}
_KINSHIP = ("父亲", "母亲", "爸爸", "妈妈", "父", "母", "爹", "娘", "儿子", "女儿", "妻子", "丈夫", "哥哥",
            "姐姐", "弟弟", "妹妹", "兄", "妻", "夫", "爷爷", "奶奶", "祖父", "祖母", "朋友", "同学",
            "助手", "秘书", "管家", "仆人", "侍女", "丫鬟", "随从", "手下", "部下", "学生", "老师")
# Trailing placeholder index: "路人1", "士兵甲", "Soldier A" are different people of one group.
_INDEX = re.compile(r"(?:\s*\d+|[甲乙丙丁戊己庚辛壬癸]|(?:(?<=\s)|(?<=[^\x00-\x7f]))[a-z])$")


def veto_pairs(cooccur: dict | None, known) -> list[list]:
    """Cache-key view of the turn-taking counts that can still change a hint."""
    return sorted([a, b, min(n, COOCCUR_VETO_STRONG)] for (a, b), n in (cooccur or {}).items()
                  if a in known and b in known and n >= COOCCUR_VETO)


def _info(name: str) -> tuple:
    """Per-name facts, computed once: (normalized, core, implied gender, latin tokens, index marker, decorated)."""
    text = _name(name)
    implied = ""
    core = text
    decorated = False
    for suffix in _SUFFIX_ORDER:
        if core.endswith(suffix) and len(core) > len(suffix):
            core, implied, decorated = core[:-len(suffix)], _SUFFIXES[suffix], True
            break
    if len(core) > 2 and core[0] in _PREFIXES:
        core = core[1:]
    folded = unicodedata.normalize("NFKC", name).casefold().strip()
    marker = _INDEX.search(folded)
    if marker and len(folded[:marker.start()].strip()) < 2:
        marker = None
    tokens = []
    if folded and all(c.isascii() and (c.isalpha() or c.isspace() or c in ".-'") for c in folded):
        tokens = [t for t in (re.sub(r"[^a-z]", "", w) for w in folded.split()) if t]
        while len(tokens) > 1 and tokens[0] in _HONORIFICS:
            implied, decorated = implied or _HONORIFICS[tokens[0]], True
            tokens = tokens[1:]
    return text, core, implied, tokens, marker.group().strip() if marker else "", decorated


def _is_relational(name: str) -> bool:
    """"李明的父亲": names a different person by relation to another, never an alias."""
    left, _, right = name.partition("的")
    return bool(left.strip()) and right.strip().endswith(_KINSHIP)


def _longest_common(a: str, b: str) -> int:
    best = 0
    previous = [0] * (len(b) + 1)
    for x in a:
        current = [0]
        for j, y in enumerate(b, 1):
            current.append(previous[j - 1] + 1 if x == y else 0)
            best = max(best, current[-1])
        previous = current
    return best


def _name_match(left_info: tuple, right_info: tuple, gender: str, age, other_gender: str, other_age) -> tuple | None:
    """Rank how strongly two names suggest one person: (tier, shared length, demographic) or None."""
    left, left_core, left_implied, left_tokens, left_marker, left_decorated = left_info
    right, right_core, right_implied, right_tokens, right_marker, right_decorated = right_info
    if not left or not right:
        return None
    if left == right:
        return 5, len(left), 0
    if left_marker and right_marker and left_marker != right_marker:
        return None
    gender = gender or left_implied
    other_gender = other_gender or right_implied
    if gender and other_gender and gender != other_gender:
        return None
    demographic = bool(gender) and gender == other_gender and age is not None and other_age is not None
    if left_tokens and right_tokens:
        if set(left_tokens) == set(right_tokens):
            return 4, len(left_core), 0
        if len(left_tokens) > 1 and len(right_tokens) > 1 and left_tokens[0] != right_tokens[0]:
            return None  # different given names under one surname: relatives, not one person
        common = max((len(t) for t in set(left_tokens) & set(right_tokens)), default=0)
        if common >= 3:
            return 3, common, 0
        if len(left_tokens) == len(right_tokens) == 1:
            short_token, long_token = sorted((left_tokens[0], right_tokens[0]), key=len)
            if len(short_token) >= 3 and long_token.startswith(short_token):
                return 3, len(short_token), 0
            if len(short_token) >= 4 and SequenceMatcher(None, short_token, long_token).ratio() >= 0.8:
                return 3, len(short_token), 0
        return None
    if left_core == right_core:
        if len(left_core) == 1 and left_decorated and right_decorated:
            return None  # "王总" vs "王夫人": a bare surname with two different titles
        return 4, len(left_core), demographic
    short, long_ = sorted((left_core, right_core), key=len)
    if len(short) >= 2 and short in long_:
        return 4, len(short), demographic
    common = _longest_common(left_core, right_core)
    if common >= 2:
        return 3, common, demographic
    shared = {c for c in set(left_core) & set(right_core) - _GENERIC if "\u3400" <= c <= "\u9fff"}
    if shared and demographic:
        return 2, 1, demographic
    # A bare surname ("林") against a fuller name led by it: needs a known, equal gender.
    if len(short) == 1 and long_.startswith(short) and gender and gender == other_gender:
        return 1, 1, demographic
    return None


def suggest_role_hints(names: list[str], config: dict, counts: dict | None = None,
                       cooccur: dict | None = None) -> dict[str, str]:
    """Suggest one earlier representative per role; never mutate config or infer identity.

    ``cooccur`` maps sorted ``(a, b)`` name pairs to how often they take turns on consecutive
    lines; repeated turn-taking means they are talking to one another, so such roles are
    never offered as the same person.
    """
    counts = counts or {}
    cooccur = cooccur or {}
    ordered = sorted(dict.fromkeys(names), key=lambda n: (-counts.get(n, 0), -len(_name(n)), n))
    profiles = {n: _profile(config.get(n) or {}) for n in ordered}
    infos = {n: _info(n) for n in ordered}
    hints = {}
    for index, source in enumerate(ordered):
        if source.casefold() in _NARRATOR or _is_relational(source):
            continue
        gender, age = profiles[source]
        best = None
        best_score: tuple | None = None
        for target in ordered[:index]:
            if target.casefold() in _NARRATOR or _is_relational(target):
                continue
            other_gender, other_age = profiles[target]
            if gender and other_gender and gender != other_gender:
                continue
            if age is not None and other_age is not None and abs(age - other_age) > 1:
                continue
            score = _name_match(infos[source], infos[target], gender, age, other_gender, other_age)
            if score is None:
                continue
            if score[0] < 5:
                limit = COOCCUR_VETO_STRONG if score[0] == 4 else COOCCUR_VETO
                if cooccur.get(tuple(sorted((source, target))), 0) >= limit:
                    continue
            if best_score is None or score > best_score:
                best, best_score = target, score
        if best is not None:
            hints[source] = best
    return hints


def collect_cooccurrence(speakers, pairs: dict) -> None:
    """Count turn-taking: sorted pairs of different speakers on consecutive lines (narrator skipped)."""
    previous = ""
    for speaker in speakers:
        if speaker.casefold() in _NARRATOR:
            continue
        if previous and previous != speaker:
            key = (previous, speaker) if previous < speaker else (speaker, previous)
            pairs[key] = pairs.get(key, 0) + 1
        previous = speaker
