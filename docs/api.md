# tapedeck HTTP API (v0.2)

Base URL: `http://<your-server>:4540` (the default port; change it with `tapedeck serve --port` or `TAPEDECK_PORT`). Plain JSON over HTTP; no HTTP/2, websockets or WebP anywhere, so an iOS 13 client works too.

## Auth and scope
- **Sign in with a username and password** (like Sonarr/Radarr): `POST /api/login {username, password}` → `{token, account, username}`. The token is this device's session; send it like any other token. `POST /api/logout` ends it. 401 = wrong pair, 429 = 8 wrong tries from one address in 10 minutes. Set logins on the server: `tapedeck account add NAME` (asks for a password), `tapedeck account password NAME [--username U]` (NAME can be `owner`; a new password signs that account's devices out). `whoami` also returns `username`.
- Every `/api/*` route except `/api/health` and `/api/login` needs a token: `Authorization: Bearer <token>`. Media players that can't set headers (AVPlayer URLs, `<audio>`, `<img>`) use `?t=<token>` instead.
- **Owner token** (`TAPEDECK_OWNER_TOKEN`): sees everything. Use it in your own client apps and the web UI.
- **Agent token** (`TAPEDECK_AGENT_TOKEN`): the agent view.
  - Anything private is left out of personal lists (library, feed, likes, taste, jobs). There are no file paths and no history.
  - Privacy rules and events return 403.
  - Songs an agent asked for itself are never hidden from it, because hiding them would reveal a rule.
  - Agents always get proxied audio, never the owned file.
  - Everything else agents may do (library, feed, likes, downloads, activity, undo) follows the owner's permissions at `/api/permissions`.
- **Person tokens** (`tapedeck account add NAME`, printed once; only a hash is stored): owner scope, but their own account. Each person has their own listening events, history, likes, taste and Genius feed. The downloaded library is shared; a person sees and undoes only the download jobs they started. Privacy rules and agent permissions belong to the `owner` account (agents only ever see it), so they are 403 for anyone else. `agent` and `owner` are reserved names. `tapedeck account list|remove NAME`; a removed person's token gets 401 at once.
- The owner token is the `owner` account; agents always see the `owner` account, filtered.
- The env tokens live in `~/.config/tapedeck/env` on the server. Print one with `tapedeck token --show owner|agent`.
- `/mcp` is MCP Streamable HTTP for agents. It always runs in agent scope.

Errors are `{"error": "..."}` with:
- 400: bad input;
- 401: no or wrong token;
- 403: owner-only, or a permission the owner turned off;
- 404: not found;
- 409: undo of a job that is still running;
- 502: YouTube or Spotify upstream, including "YouTube refused the audio".

## Track object
Search results, feed items and album tracks all have this shape:
```json
{"title": "Loser", "artists": ["Tame Impala"], "duration": 224, "album": "Deadbeat",
 "video_id": "MX7FSAnRPug", "album_id": "MPREb_…", "artist_id": "UC…", "playlist_id": null,
 "spotify_id": null, "kind": "song", "cover": "https://lh3.googleusercontent.com/…=w544-h544-l90-rj",
 "owned": false, "liked": false}
```
- `kind` is one of `song | album | artist | playlist`.
- Songs have a `video_id`. Albums have an `album_id`, artists an `artist_id`, playlists a `playlist_id`.
- `owned` and `liked` appear only on songs.
- Never load `cover` directly from the phone. Load it as `/api/cover?u=<urlencoded cover>&s=<px>&t=<token>`. That returns JPEG or PNG, and Google never sees the phone.

