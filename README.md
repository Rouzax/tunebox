# tunebox

tunebox is a self-hosted web radio player. You run it on your own server
and listen in a browser: in the car, on a phone or on a computer. It
follows your internet radio stations and always knows which song is
playing, which one is next and which ones played before, with a matching
album cover for each.

It is built for listening in the car: titles switch when you actually hear
the new song, not half a minute early, the page fits the car's narrow and
wide windows, and a dropped connection (a tunnel, a 4G handover) is picked
up again without a tap.

tunebox can also feed an existing PawTunes player; you do not need PawTunes
to use it.

## What you get

For listeners:

- **A player for every screen.** Stations in groups with their logos, a
  quality choice per station (for example a lower bitrate on 4G), recently
  played songs with covers, and a layout that puts the cover above the
  title in a narrow window and beside it in a wide one. Long titles scroll.
- **Day and night.** A button cycles automatic, day and night; night dims
  everything but the title.
- **Visualizer.** Twelve styles, from spectrum bars to a ring around the
  cover and slow ambient colour fields, coloured from the album cover. It
  switches itself to a lighter mode on slow hardware.
- **Lock screen and media keys.** Phones and cars show the song and cover;
  next and previous change station.
- **Small window and embedding.** Open the player in a small pop-out window,
  or put it on another web page.
- **Reconnects by itself** when the stream drops, with pauses that grow to
  a minute.

Behind the player:

- **Now playing, next and recently played** for every station, the same on
  every device. The recent list (the last 50 songs) survives a restart.
- **Covers you can trust.** tunebox looks each song up on Deezer, then on
  iTunes, and only accepts a cover when both the title and an artist match.
  When no song cover exists it shows a photo of the artist instead, and when
  there is neither, no picture rather than the wrong one.
- **Tidy titles.** Station jingles and voice-overs are recognised and not
  shown as songs. Credits such as "(Played by ... at Tomorrowland)" are
  split off the title so a player can show them on their own line.
- **Titles in time with the music.** You set how many seconds after a song
  starts it counts as heard; a player adds how far it plays behind the live
  stream.

Supported stations: those whose now-playing information comes from Triton
Digital (One World Radio and the other Tomorrowland channels, Radio 538,
Radio 10, Sky Radio, Veronica, SLAM!), Qmusic, and NPO Radio 1, 2 and 3FM.
The example configuration has the three Tomorrowland channels ready to go;
`examples/stations-nl.toml` has 50 tested stations to copy from.

## Before you start

You need:

- A Linux machine with **Docker** and the **Docker Compose** plugin.
- About 100 MB of disk for the image, plus a few MB for its data.
- Outgoing internet access to the radio station, Deezer and iTunes.
- Optional: a reverse proxy (such as Nginx Proxy Manager, Caddy or Traefik)
  if you want to reach tunebox from your phone or car over HTTPS. Without
  one, tunebox is reachable only from the machine it runs on.

## Install

1. Create a folder for tunebox with a `data` folder inside it. tunebox runs
   as user id 1000 and must be able to write to `data`:

   ```sh
   mkdir -p tunebox/data && cd tunebox
   sudo chown 1000:1000 data
   ```

