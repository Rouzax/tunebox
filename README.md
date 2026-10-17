# tunebox

A self-hosted web radio player: a small radio API (now playing, song
history, cover art) and a standalone browser player built for slow
in-car browsers and phones.

This first release is the radio API. It polls each channel's now-playing
feed, finds a matching cover for every track, and serves what is playing,
what comes next and what played recently. A small status page shows the
result; the player itself follows in a later release.

## What it does

- Polls the now-playing feed once per channel, however many players are
  open, and keeps the last 50 tracks per channel. The list survives a
  restart.
- Switches the title at about the moment you hear the song, not when the
  station announces it: each track counts as heard a configurable number of
  seconds after it started.
- Looks up a cover for every track on Deezer, then iTunes, and accepts one
  only when both the title and one of the artists match. Without a song
  cover it falls back to an artist photo, and without either the track has
  no cover.
- Recognises station IDs and voice-overs, and splits "(Played by ...)"
  credits off the title.
- Offers an endpoint in the format PawTunes' "custom" stats source reads,
  so an existing PawTunes install can use it.

Only Triton Digital now-playing feeds are supported as a source so far.

## Run it with Docker

You need Docker with the compose plugin.

1. Create a directory for the stack and a `data` directory inside it, owned
   by uid 1000 (the container runs as that user and keeps its database
   there):

   ```sh
   mkdir -p tunebox/data && cd tunebox
   sudo chown 1000:1000 data
   ```

