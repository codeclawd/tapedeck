"""Genius v3: your own home feed, anonymous, built the way YouTube's recommender is described.

Seeds come from what you do in tapedeck (finishes, likes, skips, downloads), never from an account or Navidrome.
Candidates come from anonymous YouTube Music (song radios, artist radios, related artists, artist pages), whose
radios already carry everyone's co-listening. Then one ranker scores every candidate, following "Deep Neural Networks
for YouTube Recommendations" (Covington et al., 2016) and "On YouTube's recommendation system" (2021):
- which sources nominated a song and how high (consensus across your radios wins);
- your history with its artist: how much you finish and like them versus skip them ("valued" listening, not clicks);
- churn: a song shown on earlier feeds that you didn't play sinks, so successive feeds aren't identical;
- explore/exploit (Spotify's bandit framing): a share of every list goes to artists you've never played.
Shelves: Quick picks, Listen again (favourites you haven't played lately), artist mixes, a time-of-day row, Because
you played (your most recent), New to you, new releases, albums, related artists, then YouTube Music's own shelves.
A cold start (no history yet) falls back to YouTube Music's anonymous home shelves, the same for everyone.
"""
import collections
import logging
import random
import re
import time

from .match import norm
from .outside import SOURCE_WEIGHT
from .models import Track

PICKS = 25
SHELF_SIZE = 20
SEEDS = 6
ARTIST_MIXES = 3
EXPLORE_SHARE = 0.2          # of Quick picks, from artists you have never played
CHURN_DECAY = 0.7            # score kept per earlier impression you didn't act on
LISTEN_AGAIN_AFTER = 3 * 86400
OUTSIDE_SEEDS = 3            # seeds whose Spotify/SoundCloud recommendations also vote
OUTSIDE_NEW_PER_SEED = 6     # songs only another service suggested, matched to YouTube Music, per seed and service
OUTSIDE_BUDGET = 20.0        # seconds a feed build may spend on other services; later seeds go YouTube-only
logger = logging.getLogger("tapedeck")

_VIDEO_WORDS = re.compile(
    r"\s*[\(\[]\s*(official[^\)\]]*|(music |lyric |lyrics |hd |4k )?video|visuali[sz]er|lyrics?|audio|"
    r"official audio|explicit video|mv|m/v)\s*[\)\]]|\s+[-|–]\s+(official|lyrics?|visuali[sz]er)\b.*$", re.I)


def clean_title(title: str) -> str:
    """"goosebumps (Official Video)" → "goosebumps". Only whole video tags go; "(Audio Bullys Remix)" stays."""
    return _VIDEO_WORDS.sub("", title or "").strip() or (title or "")


def _d(t: Track | dict) -> dict:
    return t.to_dict() if isinstance(t, Track) else dict(t)


def _lead(t: dict) -> str:
    return norm((t.get("artists") or [""])[0])


def _song_key(t: dict) -> str:
    """Same song, any upload: the video and its album track share this key."""
    return f"{norm(clean_title(t.get('title') or ''))}|{_lead(t)}"


def _near(t: dict, orbit: set[str]) -> bool:
    """A YTM card names its artists in `artists` (songs) or a "A, B and C" description (playlists)."""
    for a in t.get("artists") or []:
        if norm(a) in orbit:  # "Earth, Wind & Fire" is one artist
            return True
        if t.get("kind") == "playlist" and any(norm(n) in orbit for n in re.split(r",|&| and |•", a) if n.strip()):
            return True
    return False


def _year(v) -> int | None:
    m = re.match(r"\s*(\d{4})", str(v or ""))
    return int(m.group(1)) if m else None


