from dataclasses import asdict, dataclass, field


@dataclass
class Track:
    title: str
    artists: list[str] = field(default_factory=list)
    duration: int | None = None  # seconds
    album: str | None = None
    video_id: str | None = None  # YouTube Music
    album_id: str | None = None  # YouTube Music album browseId
    spotify_id: str | None = None
    kind: str = "song"  # "song", "album", "artist" or "playlist" (search/feed results)
    cover: str | None = None  # remote image URL; clients fetch it through /api/cover
    artist_id: str | None = None  # YouTube Music channel id of the first artist (or of an artist result)
    playlist_id: str | None = None

    @property
    def artist(self) -> str:
        return ", ".join(self.artists)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Track":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
