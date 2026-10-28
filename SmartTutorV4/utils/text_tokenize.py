"""
Sentence splitting and word counting for the knowledge-base chunker, with no third-party
dependency.

Why this exists
---------------
This replaces ``nltk.sent_tokenize`` and ``len(nltk.word_tokenize(...))``, the only two
NLTK APIs the application ever called. NLTK was removed because CVE-2026-81726 (model
artifact APIs bypass ``nltk.pathsec``) has no patched release in any version, so the
dependency scan could not be cleared by upgrading; NLTK had also produced twelve
advisories in one scan, none of which were reachable from this code base. Dropping it also
removes the ``punkt`` / ``punkt_tab`` download from the Docker build, so the image no
longer needs network access at build time to tokenize text.

Behavioural compatibility
-------------------------
The chunker feeds sentences into fixed-size word buckets and gates segments on word
counts, so what matters is not token-for-token equality with NLTK but that
:func:`count_words` stays on NLTK's scale and that :func:`sent_tokenize` splits in the
same places. Both are asserted directly against NLTK's output in
``scripts/check_text_tokenize.py``, which measured ~1.2% mean counting error and a +0.6
token mean bias over an SOP corpus plus the project README - small enough that chunk
boundaries land where they did before.

Deliberate choices that keep this close to the behaviour it replaces:

* Sentences are split on terminal punctuation only, never on a line break. Text arriving
  from a PDF or a PPTX is full of mid-sentence newlines, and ``punkt`` ignored them too -
  splitting on them would fragment chunks that are single chunks today.
* Word counting follows the Penn Treebank convention NLTK used: punctuation counts as its
  own token and a contraction counts as two (``don't`` -> ``do`` + ``n't``), while a
  decimal, a dotted filename, a slash path and a double dash each stay one token. Counting
  bare whitespace-separated words instead would have inflated every chunk by roughly a
  fifth and silently re-cut the whole knowledge base.
* Abbreviations are handled in two tiers, because not every one is mid-sentence: see
  :data:`_NEVER_FINAL` and :data:`_MAYBE_FINAL`.
* Markdown emphasis and code markers are recognised around a boundary, so a sentence can
  end on ``...the slide count.**`` and the next can open on ```_ppt_group_note```. Course
  material is written in markdown, and ignoring these ran whole paragraphs together.

Everything here is linear-time: the patterns have no nested or overlapping quantifiers,
and the abbreviation lookup is plain string scanning, so there is no backtracking risk
(Sonar ``python:S2631``).
"""
from __future__ import annotations

import re
from typing import List

# Words that end in a period WITHOUT ending the sentence. NLTK's pre-trained English punkt
# model was fitted on newswire and did not know the ones common in training material and
# support SOPs (incl., approx., ext., Rs.), so it split mid-sentence there.
#
# The list is in two tiers because an abbreviation is not always mid-sentence. "Rs." is
# always followed by the amount it introduces, so it can never be a boundary. But "min."
# genuinely ends a sentence in "takes approx. 45 min. Book the slot." - treating every
# abbreviation as never-final would have swallowed that boundary and merged two sentences.
_NEVER_FINAL = frozenset(
    {
        # titles - always followed by a name
        "dr", "mr", "mrs", "ms", "prof", "sr", "jr", "st", "rev", "hon", "capt", "sgt",
        # organisations - a trailing "Ltd." is possible but far rarer than "Ltd. of India"
        "inc", "ltd", "llc", "co", "corp", "pvt", "dept", "div", "univ", "assn", "govt",
        # latin and editorial - introduce the phrase that follows
        "eg", "ie", "cf", "vs", "viz", "al", "ibid", "esp", "resp",
        # quantifiers and cross-references - followed by the thing being quantified
        "approx", "avg", "est", "incl", "excl", "qty", "vol", "no", "nos", "fig", "figs",
        "ref", "ext", "pt", "pp",
        # currency - followed by the amount
        "rs", "usd", "inr", "eur", "gbp",
    }
)

# These end a sentence only when what follows starts with a capital. "12 Jan. 2024" and
# "ext. 4412." continue; "45 min. Book the slot." does not.
_MAYBE_FINAL = frozenset(
    {
        "etc",
        # duration and measurement
        "min", "max", "sec", "secs", "hr", "hrs", "wk", "yr", "yrs", "mth", "mths",
        "kg", "km", "cm", "mm", "ft", "lb", "oz",
        # clock time
        "am", "pm",
        # months and days
        "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov",
        "dec", "mon", "tue", "tues", "wed", "thu", "thur", "thurs", "fri", "sat", "sun",
    }
)

# End of sentence: terminal punctuation, then any closing quote/bracket, then whitespace.
# The closing set includes the markdown emphasis and code markers (`*`, `_`, backtick),
# because SOPs and technical training material end sentences inside them - "...the slide
# count.**" - and skipping those boundaries merges a whole paragraph into one sentence.
_SENTENCE_END = re.compile(r"[.!?…]+[\"'’”)\]}*_`]*(?=\s)")