def _history(store, keep: set[str]) -> dict:
    """Per song: last play time and the hours you play it; per artist: plays, finishes, skips. Only songs in `keep`
    (your scored history after privacy filtering) count, so a private song can't shape anything."""
    last: dict[str, float] = {}
    hours: dict[str, collections.Counter] = {}
    artists: dict[str, collections.Counter] = {}
    for r in store.db.execute("SELECT video_id, artist, artist_id, event, ts FROM events WHERE account=?",
                              (store.account,)):
        v = r["video_id"]
        if v not in keep:
            continue
        if r["event"] in ("play", "finish"):
            last[v] = max(last.get(v, 0.0), r["ts"])
            hours.setdefault(v, collections.Counter())[time.localtime(r["ts"]).tm_hour] += 1
        for k in {r["artist_id"], norm((r["artist"] or "").split(",")[0])} - {None, ""}:
            artists.setdefault(k, collections.Counter())[r["event"]] += 1
    return {"last": last, "hours": hours, "artists": artists}


def _artist_skip_rate(stats: collections.Counter | None) -> float:
    if not stats:
        return 0.0
    return stats["skip"] / (stats["play"] + stats["skip"] + 2)  # smoothed: one skip of a new artist isn't a verdict


def _bucket(hour: int) -> tuple[str, range | tuple]:
    if 5 <= hour < 12:
        return "Your mornings", range(5, 12)
    if 12 <= hour < 18:
        return "This afternoon", range(12, 18)
    if 18 <= hour < 23:
        return "Tonight", range(18, 23)
    return "Late night", (23, 0, 1, 2, 3, 4)


def pick_seeds(scores: dict[str, dict], rng: random.Random, n: int = SEEDS, pool: int = 12) -> list[dict]:
    """Weighted pick from the top of your history, one song per lead artist, so each refresh leans on different
    favourites and one artist can't take over the page."""
    top = sorted((e for e in scores.values() if e["score"] > 0 and e["track"].get("title")),
                 key=lambda e: -e["score"])[:pool]
    seeds, artists = [], set()
    while top and len(seeds) < n:
        e = rng.choices(top, weights=[x["score"] for x in top])[0]
        top.remove(e)
        if _lead(e["track"]) in artists:
            continue
        artists.add(_lead(e["track"]))
        seeds.append(e)
    return seeds


class _Pool:
    """Stage 1, candidate generation: every source nominates songs with a position score."""

    def __init__(self):
        self.c: dict[str, dict] = {}

    def add(self, source: str, tracks: list[dict], weight: float = 1.0):
        n = len(tracks)
        for pos, t in enumerate(tracks):
            v = t.get("video_id")
            if not v:
                continue
            e = self.c.setdefault(v, {"track": t, "sources": {}})
            e["sources"][source] = max(e["sources"].get(source, 0.0), weight * (1 - pos / (n + 1)))


class _Ranker:
    """Stage 2, ranking: one score per candidate from sources, your artist history and impressions."""

    def __init__(self, pool: _Pool, affinity: dict[str, float], hist: dict, shown: dict[str, float]):
        self.score: dict[str, float] = {}
        for v, e in pool.c.items():
            t, src = e["track"], e["sources"]
            lead = _lead(t)
            s = sum(src.values()) + 0.5 * (len(src) - 1)                       # nominated by more, ranked higher
            s += 0.2 * max(min(affinity.get(lead, 0.0), 5.0), -5.0)            # your history with the artist
            stats = hist["artists"].get(t.get("artist_id")) or hist["artists"].get(lead)
            s -= 1.5 * _artist_skip_rate(stats)                                 # ...and how often you skip them
            if s > 0:                                                           # shown before, not played:
                s *= CHURN_DECAY ** shown.get(v, 0.0)                           # each impression costs 30 %
            self.score[v] = s

    def order(self, tracks: list[dict]) -> list[dict]:
        return sorted(tracks, key=lambda t: -self.score.get(t.get("video_id"), -99))


class _Used:
    """Songs already placed on the page, by id and by song key (another upload of the same song)."""

    def __init__(self, banned: set[str], known_keys: set[str]):
        self.banned, self.ids, self.keys = banned, set(), set(known_keys)

    def free(self, t: dict) -> bool:
        v = t.get("video_id")
        return bool(v) and v not in self.banned and v not in self.ids and _song_key(t) not in self.keys

    def mark(self, t: dict):
        if t.get("video_id"):
            self.ids.add(t["video_id"])
            self.keys.add(_song_key(t))

    def take(self, tracks, n: int, per_artist: int = 3) -> list[dict]:
        """Up to n free album tracks, at most `per_artist` per lead artist. An upload with no album is a video or a
        fan re-upload; its song version, when there is one, is in the radios too."""
        out, artists = [], collections.Counter()
        for t in tracks:
            if len(out) >= n:
                break
            if self.free(t) and t.get("album") and artists[_lead(t)] < per_artist:
                artists[_lead(t)] += 1
                self.mark(t)
                out.append(t)
        return out


