"""MCP tools for agents. Every tool runs as Deck("agent"): the owner's private songs, artists and albums never
appear in anything personal (library, feed, taste, likes, jobs), there are no file paths or timestamps, and
the privacy rules themselves can't be read or changed from here."""
import anyio
from mcp.server.mcpserver import MCPServer

from .service import Deck

INSTRUCTIONS = """tapedeck is the owner's private music service: anonymous YouTube Music catalog + Spotify links,
ad-free streaming, and downloads into their Navidrome library. You see it through an agent view:
- Anything the owner marked private is left out of their library, feed, taste and download history. That is
  deliberate. Don't try to work out what is hidden or why, and don't ask about it.
- Catalog search is public and unfiltered. Downloads you start are labelled as started by an agent.
- The owner decides what you may do (see my_permissions). If a tool says access is turned off, tell the user
  rather than working around it.
- Song ids are YouTube Music video ids (11 chars). Albums use MPRE… ids, artists UC… ids."""


async def _run(fn):
    return await anyio.to_thread.run_sync(fn)


def build_mcp() -> MCPServer:
    mcp = MCPServer("tapedeck", instructions=INSTRUCTIONS)

    @mcp.tool()
    async def search_music(query: str, kind: str = "songs", limit: int = 10) -> list[dict]:
        """Search YouTube Music anonymously. kind: songs | albums | artists."""
        return await _run(lambda: Deck("agent").search(query, kind, min(limit, 30)))

    @mcp.tool()
    async def genius_feed() -> dict:
        """The owner's home feed (Genius Mix, 'Because you played…', related artists, YouTube Music shelves),
        minus anything private."""
        return await _run(lambda: Deck("agent").feed())

    @mcp.tool()
    async def taste(limit: int = 10) -> list[dict]:
        """The owner's top artists from their own listening (names only, no play counts or times)."""
        return await _run(lambda: Deck("agent").taste(min(limit, 25)))

    @mcp.tool()
    async def library(limit: int = 50) -> list[dict]:
        """Songs tapedeck has downloaded into the owner's library (newest first), minus anything private."""
        return await _run(lambda: Deck("agent").library(min(limit, 200)))

    @mcp.tool()
    async def album(album_id: str) -> dict:
        """An album's tracks (album_id starts with MPRE)."""
        return await _run(lambda: Deck("agent").album(album_id))

    @mcp.tool()
    async def artist(artist_id: str) -> dict:
        """An artist's top songs, albums, singles and related artists (artist_id starts with UC)."""
        return await _run(lambda: Deck("agent").artist(artist_id))

    @mcp.tool()
    async def radio(video_id: str) -> list[dict]:
        """Up to 50 songs similar to one song (anonymous YouTube Music radio)."""
        return await _run(lambda: Deck("agent").radio(video_id))

    @mcp.tool()
    async def download(video_ids: list[str] | None = None, album_id: str | None = None,
                       spotify_link: str | None = None) -> dict:
        """Queue a download into the owner's Navidrome library: songs by video id, a whole album, or a public
        Spotify playlist/album/track link (matched to YouTube Music; unmatched songs are skipped, not guessed).
        Returns the job; check it with downloads_status."""
        items = [{"video_id": v} for v in video_ids or []]
        if album_id:
            items.append({"album_id": album_id})
        if spotify_link:
            items.append({"spotify": spotify_link})
        return await _run(lambda: Deck("agent").enqueue(items))

    @mcp.tool()
    async def my_permissions() -> dict:
        """What the owner currently lets agents do: library, feed, likes, download, jobs (true/false) and undo
        ("off", "own" = only downloads an agent started, or "all")."""
        return await _run(lambda: Deck("agent").permissions())

    @mcp.tool()
    async def undo_download(job_id: int) -> dict:
        """Delete the files one download job added to the library, if the owner's undo permission allows it."""
        return {"removed": await _run(lambda: Deck("agent").undo(job_id))}

    @mcp.tool()
    async def downloads_status(limit: int = 10) -> list[dict]:
        """Recent download jobs (queued/running/done/failed) with counts."""
        return await _run(lambda: Deck("agent").jobs(min(limit, 50)))

    return mcp
