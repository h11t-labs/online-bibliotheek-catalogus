"""Text normalisation helpers: author splitting, publisher canonicalisation,
and fuzzy match keys used for curated-list matching."""

from __future__ import annotations

import re
import unicodedata

_AUTHOR_SPLIT = re.compile(r"\s*[|;]\s*")


def split_authors(value: str | None) -> list[str]:
    """Split a multi-author string on '|' / ';' into individual names.

    Commas are intentionally NOT split on — in this catalog they appear inside
    names ("Buren, van"), not as separators.
    """
    if not value:
        return []
    parts = [p.strip(" \t,") for p in _AUTHOR_SPLIT.split(value)]
    return list(dict.fromkeys(p for p in parts if p))


def publisher_key(value: str | None) -> str:
    """Loose grouping key so 'De Correspondent, Amsterdam' and
    'de Correspondent, [Amsterdam]' collapse together."""
    if not value:
        return ""
    s = value.lower().replace("[", "").replace("]", "")
    s = re.sub(r"\s+", " ", s).strip(" .,")
    return s


# Curated publisher aliases for cases plain key-folding can't merge (different
# words / imprints). Each entry: canonical name + folded substrings that map to
# it. Extend this list as you spot more. First match wins.
PUBLISHER_ALIASES: list[tuple[str, list[str]]] = [
    ("De Correspondent, Amsterdam", ["correspondent"]),
    ("Das Mag, Amsterdam", ["das mag"]),
    # "Bert Bakker" is a Prometheus sub-imprint and stays distinct from the main
    # "Prometheus, Amsterdam"; merge only the Bert Bakker spelling variants.
    ("Prometheus Bert Bakker, Amsterdam", ["bert bakker"]),
]


def canonical_publisher(value: str | None, fallback: str | None = None) -> str | None:
    """Map a publisher to a curated canonical name, else return ``fallback``
    (typically the most-common spelling of its group) or the value itself."""
    if not value:
        return value
    f = fold(value)
    for canon, patterns in PUBLISHER_ALIASES:
        if any(p in f for p in patterns):
            return canon
    return fallback if fallback is not None else value


def fold(value: str | None) -> str:
    """Lowercase, strip diacritics and non-alphanumerics — for matching."""
    if not value:
        return ""
    s = unicodedata.normalize("NFKD", value)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def slugify(value: str | None) -> str:
    """URL slug from a display name: 'Lisbeth Imbo' -> 'lisbeth-imbo'.

    Built on :func:`fold`, so it is always URL-safe ASCII and — importantly —
    reversible into a ``name_fold`` by swapping the dashes back to spaces. That
    makes a slug an indexed lookup key rather than something to store. Names with
    no Latin characters at all (Greek script, junk rows) fold to an empty string
    and therefore have no slug; callers must handle that.
    """
    return fold(value).replace(" ", "-")


# Dutch/Flemish name particles. The catalog stores names first-name-first, so the
# surname is the last token — unless the name is already inverted ("Buren, van"),
# where the trailing token is a particle and the real surname sits before it.
_NAME_PARTICLES = {
    "van", "de", "der", "den", "het", "ten", "ter", "te", "op", "aan", "in", "'t",
    "du", "des", "del", "della", "la", "le", "di", "da", "dos", "von", "zu", "af",
    # surname_key strips apostrophes before folding, so an inverted "Wolde, van 't"
    # arrives as [wolde, van, t] — the bare "t" must count as a particle too, or
    # the name files under T.
    "t",
}


# fold() strips diacritics by decomposing them, which silently *deletes* the Latin
# letters that have no combining form: "Strøm" folds to "str m", "Þórarinsdóttir"
# to "orarinsdottir". Harmless for matching, wrong for alphabetising — it filed 33
# Nordic and Icelandic authors under a letter from the middle of their name (Røyne
# under Y, Bøe under E). Spelled out here rather than inside fold(), because
# authors.name_fold is written at normalize time and every slug URL round-trips
# through it: changing fold() would 404 those pages until the next full rebuild.
_TRANSLITERATE = str.maketrans({
    "ø": "o", "Ø": "O", "æ": "ae", "Æ": "Ae", "œ": "oe", "Œ": "Oe",
    "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ð": "d", "Ð": "D",
    "þ": "th", "Þ": "Th", "ß": "ss", "ı": "i",
})