def _song_seed(ytm, track: dict, exclude) -> dict:
    """A music video's radio is all videos; radio from its album track instead, unless that track is private."""
    if track.get("album") or not hasattr(ytm, "song_version"):
        return track
    try:
        song = ytm.song_version({**track, "title": clean_title(track.get("title") or "")})
    except Exception:
        logger.exception("no song version for %s; using the video", track.get("video_id"))
        return track
    if not song or (exclude and exclude(song)):
        return track
    return song


def build(store, ytm, owned: set[str], rng: random.Random | None = None, exclude=None,
          shown: dict[str, float] | None = None, outside=None, resolve=None, outside_artist=None,
          personal_home=None) -> dict:
    """exclude(track_dict) -> True drops that history entry before anything is derived from it. The agent feed
    passes the privacy filter here, so a private song can't even shape what agents are recommended.
    shown: video_id -> how often earlier feeds showed it (decayed); those sink unless you played them.
    outside(seed) -> {service: [tracks]} and resolve(track) -> YouTube Music audio track: other services' votes
    for the first OUTSIDE_SEEDS seeds (outside.py). outside_artist(seed): the same for artist mixes."""
    rng = rng or random.Random()
    shown = shown or {}
    now = time.time()
    scores = store.track_scores(owned)
    if exclude:
        scores = {v: e for v, e in scores.items() if not exclude(e["track"])}
    hist = _history(store, set(scores))
    banned = store.recent_skips() | store.disliked() | set(scores)  # nothing you already know comes back as new
    known_keys = {_song_key(e["track"]) for e in scores.values() if e["track"].get("title")}
    if hasattr(store, "titles"):  # disliked songs aren't in scores; their other uploads mustn't come back either
        known_keys |= {_song_key(t) for t in store.titles(store.disliked()) if not (exclude and exclude(t))}
    affinity: dict[str, float] = {}
    for e in scores.values():
        for a in e["track"].get("artists") or []:
            affinity[norm(a)] = affinity.get(norm(a), 0.0) + e["score"]
    known_artists = set(affinity) | set(hist["artists"])
    played_albums = {norm(e["track"].get("album") or "") for e in scores.values()} - {""}

    seeds = pick_seeds(scores, rng)
    shelves: list[dict] = []
    used = _Used(banned, known_keys)
    from .ytm import cover_url  # local import keeps genius testable with a fake ytm

    if seeds:
        pool = _Pool()
        radios: list[tuple[dict, list[dict]]] = []

        def radio_for(entry: dict, source: str) -> list[dict]:
            seed = _song_seed(ytm, entry["track"], exclude)
            try:
                tracks = [_d(t) for t in ytm.radio(seed["video_id"])]
            except Exception:
                logger.exception("radio for seed %s failed; skipping it", seed.get("video_id"))
                return []
            tracks = [t for t in tracks if t.get("video_id") not in (seed["video_id"], entry["track"]["video_id"])]
            weight = max(entry["score"], 0.1) ** 0.5
            pool.add(source, tracks, weight)
            if outside and source.startswith("radio:") and outside_seeds[0] > 0:
                outside_seeds[0] -= 1
                vote_outside(seed, source, weight)
            return tracks

        def vote_outside(seed: dict, source: str, weight: float, ask=None) -> list[dict]:
            """Other services' answers for the same seed vote too (Spotify's radio, SoundCloud's related tracks; or
            Spotify's artist radio for a mix). A song already in the pool gains that service's vote, so agreement
            ranks higher; a few new ones are matched to their YouTube Music audio track and join. Returns them."""
            if time.monotonic() > outside_deadline:  # a cold match cache mustn't make the first feed take minutes
                return []
            try:
                votes = (ask or outside)(seed)
            except Exception:
                logger.exception("outside recommendations for %s failed", seed.get("video_id"))
                return []
            by_key = {_song_key(e["track"]): e["track"] for e in pool.c.values()}
            seed_key, joined = _song_key(seed), []
            for name, found in votes.items():
                mapped, new = [], 0
                for t in found:
                    k = _song_key(t)
                    if k == seed_key or k.startswith("|") or k.endswith("|"):  # no title or no artist: unmatchable
                        continue
                    hit = by_key.get(k)
                    if not hit and resolve and new < OUTSIDE_NEW_PER_SEED:
                        new += 1
                        hit = resolve(t)
                    if hit:
                        mapped.append(hit)
                pool.add(f"{name}:{source.split(':', 1)[-1]}", mapped, weight * SOURCE_WEIGHT.get(name, 0.5))
                joined += mapped
            return joined

        outside_seeds = [OUTSIDE_SEEDS]
        outside_deadline = time.monotonic() + OUTSIDE_BUDGET

        for s in seeds:
            tracks = radio_for(s, f"radio:{s['track']['video_id']}")
            if tracks:
                radios.append((s, tracks))

        # Artist mixes: YouTube Music's artist radio, led by that artist with the artists around them.
        top_artists, seen_ids = [], set()
        for s in sorted(seeds, key=lambda s: -affinity.get(_lead(s["track"]), 0.0)):
            aid = s["track"].get("artist_id")
            if aid and aid not in seen_ids:
                seen_ids.add(aid)
                top_artists.append(s)
        pages: dict[str, dict] = {}

        def page(aid: str) -> dict:
            if aid not in pages:
                try:
                    pages[aid] = ytm.artist(aid) or {}
                except Exception:
                    logger.exception("artist page for %s unavailable", aid)
                    pages[aid] = {}
            return pages[aid]

        artist_mixes = []
        for s in top_artists[:ARTIST_MIXES]:
            aid = s["track"]["artist_id"]
            rid = page(aid).get("radioId")
            tracks = []
            if rid and hasattr(ytm, "artist_radio"):
                try:
                    tracks = [_d(t) for t in ytm.artist_radio(rid)]
                except Exception:
                    logger.exception("artist mix for %s unavailable", aid)
            pool.add(f"artist:{aid}", tracks, 0.8)
            if outside_artist:  # Spotify's artist radio for the same artist joins the mix
                have = {t.get("video_id") for t in tracks}
                for t in vote_outside(s["track"], f"artist:{aid}", 0.8, ask=outside_artist):
                    if t.get("video_id") not in have:
                        have.add(t.get("video_id"))
                        tracks.append(t)
            if tracks:
                artist_mixes.append((s, tracks))

        # A row for this time of day: radio from what you play at this hour (YouTube Music's moments and moods).
        moment = None
        title, hours = _bucket(time.localtime(now).tm_hour)
        at_hour = collections.Counter()
        for v, hs in hist["hours"].items():
            n = sum(c for h, c in hs.items() if h in hours)
            if n and v in scores and scores[v]["score"] > 0:
                at_hour[v] = n * scores[v]["score"]
        seed_ids = {s["track"]["video_id"] for s in seeds}
        for v, _ in at_hour.most_common(3):
            if v not in seed_ids:
                tracks = radio_for(scores[v], "moment")
                if tracks:
                    moment = (title, scores[v], tracks)
                break

        # New to you (explore): top songs of related artists you have never played.
        explore: list[dict] = []
        lead = top_artists[0] if top_artists else None
        related = []
        if lead:
            related = [r for r in (page(lead["track"]["artist_id"]).get("related") or {}).get("results") or []
                       if r.get("browseId")]
            from .ytm import _track  # the artist page's song rows have the radio row shape
            for r in [r for r in related if norm(r.get("title") or "") not in known_artists][:5]:
                for x in ((page(r["browseId"]).get("songs") or {}).get("results") or [])[:3]:
                    try:
                        if x.get("videoId"):
                            explore.append(_d(_track(x)))
                    except Exception:
                        logger.exception("unreadable song row on artist %s; skipped", r.get("browseId"))
            pool.add("explore", explore, 0.7)

        rank = _Ranker(pool, affinity, hist, shown)

        # Quick picks: the top of the ranking, with a fixed share for artists you've never played.
        ranked = rank.order([e["track"] for e in pool.c.values()])
        # Exploit first (artists you play, songs you haven't heard), then a fixed explore share (Spotify's bandit
        # framing); only if your artists run out does the rest come from new artists.
        new_slots = int(PICKS * EXPLORE_SHARE)
        picks = used.take([t for t in ranked if _lead(t) in known_artists], PICKS - new_slots, per_artist=3)
        picks += used.take([t for t in ranked if _lead(t) not in known_artists], PICKS - len(picks), per_artist=1)
        picks = rank.order(picks)
        if picks:
            shelves.append({"id": "genius-mix", "title": "Quick picks", "subtitle": "Picked for you, right now",
                            "items": picks})

        # Listen again: favourites you haven't played for a while, longest-missed first among the most loved.
        again = []
        for v, e in scores.items():
            if v not in hist["last"]:
                continue  # liked or downloaded but never played: not "again"
            gap = now - hist["last"][v]
            if e["score"] > 0 and e["track"].get("title") and gap >= LISTEN_AGAIN_AFTER:
                again.append((e["score"] * min(gap / 86400, 30) ** 0.5, {**e["track"], "kind": "song"}))
        again = [t for _, t in sorted(again, key=lambda x: -x[0])][:SHELF_SIZE]
        if again:
            shelves.append({"id": "listen-again", "title": "Listen again", "subtitle": "Favourites you haven't played lately",
                            "owner_only": True, "items": again})

        for s, tracks in artist_mixes:
            name = (s["track"].get("artists") or ["This artist"])[0]
            items = used.take(rank.order(tracks), SHELF_SIZE, per_artist=5)
            if items:
                shelves.append({"id": f"mix-{s['track']['artist_id']}", "title": f"{name} Mix",
                                "subtitle": f"{name} and artists like them", "seed": s["track"], "items": items})

        if moment:
            title, seed, tracks = moment
            items = used.take(rank.order(tracks), SHELF_SIZE)
            if items:
                shelves.append({"id": "moment", "title": title,
                                "subtitle": f"Like {clean_title(seed['track']['title'])}, which you play around now",
                                "seed": seed["track"], "items": items})

        # Because you played: your most recent listens, like the watch-next that follows what you just watched.
        by_seed = {s["track"]["video_id"]: (s, t) for s, t in radios}
        recent = []
        for v in sorted(hist["last"], key=lambda v: -hist["last"][v]):
            if len(recent) >= 2:
                break
            if v not in scores or scores[v]["score"] <= 0 or not scores[v]["track"].get("title"):
                continue
            if v in by_seed:
                recent.append(by_seed[v])
            else:
                tracks = radio_for(scores[v], f"recent:{v}")
                if tracks:
                    recent.append((scores[v], tracks))
        for s, tracks in recent:
            items = used.take(rank.order(tracks), SHELF_SIZE)
            if items:
                shelves.append({"id": f"because-{s['track']['video_id']}",
                                "title": f"Because you played {clean_title(s['track']['title'])}",
                                "subtitle": ", ".join(s["track"].get("artists") or []),
                                "seed": s["track"], "items": items})

        items = used.take(rank.order(explore), SHELF_SIZE, per_artist=2)
        if items and lead:
            shelves.append({"id": "new-to-you", "title": "New to you",
                            "subtitle": f"Artists you haven't played, near {(lead['track'].get('artists') or [''])[0]}",
                            "seed": lead["track"], "items": items})

        # New from artists you play: singles and albums from this year or last, taking turns between artists.
        this_year = time.gmtime(now).tm_year
        by_artist: dict[str, list] = {}
        seen_albums: set[str] = set()
        for s in top_artists[:5]:
            aid = s["track"]["artist_id"]
            name = (s["track"].get("artists") or [page(aid).get("name") or ""])[0]
            for kind in ("singles", "albums"):
                for r in (page(aid).get(kind) or {}).get("results") or []:
                    year, bid = _year(r.get("year")), r.get("browseId")
                    if not bid or bid in seen_albums or not year or year < this_year - 1:
                        continue
                    seen_albums.add(bid)
                    by_artist.setdefault(aid, []).append((year, {
                        "title": r.get("title") or "", "kind": "album", "album_id": bid, "album": r.get("title") or "",
                        "artists": [name], "artist_id": aid, "cover": cover_url(r.get("thumbnails")), "year": str(year)}))
        groups = [[a for _, a in sorted(g, key=lambda x: -x[0])][:3] for g in by_artist.values()]
        turns = [g[i] for i in range(3) for g in groups if i < len(g)]
        if turns:
            shelves.append({"id": "new-releases", "title": "New from artists you play",
                            "subtitle": "Singles and albums from this year and last", "items": turns[:SHELF_SIZE]})

        # Albums for you: the albums the ranking keeps landing on, minus ones you already play.
        album_votes: dict[str, dict] = {}
        for t in ranked[:150]:
            if t.get("video_id") in banned or _song_key(t) in known_keys:
                continue
            aid, name = t.get("album_id"), t.get("album")
            if not aid or not name or norm(name) in played_albums or aid in seen_albums:
                continue
            a = album_votes.setdefault(aid, {"score": 0.0, "item": {
                "title": name, "kind": "album", "album_id": aid, "album": name, "artists": (t.get("artists") or [])[:1],
                "artist_id": t.get("artist_id"), "cover": t.get("cover")}})
            a["score"] += max(rank.score.get(t["video_id"], 0.0), 0.0)
        albums = [a["item"] for a in sorted(album_votes.values(), key=lambda a: -a["score"]) if a["score"] > 0][:12]
        if albums:
            shelves.append({"id": "albums-for-you", "title": "Albums for you",
                            "subtitle": "Where your radios keep landing", "items": albums})

        if lead and related:
            shelves.append({"id": f"artists-like-{lead['track']['artist_id']}",
                            "title": f"Artists like {(lead['track'].get('artists') or [page(lead['track']['artist_id']).get('name')])[0]}",
                            "seed": lead["track"],
                            "items": [{"title": r.get("title") or "", "kind": "artist", "artist_id": r.get("browseId"),
                                       "cover": cover_url(r.get("thumbnails")), "artists": []} for r in related]})

    orbit = set(known_artists)
    if seeds:
        orbit |= {_lead(e["track"]) for e in pool.c.values()}

    # personal_home(): the home page of the owner's own anonymous visitor, which YouTube Music tunes from the plays
    # tapedeck reports to it (service.report_play). Everyone else gets the anonymous home, the same for all.
    home, personal = [], False
    if personal_home:
        try:
            home, personal = personal_home(), True
        except Exception:
            logger.exception("your YouTube Music home shelves are unavailable; using the anonymous ones")
    if not personal:
        try:
            home = ytm.home()
        except Exception:
            logger.exception("YouTube Music home shelves unavailable")
            home = []
    for i, h in enumerate(home[: (3 if seeds else 6)]):
        items = []
        for t in (_d(x) for x in h["items"]):
            if not t.get("video_id"):
                items.append(t)
            elif used.free(t):
                used.mark(t)
                items.append(t)
        if seeds:  # anonymous shelves are for everyone: keep what features artists in your orbit
            items = [t for t in items if _near(t, orbit)]
            if len(items) < 3:
                continue
        if items:
            if personal:
                title, subtitle = h["title"], "From YouTube Music, tuned to your plays"
            else:
                # Anonymous YouTube Music isn't about you: don't let its "Quick picks" pass for yours.
                title = "Popular on YouTube Music" if seeds and h["title"].lower() == "quick picks" else h["title"]
                subtitle = "From YouTube Music, no account"
            shelves.append({"id": f"ytm-{i}", "title": title, "subtitle": subtitle, "items": items})

    return {"generated": time.time(), "cold_start": not seeds, "shelves": shelves}
