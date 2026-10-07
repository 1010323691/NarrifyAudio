"""Cheap, display-only role suggestions from names and existing voice profiles."""
from __future__ import annotations

import re
import unicodedata


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


def suggest_role_hints(names: list[str], config: dict, counts: dict | None = None) -> dict[str, str]:
    """Suggest one earlier representative per role; never mutate config or infer identity."""
    counts = counts or {}
    ordered = sorted(dict.fromkeys(names), key=lambda n: (-counts.get(n, 0), -len(_name(n)), n))
    profiles = {n: _profile(config.get(n) or {}) for n in ordered}
    normalized = {n: _name(n) for n in ordered}
    # Honorifics and nickname prefixes alone do not establish meaningful name overlap.
    generic = set("小大老阿先生小姐女士公子爷叔伯哥姐弟妹")
    hints = {}
    for index, source in enumerate(ordered):
        if source.casefold() in {"narrator", "旁白"}:
            continue
        gender, age = profiles[source]
        best = None
        best_score = -1
        for target in ordered[:index]:
            if target.casefold() in {"narrator", "旁白"}:
                continue
            other_gender, other_age = profiles[target]
            if gender and other_gender and gender != other_gender:
                continue
            if age is not None and other_age is not None and abs(age - other_age) > 1:
                continue
            left, right = normalized[source], normalized[target]
            shared = (set(left) & set(right)) - generic
            chinese = {c for c in shared if "\u3400" <= c <= "\u9fff"}
            pairs = {left[i:i + 2] for i in range(len(left) - 1)}
            latin_match = any(pair.isascii() and pair.isalpha() and pair in right for pair in pairs)
            demographic_match = (bool(gender) and gender == other_gender
                                 and age is not None and other_age is not None)
            if not left or not right:
                continue
            if not (left == right or len(chinese) >= 2 or latin_match
                    or (chinese and demographic_match)):
                continue
            score = len(shared) + 2 * demographic_match + 4 * (left == right)
            if score > best_score:
                best, best_score = target, score
        if best is not None:
            hints[source] = best
    return hints