# Editorial roles only ever appear bracketed — "Wim Kloppenburg (red.)", "Adam
# J.B. Lane (ill.)" — so they are stripped as brackets rather than as words. A
# word rule would misfile the real surname in "Ludique le Vert" under L.
_ROLE_BRACKET = re.compile(r"\([^)]*\)")
# Two authors are websites; their TLD is not a surname.
_DOMAIN_TAIL = re.compile(r"\.(?:nl|com|be|org|net|eu|de)\s*$", re.I)
# fold() treats an apostrophe as a separator, which cuts "O'Brien" into "o brien"
# and files 123 authors under the tail of their own surname (O'Brien under B).
# Dropping it binds the name back together; the standalone Dutch "'t" in
# "van 't Spijker" becomes a bare "t" token, which is not the last one anyway.
_APOSTROPHE = re.compile(r"[’'`]")
# Generation markers and "and others". Matched as token *sequences* against the
# folded name, which has already dropped the dots: "e" alone is a plausible
# surname, ("e", "a") is not.
_NAME_SUFFIXES = (
    ("e", "v", "a"), ("e", "a"), ("c", "s"), ("et", "al"), ("jr",), ("sr",),
)


def surname_key(name: str | None) -> str:
    """Folded surname for alphabetising: 'Alexander Klöpping' -> 'klopping'.

    A reader looking for Klöpping looks under K, not under A of Alexander — so the
    A-Z index has to sort on this rather than on the first character of the full
    name. "Bob de Wit" lands on 'wit', "Buren, van" on 'buren'.
    """
    raw = _DOMAIN_TAIL.sub("", _ROLE_BRACKET.sub(" ", name or ""))
    parts = fold(_APOSTROPHE.sub("", raw).translate(_TRANSLITERATE)).split()
    trimmed = True
    while trimmed and len(parts) > 1:
        trimmed = False
        for suffix in _NAME_SUFFIXES:
            n = len(suffix)
            # Leave at least a first name and a surname behind. "Mariela SR" is a
            # two-token author name where SR is the name, not a generation marker;
            # "James Burn sr." has a first name to spare, so it loses the suffix.
            if len(parts) - n >= 2 and tuple(parts[-n:]) == suffix:
                del parts[-n:]
                trimmed = True
                break
        if not trimmed and parts[-1] in _NAME_PARTICLES:
            parts.pop()
            trimmed = True
    return parts[-1] if parts else ""


# Author aliases: fold(variant) -> canonical display name. The catalog sometimes
# lists the same person under shortened/variant names; collapse them here. Extend
# as you spot more (left side is the folded form of any spelling that should map).
AUTHOR_ALIASES: dict[str, str] = {
    "bernlef": "J. Bernlef",
}


def canonical_author(name: str | None) -> str | None:
    if not name:
        return name
    return AUTHOR_ALIASES.get(fold(name), name)


# The `taal` field is occasionally polluted with non-language strings ("Fictie",
# "Verzameld werk", stray sentence fragments). Languages are a closed vocabulary,
# so we keep only known names; anything else becomes NULL (excluded from facets).
_VALID_LANG_NAMES = [
    "Nederlands", "Engels", "Duits", "Frans", "Spaans", "Italiaans", "Portugees",
    "Latijn", "Grieks", "Nieuwgrieks", "Russisch", "Pools", "Tsjechisch",
    "Slowaaks", "Hongaars", "Roemeens", "Bulgaars", "Servisch", "Kroatisch",
    "Bosnisch", "Sloveens", "Oekraïens", "Wit-Russisch", "Macedonisch", "Albanees",
    "Zweeds", "Noors", "Deens", "Fins", "IJslands", "Ests", "Lets", "Litouws",
    "Turks", "Arabisch", "Hebreeuws", "Jiddisch", "Perzisch", "Koerdisch",
    "Chinees", "Japans", "Koreaans", "Hindi", "Urdu", "Bengaals", "Indonesisch",
    "Maleis", "Thais", "Vietnamees", "Afrikaans", "Swahili", "Armeens",
    "Georgisch", "Catalaans", "Galicisch", "Baskisch", "Iers", "Schots", "Welsh",
    "Bretons", "Papiaments", "Fries", "Westerlauwers Fries", "Limburgs",
    "Esperanto", "Sanskriet", "meerdere talen",
]
VALID_LANGUAGES = {fold(n) for n in _VALID_LANG_NAMES}