# A candidate boundary only counts if what follows can begin a sentence: a capital, a
# digit, an opening quote or bracket, or a bullet. Prevents a break inside "no. 4" or
# "version 3.1 of the SOP". The second alternative allows a sentence to open on a
# markdown-marked identifier - "`_ppt_group_note` states the slide count" - which is
# lowercase and so would otherwise not look like a sentence start.
_SENTENCE_START = re.compile(r"[\"'‘“(\[{]*(?:[A-ZÀ-ɏ0-9]|[-*•·])|[`*_]+\w")

# Stricter form of the above: a capital only. Decides whether a _MAYBE_FINAL abbreviation
# is a boundary, so a digit ("12 Jan. 2024") must not qualify.
_CAPITAL_START = re.compile(r"[\"'‘“(\[{]*[A-ZÀ-ɏ]")

# Characters that may trail a sentence's terminal punctuation.
_CLOSERS = "\"'’”)]}*_` \t\n\r"

# Characters that may precede a word.
_OPENERS = "\"'‘“([{*_`"


def _abbreviation_before(head: str) -> str:
    """
    The word that a candidate sentence ends on, lower-cased and stripped of its periods,
    or ``""`` if it is not a plain word (a number, or nothing at all).

    Scanned as a string rather than matched with a regex: the natural pattern for this,
    ``([A-Za-z][A-Za-z.]*)\\.$``, has two quantifiers over overlapping character sets, so
    it backtracks on a long run of letters and dots (Sonar ``python:S2631``).
    """
    stripped = head.rstrip(_CLOSERS)
    if not stripped.endswith("."):
        return ""
    parts = stripped.split()
    if not parts:
        return ""
    word = parts[-1].lstrip(_OPENERS).replace(".", "")
    return word.lower() if word.isalpha() else ""


def sent_tokenize(text: str) -> List[str]:
    """
    Split ``text`` into sentences.

    Splits on ``.``, ``!``, ``?`` and ``…`` followed by whitespace, unless the word before
    the period is a known abbreviation or a single initial, or what follows cannot start a
    sentence. Line breaks are never treated as boundaries.
    """
    if not text or not text.strip():
        return []

    sentences: List[str] = []
    start = 0
    for match in _SENTENCE_END.finditer(text):
        end = match.end()
        head = text[start:end]
        rest = text[end:].lstrip()

        # What comes next must be able to begin a sentence: guards "version 3.1 of the SOP"
        # and "no. 4 in the list", where the period is followed by lowercase.
        if not _SENTENCE_START.match(rest):
            continue

        # "Dr. Rao" / "e.g. a duplicate" / "J. Smith" - not a boundary. "45 min. Book" is.
        word = _abbreviation_before(head)
        if word:
            if word in _NEVER_FINAL or len(word) == 1:
                continue
            if word in _MAYBE_FINAL and not _CAPITAL_START.match(rest):
                continue

        piece = head.strip()
        if piece:
            sentences.append(piece)
        start = end

    tail = text[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences


# Penn Treebank word tokens, longest-match first. Order matters: the numeric and
# abbreviation patterns must win over the bare-word pattern so "1,250.50" and "U.S." stay
# single tokens, exactly as NLTK counted them.
_WORD_PATTERN = re.compile(
    r"""
      \.\.\.|…                               # ellipsis
    | \d+(?:[.,]\d+)+                        # 1,250.50   4.5   10.30
    | /?\w+(?:[-/]\w+)*(?:\.\w+)+\.?         # U.S.  e.g.  router.py  templates/index.html
    | /?\w+(?:[-/]\w+)*/?
        (?:n't|'s|'re|'ve|'ll|'d|'m
           |’s|’re|’ve|’ll|’d|’m)            # word + clitic
    | /?\w+(?:[-/]\w+)*/?                    # word  mid-cycle  3-5  /trainee/course/
    | --                                     # Treebank emits a double dash as one token
    | [^\s\w]                                # one punctuation mark
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Treebank splits a contraction into stem + clitic, so each counts as two tokens.
_CLITIC = re.compile(r"(?i)(n't|'s|'re|'ve|'ll|'d|'m|’s|’re|’ve|’ll|’d|’m)$")


def word_tokenize(text: str) -> List[str]:
    """Tokenize ``text`` the way the Penn Treebank tokenizer NLTK used did."""
    tokens: List[str] = []
    for match in _WORD_PATTERN.finditer(text or ""):
        raw = match.group(0)
        clitic = _CLITIC.search(raw)
        if clitic and clitic.start() > 0:
            tokens.append(raw[: clitic.start()])
            tokens.append(raw[clitic.start():])
        else:
            tokens.append(raw)
    return tokens


def count_words(text: str) -> int:
    """
    Number of tokens in ``text``, on the same scale as ``len(nltk.word_tokenize(text))``.

    This is the only form the chunker ever needed: every former NLTK call site was
    ``len(nltk.word_tokenize(...))``, used to size a chunk or to gate a segment on a
    minimum length.
    """
    return len(word_tokenize(text))


__all__ = ["count_words", "sent_tokenize", "word_tokenize"]
