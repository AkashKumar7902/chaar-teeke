"""Text to a comparable form: the same fold is applied to what a caller said and to every gazetteer name.

A transcript can be Devanagari (Hindi, Dogri), Perso-Arabic (Urdu) or Latin (typed). Each word is turned
into folded Latin so that "Gandhi Nagar", "गांधीनगर" and "گاندھی نگر" meet at one spelling. The fold is lossy
on purpose (aspirates, long vowels, w/v, doubled letters), and a consonant skeleton ("phonetic key") is kept
for names that differ only in their vowels.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_ARABIC = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")

_ALIASES = {
    "nagr": "nagar",
    "mohala": "moh",
    "mohla": "moh",
    "mohalla": "moh",
    "muhala": "moh",
    "koloni": "kolni",
    "kolni": "kolni",
}


@dataclass(frozen=True)
class NormText:
    latin: str  # folded Latin, tokens separated by one space
    devanagari: str  # folded Devanagari words joined by one space ("" when none were spoken in Devanagari)
    phonetic: str  # consonant skeleton per token, joined by one space
    tokens: tuple[str, ...]

    @property
    def latin_joined(self) -> str:
        return self.latin.replace(" ", "")

    @property
    def devanagari_joined(self) -> str:
        return self.devanagari.replace(" ", "")

    @property
    def distinctive(self) -> tuple[str, ...]:
        return tuple(t for t in self.tokens if is_distinctive(t))

    def __bool__(self) -> bool:
        return bool(self.tokens)


# ----------------------------------------------------------------------------- Latin fold
_DIGRAPHS = (
    ("sh", "s"),
    ("ph", "f"),
    ("bh", "b"),
    ("dh", "d"),
    ("gh", "g"),
    ("kh", "k"),
    ("th", "t"),
    ("jh", "j"),
    ("zh", "j"),
)
_VOWEL_PAIRS = (
    ("aa", "a"),
    ("ee", "i"),
    ("ii", "i"),
    ("oo", "u"),
    ("uu", "u"),
    ("au", "o"),
    ("ou", "o"),
    ("ai", "e"),
    ("ei", "e"),
    ("ay", "e"),
)


def fold_word(word: str) -> str:
    """Fold one lowercase Latin word."""
    w = unicodedata.normalize("NFKD", word.lower())
    w = "".join(c for c in w if not unicodedata.combining(c))
    w = re.sub(r"[^a-z0-9]", "", w)
    if not w or w.isdigit():
        return w
    w = re.sub(r"c?chh?", "\x01", w)
    for a, b in _DIGRAPHS:
        w = w.replace(a, b)
    w = re.sub(r"[ao]w(?=k|$)", "o", w)
    for a, b in _VOWEL_PAIRS:
        w = w.replace(a, b)
    w = w.replace("x", "ks").replace("q", "k").replace("z", "j").replace("w", "v").replace("c", "k")
    w = w.replace("\x01", "c")
    w = re.sub(r"(.)\1+", r"\1", w)
    if len(w) > 2 and w.endswith("y"):
        w = w[:-1] + "i"
    return _ALIASES.get(w, w)


def phonetic_word(folded: str) -> str:
    """Consonant skeleton of a folded word: the first letter, then no vowels and no h."""
    if not folded or folded.isdigit():
        return folded
    return folded[0] + re.sub(r"[aeiouyh]", "", folded[1:])


# ------------------------------------------------------------------------ Devanagari to Latin
_VOWELS_IAST = set("aāiīuūṛeo")


def _schwa(word: str) -> str:
    """Hindi drops the inherent 'a' at the end of a word (nagara -> nagar). The 'a' between syllables is
    kept: dropping it right is a matter of stress and clusters, and the fuzzy match absorbs the difference."""
    if (
        len(word) > 1
        and word.endswith("a")
        and word[-2] not in _VOWELS_IAST
        and any(c in _VOWELS_IAST for c in word[:-1])
    ):
        return word[:-1]
    return word


_DEVA_PRE = str.maketrans({"ॉ": "ो", "ॅ": "े", "ऑ": "ओ", "ँ": "ं", "ऽ": ""})


def _deva_to_latin(word: str) -> str:
    from indic_transliteration import sanscript

    w = unicodedata.normalize("NFC", word).translate(_DEVA_PRE)
    iast = sanscript.transliterate(w, sanscript.DEVANAGARI, sanscript.IAST)
    # IAST writes ch as c and chh as ch; spell them out as they are typed in Latin.
    iast = iast.replace("ch", "\x02").replace("c", "ch").replace("\x02", "chh")
    iast = iast.replace("ṃ", "n").replace("ḥ", "h")
    return _schwa(unicodedata.normalize("NFC", iast))


def _deva_key(word: str) -> str:
    """A Devanagari word with the spelling differences that do not change how it sounds folded away."""
    w = unicodedata.normalize("NFD", word)
    w = w.replace("़", "")  # nukta
    w = re.sub("[ँंःऽ]", "", w)  # chandrabindu, anusvara, visarga, avagraha
    w = unicodedata.normalize("NFC", w)
    w = w.replace("ॉ", "ो").replace("ॅ", "े")
    w = w.translate(str.maketrans("ीूईऊ", "िुइउ"))
    return re.sub(r"[^ऀ-ॿ]", "", w)


# --------------------------------------------------------------------------- Urdu to Latin
_URDU = {
    "ا": "a", "آ": "a", "ب": "b", "پ": "p", "ت": "t", "ٹ": "t", "ث": "s", "ج": "j", "چ": "c", "ح": "h",
    "خ": "k", "د": "d", "ڈ": "d", "ذ": "j", "ر": "r", "ڑ": "r", "ز": "j", "ژ": "j", "س": "s", "ش": "s",
    "ص": "s", "ض": "j", "ط": "t", "ظ": "j", "ع": "a", "غ": "g", "ف": "f", "ق": "k", "ک": "k", "ك": "k",
    "گ": "g", "ل": "l", "م": "m", "ن": "n", "ں": "n", "و": "u", "ؤ": "u", "ہ": "h", "ۃ": "h", "ھ": "h",
    "ء": "", "ی": "i", "ي": "i", "ے": "e", "ئ": "i", "ۓ": "e", "ە": "h",
}  # fmt: skip


def _urdu_to_latin(word: str) -> str:
    out = []
    for ch in unicodedata.normalize("NFC", word):
        if unicodedata.category(ch).startswith("M"):
            continue  # harakat
        out.append(_URDU.get(ch, ""))
    w = "".join(out)
    return w[:-1] if len(w) > 2 and w.endswith("h") and not w.endswith(("ch", "sh", "kh", "gh")) else w


# -------------------------------------------------------------------------------- the entry
def _tokens(text: str) -> list[str]:
    """Words: letters, combining marks (matras) and digits; everything else separates."""
    text = unicodedata.normalize("NFC", text)
    kept = "".join(c if unicodedata.category(c)[0] in "LMN" else " " for c in text)
    return [part for word in kept.split() for part in re.findall(r"[0-9]+|[^0-9]+", word)]


def is_distinctive(token: str) -> bool:
    """A folded word that can help identify a place: not a place type or filler, and not a lone letter
    (a lone digit is a ward number and counts)."""
    return token not in GENERIC and (len(token) > 1 or token.isdigit())


def raw_words(text: str) -> frozenset[str]:
    """The lower-cased words as written, before any folding."""
    return frozenset(w.lower() for w in _tokens(text or ""))


def normalise(text: str) -> NormText:
    """Fold a transcript or a name. Pure; never raises on odd input (an empty result means 'nothing heard')."""
    latin: list[str] = []
    deva: list[str] = []
    for tok in _tokens(text or ""):
        if _DEVANAGARI.search(tok):
            for part in re.findall(r"[ऀ-ॿ]+|[^ऀ-ॿ]+", tok):
                if _DEVANAGARI.search(part):
                    key = _deva_key(part)
                    if key:
                        deva.append(key)
                    latin.append(fold_word(_deva_to_latin(part)))
                else:
                    latin.append(fold_word(part))
        elif _ARABIC.search(tok):
            latin.append(fold_word(_urdu_to_latin(tok)))
        else:
            latin.append(fold_word(tok))
    toks = tuple(t for t in latin if t)
    return NormText(
        latin=" ".join(toks),
        devanagari=" ".join(deva),
        phonetic=" ".join(phonetic_word(t) for t in toks),
        tokens=toks,
    )


def alias_text(text: str) -> str:
    """What the reviewer's 'remember this name' stores: the heard words in their own script, without filler
    such as 'in' or 'near'. It is folded again whenever the index is built, so it stays valid if the fold
    improves. Empty when nothing but filler and place types was said."""
    kept = [w for w in _tokens(text or "") if fold_word_any(w) not in FILLER][:6]
    text_out = " ".join(kept).lower()
    return text_out if normalise(text_out).distinctive else ""


def fold_word_any(word: str) -> str:
    """The folded Latin of one word in any script."""
    return normalise(word).latin


# Words that name a kind of place or filler in a sentence. They never identify a place on their own.
_GENERIC_WORDS = """
    nagar colony kolni koloni moh bajar road rd marg lane gali chok mandi mohala ward vard no number
    school skul hospital aspatal govt gov government sarkari near paas pas ke ki ka ko se mai men mein me
    main tha thi the hai ha hu hun ho in at the a an of by next side opposite opp bhi aur and or i was
    bite bitten kata kati kate dog kutta kutte ne mujhe muje mera meri mere hum ham yahan vahan idhar
    udhar wahan area city jammu tehsil chowk bus stand station park temple mandir masjid gurdwara
    ghar home house bahar andar samne piche upar niche par wala wali
    बस स्टैंड स्टेशन स्कूल अस्पताल सड़क बाजार बाज़ार चौक मंदिर मस्जिद गुरुद्वारा पार्क नगर कॉलोनी मोहल्ला रोड
    के पास में का की को से पर घर नंबर वार्ड मुझे ने काटा कुत्ते कुत्ता
    """.split()

# Said around a place name, not part of it: dropped when a reviewer teaches the system a new name.
_FILLER_WORDS = """
    ke ki ka ko se mai men mein me main tha thi the hai ha hu hun ho in at the a an of by near paas pas
    next side mujhe muje mera meri mere ne kata kati kate bite bitten dog kutta kutte i was and aur or bhi
    yahan vahan idhar udhar wahan area
    के पास में का की को से मुझे ने काटा कुत्ते कुत्ता यहाँ वहाँ और भी था थी है
    """.split()


def _fold_all(words: list[str]) -> frozenset[str]:
    return frozenset(normalise(w).latin for w in words)


GENERIC = _fold_all(_GENERIC_WORDS)
FILLER = _fold_all(_FILLER_WORDS)