def valid_language(name: str | None) -> str | None:
    """Return the language if it's a known language name, else None."""
    if name and fold(name) in VALID_LANGUAGES:
        return name
    return None


# schema.org's inLanguage wants an IETF BCP 47 code, not a Dutch language name.
# "Schots" is deliberately absent: it maps to either Scots (sco) or Scottish
# Gaelic (gd) and the catalog doesn't say which — better no code than a wrong one.
_LANGUAGE_CODES: dict[str, str] = {
    "Nederlands": "nl", "Engels": "en", "Duits": "de", "Frans": "fr",
    "Spaans": "es", "Italiaans": "it", "Portugees": "pt", "Latijn": "la",
    "Grieks": "el", "Nieuwgrieks": "el", "Russisch": "ru", "Pools": "pl",
    "Tsjechisch": "cs", "Slowaaks": "sk", "Hongaars": "hu", "Roemeens": "ro",
    "Bulgaars": "bg", "Servisch": "sr", "Kroatisch": "hr", "Bosnisch": "bs",
    "Sloveens": "sl", "Oekraïens": "uk", "Wit-Russisch": "be",
    "Macedonisch": "mk", "Albanees": "sq", "Zweeds": "sv", "Noors": "no",
    "Deens": "da", "Fins": "fi", "IJslands": "is", "Ests": "et", "Lets": "lv",
    "Litouws": "lt", "Turks": "tr", "Arabisch": "ar", "Hebreeuws": "he",
    "Jiddisch": "yi", "Perzisch": "fa", "Koerdisch": "ku", "Chinees": "zh",
    "Japans": "ja", "Koreaans": "ko", "Hindi": "hi", "Urdu": "ur",
    "Bengaals": "bn", "Indonesisch": "id", "Maleis": "ms", "Thais": "th",
    "Vietnamees": "vi", "Afrikaans": "af", "Swahili": "sw", "Armeens": "hy",
    "Georgisch": "ka", "Catalaans": "ca", "Galicisch": "gl", "Baskisch": "eu",
    "Iers": "ga", "Welsh": "cy", "Bretons": "br", "Papiaments": "pap",
    "Fries": "fy", "Westerlauwers Fries": "fy", "Limburgs": "li",
    "Esperanto": "eo", "Sanskriet": "sa", "meerdere talen": "mul",
}
LANGUAGE_CODES = {fold(name): code for name, code in _LANGUAGE_CODES.items()}


def language_code(name: str | None) -> str | None:
    """'Nederlands' -> 'nl'. Unknown or ambiguous names return None."""
    return LANGUAGE_CODES.get(fold(name)) if name else None


def match_key(title: str | None, author: str | None) -> str:
    """Catalog-match key from title + first author surname token."""
    a = fold(author).split()
    return f"{fold(title)}|{a[-1] if a else ''}"


# Conservative series patterns — only explicit markers, to avoid false positives
# (e.g. "1984" or "Catch-22" must NOT be treated as series).
_SERIES_PATTERNS = [
    re.compile(r"^(?P<s>.+?)\s*[:\-–—]\s*deel\s*(?P<n>\d+)\b", re.I),
    re.compile(r"\(\s*(?P<s>[^()]+?)\s*[,;]?\s*deel\s*(?P<n>\d+)\s*\)", re.I),
    re.compile(r"\bdeel\s*(?P<n>\d+)\s+van\s+(?:de\s+)?(?:reeks|serie)\s+(?P<s>[^.()]+)", re.I),
]


def detect_series(title: str | None) -> tuple[str | None, int | None]:
    """Extract (series name, number) from a title when it has an explicit
    'deel N' marker; otherwise (None, None)."""
    if not title:
        return None, None
    for p in _SERIES_PATTERNS:
        m = p.search(title)
        if m:
            return m.group("s").strip(" :-,;"), int(m.group("n"))
    return None, None