## Routes
| Method | Path | Returns |
|---|---|---|
| GET | `/api/health` | `{ok, name:"tapedeck", version}`. No token needed; use it to detect the server. |
| GET | `/api/whoami` | `{scope, account, permissions}` |
| GET | `/api/lyrics/{video_id}` | `{source, synced, lines: [{t, text}]}`; `t` is seconds, or null when not synced. Timed lyrics win (YouTube Music, then LRCLIB), then plain. 404 when none. Only title and artist go to LRCLIB. |
| GET, PUT | `/api/permissions` | agent permissions `{library, feed, likes, download, jobs: bool, undo: "off"\|"own"\|"all"}`; PUT (owner only) changes any subset |
| POST | `/api/check-stream` | owner: test that YouTube audio is reachable from the server's network now → `{youtube, detail}` |
| GET | `/api/status` | version, yt_dlp, downloads, queue, navidrome, feed_age, permissions, `youtube: {youtube, at, detail}` (owner also gets private_rules, events, library, vpn_switch_requested) |
| GET | `/api/feed[?refresh=1]` | `{generated, cold_start, shelves:[{id, title, subtitle?, items:[Track]}]}` |
| GET | `/api/search?q=&kind=songs\|albums\|artists&limit=` | `[Track]` |
| GET | `/api/search?q=&kind=all&limit=` | `{songs:[Track], albums:[Track], artists:[Track]}` from one upstream search (~0.5 s vs ~1.3 s for three). Use this for a merged results list. |
| GET | `/api/album/{album_id}` | `{album_id, title, year, artists, cover, tracks:[Track]}` |
| GET | `/api/artist/{artist_id}` | `{artist_id, name, cover, songs, albums, singles, related}` |
| GET | `/api/playlist/{playlist_id}` | `{playlist_id, title, cover, tracks}` |
| GET | `/api/radio/{video_id}` | `[Track]`, up to 50 similar songs |
| GET | `/api/track/{video_id}` | Track |
| GET | `/api/spotify?link=` | `{name, truncated, tracks}` (Spotify's own data, not matched yet) |
| GET | `/api/stream/{video_id}` | `audio/mp4` (AAC). Supports `Range` (206). Owned songs are served from disk. |
| GET | `/api/cover?u=&s=` | JPEG or PNG, Google image hosts only |
| GET | `/api/library?limit=` | tapedeck's downloads, newest first (owner also gets `path`) |
| GET | `/api/likes` | `[Track]` |
| GET | `/api/taste` | top artists `[{name, artist_id, score?}]` |
| GET | `/api/history?limit=` | your own events, newest first: `[{id, ts, video_id, title, artist, album, cover, event}]` (person tokens only) |
| DELETE | `/api/history` | clear your listening history (plays, finishes, skips). Likes and dislikes stay. Genius rebuilds from what's left. → `{removed}` |
| DELETE | `/api/history/{id}` | remove one history entry → `{removed: 1}` or 404 |
| GET | `/api/autoplay/{video_id}?exclude=id,id&limit=10` | what to play after this song when the queue ends: YouTube Music's Up Next re-ranked by your taste, with recent plays, skips, dislikes and `exclude` (your queue) removed, max 2 per artist, about 1 in 4 from a favourite's radio. Top 2 are prefetched. |
| POST | `/api/events` | owner only. Body `{"track": Track, "event": "play\|finish\|skip\|like\|unlike\|dislike"}`. |
| POST | `/api/downloads` | Body `{"items":[{"video_id"}\|{"album_id"}\|{"playlist_id"}\|{"spotify": link}], "label"?}`. Returns 202 with the job. |
| GET | `/api/jobs?limit=` | `[{id, status: queued\|running\|done\|failed\|undone, label, actor, total, done, failed, created, finished}]` |
| POST | `/api/jobs/{id}/undo` | owner only. Deletes exactly the files that job wrote. |
| GET, POST | `/api/private` | owner only. Lists rules, or adds one with body `{kind: song\|artist\|album, key, ref?, label?}`. |
| DELETE | `/api/private/{id}` | owner only |

### Events feed the Genius algorithm
- Send `play` when a song starts.
- Send `skip` only when the listener moves on before **30 s** (Spotify's rule). A listen past 30 s is never a skip.
- Send `finish` when it ends naturally, or after 90% of it.
- Send `like`, `unlike` and `dislike` from the UI.
- Include the whole Track, so the feed can show titles and covers without another lookup.

Genius tunes itself: after 3 new listening events (plays included), the owner's feed rebuilds in the background, so the next open is re-ranked and still instant. Otherwise it rebuilds every 30 min, or on `?refresh=1`.

### Faster first play
A song's first play needs a YouTube lookup (~3 s). After that it's cached and starts in ~0.2 s.
- The server already looks up the first songs of every feed build and the top 3 search results.
- Clients should call `POST /api/prefetch {"video_ids": [...]}` (up to 10, returns 202 `{queued}`) with the next 2 songs in the queue when a song starts, and with the first songs of a shelf or album the user opens.

### Privacy rule keys
| Kind | `key` | `ref` (optional) |
|---|---|---|
| song | video_id | |
| artist | the artist's name (normalised on the server) | artist_id |
| album | album_id | album title |

## The server's network
YouTube sometimes refuses audio to some networks (403). tapedeck tracks this (`/api/status` → `youtube`). The first refusal runs `TAPEDECK_ON_STREAM_BLOCKED` from the env file (at most once every 2 h), for example a script that notifies you. `tapedeck check-stream` exits 0 when YouTube audio is reachable.

## Autoplay (Spotify-style "keep playing")
When the queue runs out, call `/api/autoplay/{last song}` with `exclude` = the ids already played or queued in this session, append the results, and call it again when those run low. Spotify's own recommendation endpoints are closed to new apps (2024-11-27), so the candidates are YouTube Music's Up Next; the ranking is yours (Genius).
