"""Score a wanted track (e.g. from Spotify) against YouTube Music candidates.

Rules, in order:
- Version tags must agree on both sides: Live, Remix, Acoustic, Instrumental, Vocals Only / No Music / Acapella,
  Duff/Daf, Drums, With Music, 8D, Sped Up... They are read from brackets and " - " segments of the title, plus
  strong markers in the album name ("Live at", "Unplugged", "Vocals Only"). A vocals-only nasheed is never swapped for
  a version with music, and a live take never for the studio one.
- Credit/packaging tags (feat., with, Remastered, Radio Edit, From "Movie", Deluxe Edition...) are ignored.
- Titles must be equal once spacing/punctuation is ignored. Long titles (12+ chars) may differ slightly in spelling,
  but never in their last word ("Part I" vs "Part II") or in their numbers.
- Text folding: Latin accents, Arabic harakat, Turkish dotless i, Arabic/Persian/Urdu letter variants (ی/ي, ک/ك, ة/ه),
  tatweel and ﷺ. Indic vowel signs and kana dakuten are kept (they change the word).
- Artist: the wanted lead artist must appear in the candidate's credits (or the credits must be identical). Near names
  ("Drake" vs "Drake Bell", tribute acts) are rejected. Durations within 10 s.
Known edge: Spotify joins credits with commas, so a band named "Earth, Wind & Fire" also matches a candidate "Earth".
"""
import re
import unicodedata
from difflib import SequenceMatcher

from .models import Track

CREDIT = (r"feat|ft|featuring|with|prod|produced by|remaster(?:ed)?|digitally remastered|bonus track|single version|"
          r"album version|radio edit|edit|mono|stereo|official(?: audio| video| music video)?|audio|lyrics?|video|from|"
          r"soundtrack(?: version)?|original motion picture|motion picture|deluxe(?: edition| version)?|"
          r"anniversary(?: edition)?|studio version|original version|visualizer|explicit")
VARIANT = (r"vocals? only|voice only|vocals?|acapella|a cappella|acappella|no music|without music|with music|"
           r"instrumental|karaoke|acoustic|unplugged|live|remix|mix|slowed|sped up|speed up|reverb|cover|demo|"
           r"orchestral|nightcore|drums? only|drums?|duff|daff?|percussion|with instruments?|8d|spatial|bass boosted|"
           r"piano|lo-?fi|dub|reprise|medley|intro|mashup")
ALBUM_VARIANT = re.compile(r"\b(?:live (?:at|in|from|on)|unplugged|vocals? only|acapella|a cappella|no music|"
                           r"instrumental|karaoke|sped up|slowed|nightcore)\b", re.IGNORECASE)
TAG = re.compile(rf"\b(?:{CREDIT}|{VARIANT})\b", re.IGNORECASE)
VARIANT_RE = re.compile(rf"\b(?:{VARIANT})\b", re.IGNORECASE)
WHOLE_GROUP = re.compile(r"\s*(?:clean|clean version|explicit|hd|version)\s*", re.IGNORECASE)  # only as an entire bracket
BRACKETED = re.compile(r"[\(\[]([^\)\]]*)[\)\]]")
DASH = re.compile(r"\s+[-–—]\s+")
SPLIT_ARTISTS = re.compile(r"\s*(?:,|&|\band\b|\bfeat\.?|\bft\.?)\s*", re.IGNORECASE)
LETTERS = str.maketrans({"ı": "i", "ی": "ي", "ى": "ي", "ے": "ي", "ک": "ك", "ة": "ه", "ہ": "ه", "ۀ": "ه",
                         "ـ": "", "ﷺ": "", "&": " and "})
THRESHOLD = 0.8
ARTIST_GATE = 0.7
MAX_DRIFT = 10  # seconds


def _ignorable(part: str) -> bool:
    return bool(TAG.search(part) or WHOLE_GROUP.fullmatch(part))


def _canon(v: str) -> str:
    v = v.lower()
    if v.startswith(("vocal", "voice", "acapella", "a cappella", "acappella", "no music", "without music")):
        return "vocals"
    if v.startswith("drum"):
        return "drums"
    if v.startswith("da") or v == "duff":
        return "duff"
    if v.startswith("live"):
        return "live"
    if v.startswith("with instrument"):
        return "music"
    return {"with music": "music", "speed up": "sped up", "lo-fi": "lofi"}.get(v, v)