2. Download the example configuration and the compose file:

   ```sh
   curl -fsSL -o data/tunebox.toml https://raw.githubusercontent.com/Rouzax/tunebox/main/tunebox.example.toml
   curl -fsSL -o compose.yaml https://raw.githubusercontent.com/Rouzax/tunebox/main/compose.example.yaml
   ```

   The configuration already contains One World Radio, Daybreak Sessions
   and Tomorrowland Anthems. You can [change the stations](#change-the-stations)
   later.

3. If port 8080 is already used on this machine, open `compose.yaml` and
   change the first `8080` in `"127.0.0.1:8080:8080"` to a free port, for
   example `"127.0.0.1:8085:8080"`. Leave the second `8080` as it is.

4. Start it:

   ```sh
   docker compose up -d
   ```

Docker downloads the image `ghcr.io/rouzax/tunebox:latest` and starts it in
the background. It restarts automatically after a reboot.

## Check that it works

Open the status page, `http://127.0.0.1:8080/status.html`, in a browser on
the same machine (use your own port if you changed it in step 3).

You should see one section per station with the song playing now, often the
next one, and the five songs before it. Covers fill in during the first
minute or two: tunebox looks them up one at a time so it stays within
Deezer's and iTunes' limits.

To see what tunebox is doing:

```sh
docker compose logs -f
```

A healthy start looks like this (one line per event):

```
{"ts": "...", "level": "info", "logger": "tunebox", "event": "started", "port": 8080, "channels": ["owr", "daybreak", "anthems"]}
{"ts": "...", "level": "info", "logger": "tunebox.covers", "event": "cover_found", "artist": "SONNY FODERA, CHRYSTAL", "title": "My Loving", ...}
```

`docker compose ps` shows the container as `healthy` after about half a
minute.

If the container stops right away or the page does not load, see
[Troubleshooting](#troubleshooting).

Then open the player itself at `http://127.0.0.1:8080/`.

`/check.html` reports what a browser supports: the audio formats it plays,
its dark mode setting, its window size and the features the player uses.
Open it on the device you want to listen on (a phone, a car) when something
does not work there.

## Use the player

Open `http://<your host>:<port>/` (or the address your reverse proxy gives
it). What the buttons do:

- **Play / stop** (middle). Stop drops the stream, so play always starts at
  what is on air now.
- **Recently played** (clock, bottom left) and **Stations** (radio). On the
  stations page, tap a station to switch; under the playing station you can
  pick its quality, for example "Data saver (AAC 96)" for 4G.
- **Volume and mute.** Narrow windows (the car's third, phones) and phones
  that do not let a web page set the volume (iPhones) show only mute; use
  the device's own volume control there.
- **Visualizer** (bars icon), **small window** (box with arrow; not on
  touch screens) and **fullscreen**.
- **Day/night** (top left) cycles automatic, day and night. Automatic
  follows sunrise and sunset when `[player]` has a location, otherwise the
  device's dark mode setting.

The station, quality, volume, visualizer and day/night choice are
remembered in this browser only; nothing about listeners is stored on the
server.

Good to know:

- Browsers do not start sound until you have tapped the page once. After a
  reload the player starts again if it was playing; if the browser refuses,
  press play.
- "Reconnecting..." means the stream dropped. The player keeps trying with
  pauses of 2 seconds up to a minute; you do not need to do anything.
- The visualizer starts after your first tap or key press on the page.
- Opening a station nobody listened to for a while takes a moment: tunebox
  checks its feed first, so what you see is what is on now.

## Embed the player on another page

```html
<iframe src="https://radio.example/?channel=owr&embed=1" width="420" height="640" allow="autoplay; fullscreen"></iframe>
```

Replace `radio.example` with your own address. `channel=` is any station
`id` from `data/tunebox.toml`. `embed=1` gives the compact layout: no
day/night, fullscreen or small-window buttons, and the cover always above
the title. The small-window button opens this same address.

## Reach it from other devices

The compose file only accepts connections from the machine itself, on
purpose: phones and cars should reach tunebox through your reverse proxy,
which adds HTTPS. The player needs HTTPS outside your own network: browsers
block some features on plain-http pages from elsewhere. In your proxy, forward a hostname to
`http://127.0.0.1:8080` (or the port you chose).

If your proxy runs in Docker on the same network as tunebox, it can use
`http://tunebox:8080` instead.

## Use it with PawTunes

You only need this while you still use PawTunes. tunebox can supply the
song titles, history and covers for an existing PawTunes player.

1. In PawTunes' admin panel, open the channel's settings.
2. Set the stats (now playing) method to **custom**.
3. Set the URL to tunebox's PawTunes address for that station:
   `https://<your-host>/api/pawtunes/<station id>`, for example
   `https://radio.example.com/api/pawtunes/anthems`. If PawTunes runs in a
   container on the same Docker network, `http://tunebox:8080/api/pawtunes/anthems`
   works too.

The station ids are the `id` values in `data/tunebox.toml`: `owr`,
`daybreak` and `anthems` in the example.

What changes in PawTunes:

- Titles switch about when you hear the song, and keep their
  "(Played by ...)" credits.
- History shows up to 20 songs, without jingles.
- Covers come from tunebox. A song's cover is decided the first time
  PawTunes sees the song; if tunebox had not found one yet, PawTunes uses
  its own artwork for that song. (Changing the cover while a song plays
  would make PawTunes list the song twice.)
- During a station jingle PawTunes shows its default artist with the jingle
  code; the PawTunes car theme shows the station name instead.

## Change the stations

`examples/stations-nl.toml` in the repository lists 50 Dutch and
Tomorrowland stations that were tested with tunebox (streams, now-playing
data, logos). Copy the `[[channels]]` blocks you want into
`data/tunebox.toml`. Each block ends where the next `[[channels]]` starts;
its `[[channels.streams]]` and `[channels.rules]` tables belong to it.

Stations are listed in `data/tunebox.toml`, one `[[channels]]` block each:

```toml
[[channels]]
id = "owr"                       # short name used in addresses
name = "One World Radio"         # shown during jingles
group = "Tomorrowland"
always = true
source = { type = "triton", mount = "OWR_INTERNATIONAL" }

[[channels.streams]]
name = "High"
url = "https://playerservices.streamtheworld.com/api/livestream-redirect/OWR_INTERNATIONAL_ADP.aac"
codec = "aac"
kbps = 256

[[channels.streams]]
name = "Compatible"
url = "https://playerservices.streamtheworld.com/api/livestream-redirect/OWR_INTERNATIONAL.mp3"
codec = "mp3"
kbps = 192

[channels.rules]
station_id = '^[A-Z]\d{4} \d{2} [A-Za-z]{3} \d{4}\b'
played_by = '\s*\(Played by ([^)]*)\)\s*$'
```

- **To remove a station**, delete its block.
- **To add a Triton station**, copy a block and change `id`, `name`, the
  stream `url`s and `mount`. The `mount` is the station's name in Triton's
  now-playing feed. For the Tomorrowland channels it is the stream's name
  without `_ADP`. Check that you have the right one by opening this address
  in a browser; it should list recent songs:

  ```
  https://np.tritondigital.com/public/nowplaying?mountName=OWR_INTERNATIONAL&numberToFetch=5&eventType=track
  ```

- **Other source types.** `source = { type = "qmusic", station = "qnl_top40" }`
  reads Qmusic's play history (station ids such as `qmusic_nl`,
  `qnl_nonstop`, `qnl_foute_uur`, `qnl_top40`, `qnl_thema`).
  `source = { type = "npo", site = "nporadio2" }` reads the track list on
  NPO's site (`nporadio1`, `nporadio2` or `npo3fm`). `examples/stations-nl.toml`
  has every tested station ready to copy.
- **Streams** are the quality choices listeners see; the first one is the
  default. Every `url` must be `https://`: the player page is served over
  https and browsers refuse plain-http audio on it. `codec` (`aac` or
  `mp3`) and `kbps` are shown in the choice, for example "High (AAC 256)".
  Set `kbps` to the stream's real bitrate: tunebox also uses it to work
  out how far behind live the stream starts (see
  [Fine-tune when titles change](#fine-tune-when-titles-change)).
  A single `stream_url = "..."` still works and becomes one stream named
  "Default".
- **`group`** puts stations under a heading on the channel page.
- **`always = true`** keeps tunebox checking the station while nobody
  listens. Without it, a station is only checked while someone has listened
  to it in the last ten minutes; its history then has gaps.
- **`[channels.rules]`** tells tunebox how a station writes its titles. The
  two patterns in the example match Tomorrowland's jingle codes (such as
  `E1700 16 Oct 2026 M19 VO`) and its "(Played by ...)" credits. For
  another station you can leave the block out; every entry is then shown as
  a song.

After editing, apply the change with:

```sh
docker compose restart
```

If the file has a mistake, the container stops and the log names the line
it did not accept, for example `tunebox: channels[2].source.mount: required`.

## Fine-tune when titles change

Every station sends a song's title some seconds before you hear it, and how
many differs by station and by player. Two settings in `data/tunebox.toml`
control the switch:

```toml
[defaults]
delay_s = 7            # for tunebox's own player (it adds its own buffer)
pawtunes_delay_s = 28  # for PawTunes
```

- The title changes **before** the song: raise the number.
- The title changes **after** the song: lower it.

tunebox's own player adds how far behind live it plays, which differs by
stream: a station sends the last 10 to 30 seconds of audio at once when a
player connects, so the player starts that far behind. tunebox measures
that burst itself, the first time a stream is played and then once a day,
by reading the stream for a few seconds. The log shows each result as
`burst_measured`. A stream without `kbps` is not measured, and its titles
change that much early.

Restart tunebox after a change. Both defaults come from a few measured
song changes, so small adjustments are normal. You can also set a different
value for one station by adding `delay_s` or `pawtunes_delay_s` to its
`[[channels]]` block.

## Update, back up and reset

- **Update** to the latest release:

  ```sh
  docker compose pull && docker compose up -d
  ```

- **Back up**: everything worth keeping is in the `data` folder. Only
  `tunebox.toml` matters; the database is rebuilt on its own.
- **Reset** the recent lists and covers: stop tunebox, delete
  `data/tunebox.db`, `data/tunebox.db-wal` and `data/tunebox.db-shm`, and
  start it again. History refills from the stations and covers are looked
  up again. Your configuration is not touched.
- **Logos** are kept in `data/logos/`, as downloaded. Delete that folder to
  download them again at the next start. The player loads them the first
  time a listener opens the station list, once per browser, so a large logo
  costs mobile data only then; to keep that small, point `logo` at a small
  image (around 100 px is plenty).

## Troubleshooting

| What you see | Likely cause | What to do |
|---|---|---|
| The container stops right away; the log says `tunebox: <something>: <reason>` | A mistake in `data/tunebox.toml`, or the file is missing. | Fix what the message names, then `docker compose up -d`. |
| The log says `tunebox: cannot listen on 0.0.0.0:<port>` | `port` in `[server]` was changed to one that is not free inside the container. | Remove `port` from `[server]` (8080 is the default) and change the published port in `compose.yaml` instead. |
| `docker compose up` complains the port is already allocated | Another program on the machine uses that port. | Choose a free port as in [Install](#install) step 3. |
| `store_unavailable` in the log | The `data` folder is not writable by user id 1000. | `sudo chown 1000:1000 data` and restart. Until then tunebox works but forgets everything on restart. |
| The status page shows no covers for some songs | The song is in neither Deezer nor iTunes (common for unreleased tracks), or the catalogues could not be reached. | Nothing to do for unreleased tracks. A failed lookup is retried after 10 minutes, a song that was not found after 3 days. Look for `search_failed` in the log. |
| A station's name on the status page says "(feed unreachable)" | The station's now-playing feed does not answer. | Nothing to do: tunebox keeps showing the last known songs and keeps trying, at most every 2 minutes. |
| The title changes too early or too late | The delay does not fit this station or player. Stations other than the Tomorrowland channels start at the default and often need their own value. | See [Fine-tune when titles change](#fine-tune-when-titles-change). |
| No sound, and the ring around the stop button keeps spinning ("Reconnecting...") | The stream cannot be reached from this device or network, or the browser cannot play it. | The player keeps trying. Try another quality on the stations page; open `/check.html` on the device to see which formats it plays. To see why, open the player with `?debug=1` added to its address (for example `https://radio.example.com/?debug=1`) and press play: a panel lists what the browser reports about the stream, with its own error text and the player's reconnect decisions. Copy puts the list on the clipboard. The panel stays in that browser; nothing is sent to tunebox. |
| "This browser cannot play AAC" (or MP3) | The browser lacks that codec (common for AAC in some Linux browsers). | Pick a quality with the other codec, usually "Compatible" (MP3). |
| No visualizer | It starts after the first tap or key press on the page. A stream served without the cross-origin permission plays without it ("No visualizer for this stream"). | Tap the page once. Nothing to do for such a stream. |
| A station shows only its name, no song | Its feed answered slowly or not at all when you opened it (tunebox does not show a song it cannot confirm is on now). | Wait for the next update (10 seconds). If it stays empty, `/healthz` shows `stale_since` for that station. |
| PawTunes shows nothing new | PawTunes cannot reach the URL. | Open the same `/api/pawtunes/<id>` address in a browser from the PawTunes machine; you should see the current song. |

For more detail in the log, set `LOG_LEVEL: info,tunebox.covers=debug` in
`compose.yaml` and restart.

## Reference

### Configuration file

`/data/tunebox.toml` in the container. Unknown keys are rejected, so a typo
is reported at startup instead of being ignored.

`[server]`

| Key | Default | Meaning |
|---|---|---|
| `host` | `"0.0.0.0"` | Address to listen on inside the container. |
| `port` | `8080` | Port to listen on. Keep 8080 in the container; the health check uses it. |
| `data_dir` | the folder of the config file | Where `tunebox.db` is kept. |

`[defaults]` (each can also be set per station)

| Key | Default | Meaning |
|---|---|---|
| `delay_s` | `7` | Seconds after a song's start at which `/api/channels/<id>/now` reports it as current. |
| `pawtunes_delay_s` | `28` | The same for `/api/pawtunes/<id>`. |
| `poll_interval_s` | `12` | Seconds between checks of a station's feed, plus up to 3 s of random spread. After a failure the wait doubles each time, up to 120 s. Only while the station is active (see `always`). |
| `history_size` | `50` | Songs kept per station. |

`[player]` (optional)

| Key | Default | Meaning |
|---|---|---|
| `latitude`, `longitude` | none | Where day/night "automatic" takes sunrise and sunset from. Give both or neither. Without them "automatic" follows the browser's dark mode setting. |
| `default_channel` | the first channel | The channel a first-time listener starts on. |

`[[channels]]`

| Key | Required | Meaning |
|---|---|---|
| `id` | yes | Used in addresses. Lower-case letters, digits and `-`. |
| `name` | yes | Shown during jingles. |
| `streams` | yes, or `stream_url` | One or more `[[channels.streams]]` tables: `name` (required), `url` (required, `https://`), `codec` (`aac` or `mp3`), `kbps` (whole number). The first is the default. |
| `stream_url` | instead of `streams` | One stream, `http://` or `https://`, named "Default". An `http://` stream only plays when the player page itself is plain http (on your own network); browsers block it on an https page. |
| `default_image` | no | An image address for the player to show when a song has no cover. |
| `source` | yes | `{ type = "triton", mount = "<feed mount>" }`, `{ type = "qmusic", station = "<station id>" }` or `{ type = "npo", site = "nporadio1" \| "nporadio2" \| "npo3fm" }`. |
| `group` | no | Heading on the channel page. |
| `logo` | no | `https://` address of the station logo. tunebox downloads it once into the data folder and serves it from there, so listeners' browsers never contact that address. PNG, JPEG, WebP or SVG, up to 2 MB. |
| `always` | no, default `false` | Check the station even while nobody listens. |

`[channels.rules]` (optional)

| Key | Default | Meaning |
|---|---|---|
| `station_id` | none | A regular expression. An entry with no artist whose title matches it is a jingle: not shown as a song and not kept in history. |
| `played_by` | none | A regular expression for a credit at the end of a title. Its first group (in parentheses) is kept as the credit; the match is removed from the title. |
| `artist_separator` | `","` | What separates several artists. Only this splits: `&`, `.` and `/` inside a name are kept. |

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `TUNEBOX_CONFIG` | `/data/tunebox.toml` | Path to the configuration file. |
| `LOG_LEVEL` | `info` | Log detail: a level, optionally with per-part levels, such as `info,tunebox.covers=debug`. |
| `TUNEBOX_WEB` | the built-in `web` folder | Folder with the files served at `/`. |
| `TZ` | | Time zone, as in the compose file. Log times are always UTC. |

### HTTP API

For player developers. All answers are JSON.

**`GET /api/channels`** lists the stations: `id`, `name`, `stream_url`,
`default_image`, `group`, `streams` (each with `name`, `url`, `codec`,
`kbps`; `stream_url` is the first one's address) and `logo` (a path such as
`/logos/owr.png?v=18df62ef34e6b8a7`, or `null` until the logo is downloaded
or when none is configured). It may be cached for
five minutes.

**`GET /logos/<file>`** serves a downloaded logo. Browsers may keep it for a
year: the `v` in the path from `/api/channels` changes whenever the logo is
downloaded again, so use that path as given. SVG logos are sent with a
policy that keeps them from running script.

**`GET /api/channels/<id>/now?buffer_s=<seconds>&stream=<index>`** answers what is playing:

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

- `buffer_s` (0 to 120, optional) is how far the player has fallen behind
  the moment it asked for the stream: the seconds since it set the audio
  source, minus `audio.currentTime`. Start the clock again on every
  reconnect. tunebox adds it to `delay_s`. An invalid value is a `400`
  with `{"error": "bad_buffer_s"}`. (`audio.buffered` is no measure of
  this: Chrome keeps only a couple of seconds there.)
- `stream` (optional) is the index of the stream playing, in the channel's
  `streams` list. tunebox then adds the burst of earlier audio that stream
  sends on connect, which it measures itself; the first answer for a new
  stream comes without it. Leave it out while nothing plays. An index the
  channel does not have is a `400` with `{"error": "bad_stream"}`.
- `current` is `null` during a jingle; show the station name.
- `upcoming` is the next song, announced but not heard yet. `in_s` counts
  down to the moment it becomes current. It is relative, so a wrong device
  clock does not matter. `upcoming.track` is `null` when a jingle comes next.
- `history` is newest first, without jingles and without the current and
  upcoming songs.
- `played_by` is the credit without the "Played by" words, or `null`.
- `started_at` is the song's start in milliseconds since 1970 (UTC).
- `cover` is a Deezer or iTunes image address, or `null` while the lookup
  runs or when nothing matched. `cover_kind` is `album`, `artist` (an artist
  photo) or `null`. Both image hosts allow reading the pixels from another
  site (for colour extraction).
- `stale_since` is when the station's feed stopped answering, in
  milliseconds since 1970, or `null`. The data shown is then the last known.
- Answers carry an `ETag`. Send it back as `If-None-Match` and an unchanged
  answer is a `304` without a body. The countdown is not part of the
  comparison, so on a `304` keep your own deadline.

**`GET /api/pawtunes/<id>`** answers in the format PawTunes' "custom" stats
source reads: `artist`, `title` (with its credit), `artwork` (optional) and
`history` (up to 20 entries with `artist`, `title`, `played_at` in
milliseconds and optional `artwork`).

**`GET /healthz`** is `200` while the server runs, with each station's last
successful check, `stale_since` and `active`. An unreachable station does
not make it fail. `active` is `false` for a station nobody has listened to in
the last ten minutes (and that has no `always = true`); tunebox does not
check its feed then, so an old `last_poll_ok` is expected.

**`GET /api/player`** gives the player's settings from `[player]`:
`latitude`, `longitude` (both `null` when not set) and `default_channel`
(the configured one, else the first station). It may be cached for five
minutes.

Everything else is served from the web folder. Pages are sent with a
Content-Security-Policy that allows scripts, styles and fonts only from
tunebox itself, so pages in that folder must not use inline scripts or
styles. Only known file types are served.
Unknown addresses are a `404` with `{"error": "not_found"}`, unknown
stations `{"error": "unknown_channel"}`.

## Development

You need [uv](https://docs.astral.sh/uv/) and Node.js 20 or later (CI uses
24.22.0). The server uses only the Python standard library and the player
has no build step; the tools below are for development, and no npm packages
are installed.

```sh
uv venv --python 3.14 .venv
uv pip install --python .venv --require-hashes -r requirements-dev.txt
.venv/bin/pre-commit install
```

The commit hook runs ruff, pyright (strict), vulture, `node --check` and the
player's tests; the push hook runs the Python tests with coverage. To run
them by hand:

```sh
.venv/bin/pre-commit run --all-files
.venv/bin/pre-commit run --all-files --hook-stage pre-push
```

```sh
node --test web/tests/*.test.js   # player logic and vendored file hashes
```

The layout check loads the player in headless Chrome at the car's window
sizes, a phone and a desktop, on every page, in day and night, and reports
any control outside the window or covered, overlapping parts, clipped text
and sideways scrolling. It needs Chrome or Chromium and a running tunebox:

```sh
node web/tests/browser/layout.mjs http://127.0.0.1:8080 /tmp/layout-shots
```

(Node 20 needs `--experimental-websocket`; the second argument is optional
and saves a screenshot of every view.)

Tests do not use the network, except those marked `live`
(`.venv/bin/pytest -m live`).

The player is plain ES modules in `web/js/`. Keep to ES2019 syntax (no `?.`,
`??`, class fields): the car's browser engine is old and unknown, and
`web/tests/syntax.test.js` enforces it. Vendored files: see
`web/vendor/README.md`.

Run the server from a checkout:

```sh
mkdir -p data && cp tunebox.example.toml data/tunebox.toml
TUNEBOX_CONFIG=data/tunebox.toml .venv/bin/python -m tunebox
```

Dev tool versions are pinned in `requirements-dev.in` and compiled with
hashes into `requirements-dev.txt`:
`uv pip compile requirements-dev.in --python-version 3.14 --generate-hashes -o requirements-dev.txt`.

## License

GNU Affero General Public License v3.0; see [LICENSE](LICENSE).