2. Put the example configuration in place. It defines three channels; edit
   it as needed (see [Configuration](#configuration)):

   ```sh
   curl -fsSL -o data/tunebox.toml https://raw.githubusercontent.com/Rouzax/tunebox/main/tunebox.example.toml
   curl -fsSL -o compose.yaml https://raw.githubusercontent.com/Rouzax/tunebox/main/compose.example.yaml
   ```

3. Start it and open the status page at `http://127.0.0.1:8080/`:

   ```sh
   docker compose up -d
   docker compose logs -f
   ```

The first log line is `"event": "started"`, followed by `cover_found` and
`cover_miss` lines as covers are looked up. Within a minute the status page
lists each channel's current track, the next one and recent ones.

What gets created: `data/tunebox.db` and SQLite's `data/tunebox.db-wal` and
`data/tunebox.db-shm`. They hold the recent lists and the cover decisions;
deleting them only means covers are looked up again.

The host port in `compose.example.yaml` (`127.0.0.1:8080`) is an example:
publish any free port. The container port stays 8080, and the container's
health check assumes it. The port is bound to loopback because a reverse
proxy on the host is expected to provide TLS and the public name. Another
container on the same Docker network reaches tunebox as `http://tunebox:8080`.

## Configuration

tunebox reads one TOML file, `/data/tunebox.toml` in the container (see
`TUNEBOX_CONFIG` below). A missing or invalid file stops the server at
startup with a message naming the key, for example
`tunebox: channels[0].source.mount: required`, and exit code 2.

### `[server]`

| Key | Default | Meaning |
|---|---|---|
| `host` | `"0.0.0.0"` | Address to listen on inside the container. |
| `port` | `8080` | Port to listen on. Keep 8080 in the container; the health check uses it. |
| `data_dir` | the directory of the config file | Where `tunebox.db` is kept. |

### `[defaults]`

These apply to every channel; a channel can override any of them by setting
the same key in its own table.

| Key | Default | Meaning |
|---|---|---|
| `delay_s` | `7` | Seconds after a track's start time at which the API reports it as current. The player adds how much audio it has buffered on top. |
| `pawtunes_delay_s` | `28` | The same for `/api/pawtunes/<id>`. PawTunes' own player does not add its buffer, so this delay includes it. |
| `poll_interval_s` | `12` | Seconds between polls of the feed, plus up to 3 s of random jitter. After a failed poll the wait doubles each time, up to 120 s. |
| `history_size` | `50` | Tracks kept per channel. |

Both delays were measured on two track changes; if titles change before or
after you hear the song, adjust them.

### `[[channels]]`

| Key | Required | Meaning |
|---|---|---|
| `id` | yes | Used in URLs. Lower-case letters, digits and `-`. |
| `name` | yes | Shown when no track is playing (during a station ID). |
| `stream_url` | yes | The audio stream, `http://` or `https://`. Passed to the player as is. |
| `default_image` | no | An image URL for the player to show without a cover. |
| `source` | yes | Where now-playing data comes from: `{ type = "triton", mount = "<feed mount>" }`. |

`[channels.rules]` (optional) describes the channel's track titles. Without
it nothing is treated as a station ID or a credit.

| Key | Default | Meaning |
|---|---|---|
| `station_id` | none | A regular expression. An entry with no artist whose title matches it is a station ID: it is not shown as a track and not kept in history. |
| `played_by` | none | A regular expression for a credit at the end of the title. Its first group is kept as `played_by`; the rest is removed from the title. |
| `artist_separator` | `","` | What separates several artists. Only this splits: `&`, `.` and `/` inside a name are kept. |

The example file sets both patterns for the Tomorrowland channels.

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `TUNEBOX_CONFIG` | `/data/tunebox.toml` | Path to the configuration file. |
| `TUNEBOX_WEB` | the `web/` directory next to the code | Static files served at `/`. |
| `LOG_LEVEL` | `info` | A level, optionally followed by per-module levels: `info,tunebox.covers=debug`. |

## API

All responses are JSON. Channel ids come from the configuration.

### `GET /api/channels`

The configured channels. Browsers may cache it for five minutes.

```json
[{"id": "owr", "name": "One World Radio",
  "stream_url": "https://playerservices.streamtheworld.com/api/livestream-redirect/OWR_INTERNATIONAL_ADP.aac",
  "default_image": null}]
```

### `GET /api/channels/<id>/now`

What is playing now, what comes next, and what played before, for a player
that plays `buffer_s` seconds behind the live stream:
`/api/channels/owr/now?buffer_s=30`. A browser playing a live stream is
typically about 30 s behind, because the stream starts with a burst of past
audio; measure it as `audio.buffered.end(0) - audio.currentTime` and measure
again after every reconnect. Without `buffer_s` only the server's `delay_s`
applies. Values outside 0 to 120 are a `400` with `{"error": "bad_buffer_s"}`.

```json
{"current": {"artist": "CAMELPHAT, JOSH GIGANTE, KUUDA",
             "artists": ["CAMELPHAT", "JOSH GIGANTE", "KUUDA"],
             "title": "So Good", "played_by": null,
             "started_at": 1792217024107,
             "cover": "https://cdn-images.dzcdn.net/images/cover/.../1000x1000-000000-80-0-0.jpg",
             "cover_kind": "album"},
 "upcoming": {"track": {"...": "..."}, "in_s": 14},
 "history": [{"...": "..."}],
 "stale_since": null}
```

- `current` is `null` during a station ID; show the channel name then.
- `upcoming` is the next track, already announced but not heard yet, and
  `in_s` is the number of seconds until it becomes current. The countdown is
  relative, so a wrong clock on the device does not matter. `upcoming.track`
  is `null` when a station ID comes next.
- `history` is newest first, without station IDs and without the current
  and upcoming tracks.
- `cover` is a Deezer or iTunes image URL, or `null` while the lookup is
  running or when nothing matched. `cover_kind` is `album`, `artist` (an
  artist photo) or `null`.
- `stale_since` is the time (epoch ms) the feed stopped answering, or
  `null`. While it is set, the channel keeps serving its last data.
- The response has an `ETag`. Send it back in `If-None-Match` and an
  unchanged answer is `304 Not Modified` with no body. The countdown is not
  part of the comparison, so a 304 means: keep your own deadline.

### `GET /api/pawtunes/<id>`

The same data in the format PawTunes' "custom" stats source reads. See
[Using it with PawTunes](#using-it-with-pawtunes).

### `GET /healthz`

`200` whenever the server runs, with each channel's last successful poll
and `stale_since`. An unreachable feed does not make it fail.

### Everything else

Static files from the web directory; `/` is `index.html`. Unknown paths
are `404` with `{"error": "not_found"}`, unknown channels
`{"error": "unknown_channel"}`.

## Using it with PawTunes

In a PawTunes channel, set the stats method to "custom" and the URL to
`https://<your-host>/api/pawtunes/<channel id>` (or
`http://tunebox:8080/api/pawtunes/<channel id>` from a container on the
same Docker network).

PawTunes then shows tunebox's tracks with their "(Played by ...)" credits,
its history (up to 20 entries, without station IDs) and its covers. During
a station ID the artist is empty and the title is the ID code, which
PawTunes shows with its default artist name.

A track's artwork is fixed the first time PawTunes sees the track: if no
cover was known yet, PawTunes falls back to its own artwork sources for that
track even if tunebox finds one later. Changing the artwork while a track
plays would make PawTunes count it as a new track.

## Troubleshooting

| What you see | Likely cause | What to do |
|---|---|---|
| The container exits right away; the log says `tunebox: <key>: <reason>` | The configuration file is missing or a key is wrong. | Fix the named key. The path is `TUNEBOX_CONFIG` (default `/data/tunebox.toml`). |
| `tunebox: cannot listen on 0.0.0.0:8080` | Something else uses the port. | Change `server.port`, or the host side of the published port. |
| `store_unavailable` in the log | The data directory is not writable by uid 1000. | `chown 1000:1000` the data directory. tunebox keeps running, but forgets its lists and covers on restart. |
| The title changes before or after you hear the song | The delay does not match this stream or player. | Adjust `delay_s` (tunebox player) or `pawtunes_delay_s` (PawTunes). |
| Tracks have no cover | The track is in neither catalogue, or the catalogues were unreachable. | Look for `cover_miss` and `search_failed` in the log. A failed search is retried after 10 minutes; a confident miss after 3 days. |
| `stale_since` is set or the status page says "feed unreachable" | The now-playing feed does not answer. | Nothing to do; the channel keeps its last data and polls again, backing off to every 2 minutes. |

## Development

You need [uv](https://docs.astral.sh/uv/).

```sh
uv venv --python 3.14 .venv
uv pip install --python .venv --require-hashes -r requirements-dev.txt
.venv/bin/pre-commit install
```

The commit hook runs ruff, pyright (strict) and vulture; the push hook runs
the tests with coverage. Run them by hand with:

```sh
.venv/bin/pre-commit run --all-files
.venv/bin/pre-commit run --all-files --hook-stage pre-push
```

Tests never touch the network, except one marked `live`:
`.venv/bin/pytest -m live`.

To run the server from a checkout:

```sh
mkdir -p data && cp tunebox.example.toml data/tunebox.toml
TUNEBOX_CONFIG=data/tunebox.toml .venv/bin/python -m tunebox
```

Dev tool versions are pinned in `requirements-dev.in` and compiled with
hashes into `requirements-dev.txt` (`uv pip compile requirements-dev.in
--python-version 3.14 --generate-hashes -o requirements-dev.txt`). The
server itself uses only the Python standard library.

## License

Licensed under the GNU Affero General Public License v3.0; see
[LICENSE](LICENSE).