def _split(s: str) -> tuple[str, list[str]]:
    """Title with ignorable brackets/segments removed, and the removed parts."""
    removed = []

    def bracket(m):
        if _ignorable(m.group(1)):
            removed.append(m.group(1))
            return " "
        return f" {m.group(1)} "

    segments = DASH.split(BRACKETED.sub(bracket, s or ""))
    kept = [segments[0]]
    for seg in segments[1:]:
        (removed if _ignorable(seg) else kept).append(seg)
    return " ".join(kept), removed


def variants(title: str, album: str | None = None) -> set[str]:
    found = {_canon(v) for part in _split(title)[1] for v in VARIANT_RE.findall(part)}
    if album:  # albums only count strong markers; "(Music From The Motion Picture)" is not a version
        found |= {_canon(v) for v in ALBUM_VARIANT.findall(album)}
    return found


def _fold(s: str) -> str:
    out, base = [], ""
    for c in unicodedata.normalize("NFKD", s.casefold().translate(LETTERS)):
        if unicodedata.category(c) == "Mn" and (base < "ɐ" or "ً" <= c <= "ٟ" or c == "ٰ"):
            continue  # Latin accent or Arabic harakat
        if not unicodedata.category(c).startswith("M"):
            base = c
        out.append(c)
    return "".join(out)


def norm(s: str) -> str:
    s = _fold(_split(s)[0])
    s = "".join(c if c.isalnum() or unicodedata.category(c).startswith("M") else " " for c in s)
    s = re.sub(r"\b0+(?=\d)", "", s)  # "Track 01" == "Track 1"
    return " ".join(s.split())


def _key(name: str) -> str:
    name = re.sub(r"\s*-\s*topic$|vevo$", "", (name or "").strip(), flags=re.IGNORECASE)
    k = norm(name) or name.casefold()  # "!!!" stays "!!!"
    return k.removeprefix("the ")


def _names(artists: list[str]) -> list[str]:
    return [k for a in artists for k in (_key(x) for x in SPLIT_ARTISTS.split(a)) if k]


def _artist(want: Track, cand: Track) -> float:
    w, c = _names(want.artists), _names(cand.artists)
    if not w:
        return 0.5  # nothing to compare against
    if not c:
        return 0.0
    if w[0] in c or "".join(w).replace(" ", "") == "".join(c).replace(" ", ""):
        return 1.0
    return SequenceMatcher(None, " ".join(w), " ".join(c)).ratio() ** 2


def _title(want: str, cand: str) -> float:
    w, c = norm(want), norm(cand)
    if not w or not c or re.findall(r"\d+", w) != re.findall(r"\d+", c):
        return 0.0
    wc, cc = w.replace(" ", ""), c.replace(" ", "")
    if wc == cc:
        return 1.0
    if min(len(wc), len(cc)) < 12 or w.split()[-1] != c.split()[-1]:
        return 0.0  # short titles must be exact; long ones may only differ in spelling, not in their last word
    r = SequenceMatcher(None, wc, cc).ratio()
    return r if r >= 0.92 else 0.0


def score(want: Track, cand: Track) -> float:
    if variants(want.title, want.album) != variants(cand.title, cand.album):
        return 0.0
    title = _title(want.title, cand.title)
    artist = _artist(want, cand)
    if not title or (want.artists and artist < ARTIST_GATE):
        return 0.0
    if want.duration and cand.duration:
        drift = abs(want.duration - cand.duration)
        if drift > MAX_DRIFT:
            return 0.0
        return 0.5 * title + 0.35 * artist + 0.15 * (1 - drift / MAX_DRIFT)
    return (0.5 * title + 0.35 * artist) / 0.85


def best(want: Track, candidates: list[Track], threshold: float = THRESHOLD) -> Track | None:
    scored = [(score(want, c), c) for c in candidates]
    scored = [x for x in scored if x[0] >= threshold]
    return max(scored, key=lambda x: x[0])[1] if scored else None
