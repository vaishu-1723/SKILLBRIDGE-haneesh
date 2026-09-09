"""
SkillBridge AI Matching Engine
==============================

A self-contained recommendation engine that scores how good a skill-swap
match two members are, and explains *why* in plain language.

No external API calls (works fully offline / has no network dependency).
It combines:

  1. Fuzzy text similarity (difflib) — catches near-matches like
     "Python" vs "Python Programming" or "Guitar" vs "guitar lessons".
  2. Keyword / token overlap (a lightweight bag-of-words comparison) —
     catches matches phrased differently, e.g. "web design" vs
     "designing websites".
  3. Reciprocity bonus — a two-way match (I teach what you want to learn
     AND you teach what I want to learn) scores much higher than a
     one-way match.

The result is a 0-100 compatibility score plus a short natural-language
reason, similar in spirit to what an LLM-based matcher would produce,
but deterministic, fast, and dependency-free.
"""

import difflib
import re

STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "to", "for", "with", "in", "on",
    "basic", "basics", "advanced", "beginner", "intro", "introduction",
    "lessons", "lesson", "skills", "skill", "programming", "language",
}


def _tokenize(text):
    words = re.findall(r"[a-zA-Z]+", (text or "").lower())
    return {w for w in words if w not in STOPWORDS and len(w) > 1}


def _fuzzy_ratio(a, b):
    a = (a or "").strip().lower()
    b = (b or "").strip().lower()
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _token_overlap(a, b):
    tokens_a = _tokenize(a)
    tokens_b = _tokenize(b)
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union) if union else 0.0


def _skill_similarity(skill_a, skill_b):
    """Blend fuzzy string similarity with token overlap into one 0-1 score."""
    fuzzy = _fuzzy_ratio(skill_a, skill_b)
    overlap = _token_overlap(skill_a, skill_b)
    return max(fuzzy, overlap) * 0.6 + min(fuzzy, overlap) * 0.4


def score_match(viewer, candidate):
    """
    Score how good a match `candidate` is for `viewer`.

    Returns a dict: {score: int 0-100, reasons: [str, ...]}
    """
    if viewer["id"] == candidate["id"]:
        return {"score": 0, "reasons": []}

    # How well can the candidate teach what the viewer wants to learn?
    they_teach_what_i_learn = _skill_similarity(
        viewer["learn_skill"], candidate["teach_skill"]
    )

    # How well does the viewer's teaching skill match what the candidate wants to learn?
    i_teach_what_they_learn = _skill_similarity(
        viewer["teach_skill"], candidate["learn_skill"]
    )

    reasons = []

    if they_teach_what_i_learn >= 0.35:
        reasons.append(
            f"{candidate['name'].split(' ')[0]} can teach you "
            f"{candidate['teach_skill']} — close to what you want to learn "
            f"({viewer['learn_skill']})."
        )

    if i_teach_what_they_learn >= 0.35:
        reasons.append(
            f"You can teach {viewer['teach_skill']}, which matches what "
            f"{candidate['name'].split(' ')[0]} wants to learn "
            f"({candidate['learn_skill']})."
        )

    # Reciprocity bonus: a genuine two-way swap is worth much more than
    # a one-sided match.
    is_reciprocal = they_teach_what_i_learn >= 0.35 and i_teach_what_they_learn >= 0.35

    base = (they_teach_what_i_learn * 0.6) + (i_teach_what_they_learn * 0.4)
    if is_reciprocal:
        base = min(1.0, base + 0.25)
        reasons.insert(0, "🔄 This looks like a two-way swap — you'd both benefit.")

    score = round(base * 100)

    if not reasons:
        reasons.append("Skills don't overlap much yet, but exploring never hurts.")

    return {"score": score, "reasons": reasons, "reciprocal": is_reciprocal}


def rank_matches(viewer, all_users, limit=12):
    """Return the best matches for `viewer`, sorted highest-score first."""
    scored = []
    for candidate in all_users:
        if candidate["id"] == viewer["id"]:
            continue
        result = score_match(viewer, candidate)
        if result["score"] <= 0:
            continue
        scored.append({"user": candidate, **result})

    scored.sort(key=lambda item: item["score"], reverse=True)
    return scored[:limit]
