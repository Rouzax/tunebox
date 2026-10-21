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
  title in a narrow window and beside it in a wide one. The station's name
  sits at the top. The title, artists and Played-by credit sit on a card,
  resting on the control bar in a narrow window and beside the cover in a
  wide one. In a narrow window the card keeps one height, so the cover
  does not change size from song to song: each line stays one line, and
  one that does not fit scrolls slowly to its end and back (with reduced
  motion it ends in "..."). Beside the cover each wraps to two lines at
  most and ends in "..." beyond that. A new song slides the card out and
  back in with the new text.
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

- **Play / stop** (middle). Play always starts at what is on air now, not
  where you stopped.
- **Recently played** (clock, bottom left) and **Stations** (radio). On the
  stations page, tap a station to switch; under the playing station you can
  pick its quality, for example "Data saver (AAC 96)" for 4G.
- **Volume and mute.** Narrow windows (the car's third, phones) and phones
  that do not let a web page set the volume (iPhones) show only mute; use
  the device's own volume control there. The radio then plays at full
  volume, whatever the slider was set to in a wider window.
- **Visualizer** (bars icon), **small window** (box with arrow; not on
  touch screens) and **fullscreen**.
- **Day/night** (top left) cycles automatic, day and night. Automatic
  follows the sun when `[player]` has a location, otherwise the device's
  dark mode setting. It turns dark when the evening sun is 4 degrees above
  the horizon (in the Netherlands about 30 to 45 minutes before sunset) and
  light when the morning sun is that high again.

The station, quality, volume, visualizer and day/night choice are
remembered in this browser only; nothing about listeners is stored on the
server.

Good to know:

- Browsers do not start sound until you have tapped the page once. After a
  reload the player starts again if it was playing; if the browser refuses,
  press play.
- "Reconnecting..." means the stream dropped. The player keeps trying with
  pauses of 2 seconds up to a minute; you do not need to do anything.
- When the phone or car takes the sound away (a call, unplugged headphones),
  the player stops and says "Paused by the system. Press play to continue."
  In the car's kind of browser (Chromium on Linux), where a phone connecting
  takes the sound for a moment, the player first starts again by itself, at
  live, within about 20 seconds; it stops only if the sound is taken again
  right after (a call).
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

With the listening PIN locking the whole player, an embed on another site
cannot pass the PIN; see
[Settings and the listening PIN](#settings-and-the-listening-pin).

## Reach it from other devices

The compose file only accepts connections from the machine itself, on
purpose: phones and cars should reach tunebox through your reverse proxy,
which adds HTTPS. The player needs HTTPS outside your own network: browsers
block some features on plain-http pages from elsewhere. In your proxy, forward a hostname to
`http://127.0.0.1:8080` (or the port you chose).

If your proxy runs in Docker on the same network as tunebox, it can use
`http://tunebox:8080` instead.

## Set up the admin panel

The admin panel manages the stations from the browser: add, edit, order,
group, switch off and delete them, and undo any change. It is off until
you turn it on.

Turning it on changes where the configuration comes from. tunebox keeps
its stations, `[defaults]` and `[player]` in its database
(`data/tunebox.db`). With the panel off it takes them from
`data/tunebox.toml` at every start. With the panel on it reads the file
only to fill an empty database (a new install), and the panel is where
stations change from then on.

- On an existing install the database already holds the file's stations,
  so turning the panel on keeps them. Edits made to the file in the same
  restart are not taken; make them before you turn the panel on.
- While the panel is on, tunebox ignores the file's `[[channels]]`,
  `[defaults]` and `[player]`. When they say something other than the
  database, it logs `config_file_ignored` with the parts it ignored. The
  panel cannot change `[defaults]` and `[player]` yet: to change them,
  turn the panel off, edit the file and restart, then turn it on again.
- Turning the panel off makes the file the source again: at the next start
  tunebox replaces the panel's changes with the file's stations. Copy what
  you changed into the file first. The panel's versions stay in its
  History, so turning the panel back on lets you restore them.

**What you need:** an authenticator app on your phone (Google
Authenticator, 1Password, Aegis or similar), and one of these:

- **Only on your home network or VPN:** nothing more. The panel runs over
  plain http at the address you already open tunebox at, such as
  `http://tunebox.home.arpa:8083`.
- **From anywhere:** tunebox reachable over https at a public address,
  with a reverse proxy in front of it.

1. In `data/tunebox.toml`, add, for the home network only:

   ```toml
   [admin]
   enabled = true
   public_origin = "http://tunebox.home.arpa:8083"   # the address you open, with its port
   ```

   or, from anywhere:

   ```toml
   [server]
   trusted_proxies = ["172.18.0.0/16"]   # your proxy's address or network

   [admin]
   enabled = true
   public_origin = "https://radio.example"   # your public address
   ```

   `public_origin` must be exactly the address in the browser's address
   bar: the panel refuses logins from any other, including the same
   machine by its IP address. `trusted_proxies` lets tunebox see the
   visitor's address behind the proxy. Without it every visitor looks like the proxy, so one person's
   wrong passwords would lock everyone out; tunebox then logs
   `untrusted_forwarding`.
2. Restart: `docker compose up -d`.
3. Create the account:

   ```sh
   docker exec -it tunebox python -m tunebox admin setup
   ```

   It asks for a password (at least 12 characters), prints an
   `otpauth://` link and a secret to add to your authenticator app, asks
   for the code the app shows, and prints ten recovery codes. Keep those
   safe: each one replaces a code once if you lose your phone.
4. Open `public_origin` followed by `/admin/`, type the password and press
   Next, then type the code from the app. The code has a page of its own
   so a password manager that holds the authenticator secret can fill it
   in. The page shows the address tunebox sees for you; if that is your
   proxy's address, check `trusted_proxies`.

To replace the account (a lost phone and no recovery codes left), run the
command with `--reset`; that signs out every session.

**Setting it up in the browser instead:** add `setup_page = true` to
`[admin]` and restart. tunebox logs `admin_setup_token` with a one-time
token (the whole path is in the `path` field); open
`https://radio.example/admin/#setup=<token>` within 15 minutes to choose
the password and scan a QR code. A reload keeps the same secret. The page
closes once the account exists. Remove `setup_page` afterwards.

**How it is protected:** the panel only accepts changes from
`public_origin`. Five failed logins from one address lock that
address for 15 minutes. Many wrong codes after a correct password slow
logins down for everyone and show a warning once you are logged in: your
password is then probably known. To have CrowdSec ban repeated attempts at
your proxy, see `examples/crowdsec/README.md`. To switch the panel off, set
`enabled = false` (or remove `[admin]`) and restart: every `/admin`
address then answers 404.

**On plain http** the password, the code and the login cookie cross your
network unencrypted. A VPN such as WireGuard encrypts the trip from your
phone, but anyone who can watch your home network can read them, and any
other website on the same host name (another port included) receives the
login cookie. So tunebox serves the panel only to addresses on a home
network, a VPN or a tailnet (10.x, 172.16-31.x, 192.168.x, 100.64-127.x,
loopback and their IPv6 equivalents), and logs `admin_plain_http` at
start-up. A request a proxy passed on counts only if that proxy is in
`trusted_proxies` and names a client on the home network in
`X-Forwarded-For`. To everyone else the panel looks switched off, and
tunebox logs `admin_not_nearby`.

That check reads addresses, so two things are up to you:

- **Keep tunebox's port closed to the internet.** A tunnel or relay that
  passes connections on without saying who sent them (`ssh -R`, a TCP or
  stream proxy, Docker's own port forwarding for IPv6 visitors) makes an
  internet visitor look local. Do not publish the plain-http panel that
  way; for access from outside, set up https instead.
- **Phones that use a global IPv6 address are refused.** Such an address
  cannot be told apart from the internet. Open the panel by an IPv4
  address or a host name that resolves to one.

**Moving from http to https later:** sessions from plain http stay valid
until they end (at most 12 hours). To end them at once, run the setup
command with `--reset`.

### Manage the stations

**Stations** lists the stations by group. Each change is saved as soon as
you make it; the message at the top says "Saved" or why tunebox refused
it, and players pick up the change at their next update (within about ten
seconds), without a restart.

- The switch takes a station off the player without deleting it. The last
  station that is on cannot be switched off or deleted.
- The arrows move a station within its group, or a group up and down.
- Duplicate makes a copy with the id `<id>-copy`, switched off, to edit.
  It is also how you give a station a new id: the id is fixed once saved,
  because players and PawTunes use it in addresses.
- Deleting or switching off the station the player starts on
  (`default_channel`) clears that setting; the player then starts on the
  first station, and the message says so.
- A group can be deleted once it has no stations.

The edit form has the same settings as a `[[channels]]` block (see
[Change the stations](#change-the-stations)). Empty fields use the
defaults, shown greyed out. Nothing in the form is stored until you press
Save; leaving it with changes (Cancel, the menu, the back button or a
reload) asks first. Title rules are regular expressions and run on
every title, and one slow match stops all of tunebox while it runs. So
tunebox refuses a pattern that could take long on some title:

- longer than 200 characters, or with a back-reference (`\1`);
- a repeated part with another repeat or a choice inside it, such as
  `(a+)+`, `(a|aa)+` or `(?:-?){40}`;
- three or more repeats in a row that can take the same characters, such
  as `.*-.*-.*` or `\s*\s*\s*`. Use a class without the separator
  instead: `[^-]*-[^-]*-.*`;
- a new pattern that is slow on long titles made of its own characters.

The message names the field, for example
`channels[3] (owr).rules.played_by: nested repetition ... is not allowed`.
One save or import may bring at most 100 title rules that are not stored
yet (a pattern used by several stations counts once); more are refused as
a whole, so import a larger file in parts.

**History** lists every change, newest first, with what it changed. "Restore
the version before this change" shows what restoring would change compared
with now, and asks before it does it. A restore is itself a change in the
history, so it can be undone the same way. If someone saved in another tab
or browser in the meantime, a save says "Changed elsewhere": the station
list then shows the current stations, so check them before you make the
change again; an open edit form keeps what you typed, and saving it again
applies it to the current stations. When a save gets no answer at all, the
page says the change may or may not have been saved and shows what is
stored. Saving without changes stores nothing: no History entry, and the
page says "No changes to save." When a change is stored but the running
stations could not take it (a source that cannot be set up), the page says
the change is stored but not live and the stations keep running as they
were; the log has `apply_failed` with the reason. Fix the station and save
again.

**Export and import.** Export, on the History page, downloads the stored
stations as `tunebox-<date>.toml` (the date in UTC): the stations, groups,
station defaults and player settings, in the same form as `tunebox.toml`,
with the version number in its first line. It is a copy to keep or to move
to another tunebox. It does not contain the admin account, its sessions, the
listening PIN, the change history or the covers.

Import takes such a file, or a `tunebox.toml`, and first only shows it:

1. Choose **Import** and pick the file. Nothing is stored yet. The page says
   how many differences there are with what is stored now and lists them.
   `[server]` and `[admin]` tables are left out, with a note: they only
   apply to the file tunebox starts from. Another note names each station
   that needs the listening PIN now but has no `needs_pin = true` in the
   file: after Replace, devices without the PIN see it.
2. **Replace the stations with this file** replaces the stations, groups,
   defaults and player settings as a whole: a station that is not in the
   file is removed. If you have not typed an authenticator code in the
   last five minutes, the page asks for one first.
3. The import is a change in the History ("Imported from a file"), so
   "Restore the version before this change" undoes it.

A file that cannot be used is refused before anything is stored, and the
message says why: broken TOML names the line, and an unknown key, an
invalid station or a refused title rule names the field. The preview does
not time new title rules; Replace does, so a slow rule can still be refused
at that step. A file in which every station is off (or that has no
stations, such as a `tunebox.toml` with only `[server]`) is refused
already in the preview, because the player needs a station that is on.
If the stations changed elsewhere in the meantime, Replace says so and
shows the differences again, against what is stored now.

An export names uploaded logos rather than containing them (see
[Logos](#logos)). Imported on the same tunebox, they come back with it.
Another tunebox that does not have those uploads refuses the file and names
the station, for example
`channels[2] (owr).logo: no uploaded logo blob:3f2a...`. Put a logo link
in that station's `logo` in the file (or remove the line), import, and
upload the logo again there.

### Logos

A station's logo comes from a link or an upload, set in the Logo field of
its edit form:

- **A link** (`https://...`): tunebox downloads the image and serves it
  from its own data folder, so listeners' browsers never contact that
  address. The form shows "Shown after saving." instead of a preview,
  because the panel only shows images tunebox serves itself.
- **Upload**: a PNG, JPEG, WebP or SVG image of at most 2 MB (2,000,000
  bytes). tunebox judges the type by the file's contents, not its name, and
  refuses anything else ("That file is not a PNG, JPEG, WebP or SVG
  image."). After the upload the form shows the logo at full size and at
  the size of the station list, and the Logo field holds the upload's name
  (`blob:` followed by 64 characters). The station uses it after you press
  Save, like any other change in the form.

Uploads are kept in `data/logos/blobs/`, named by their contents, as long
as the stations or any entry in the History name them, so restoring an
older version brings its logo back. An upload that nothing names any more
is removed at a later start, once it is more than a day old. After a
database reset (see [Update, back up and reset](#update-back-up-and-reset))
uploads are kept for at least a day, so an export can be imported back
with its logos.

### Settings and the listening PIN

**Settings** (in the panel's menu) holds what every station inherits unless
it sets its own: the title delay, the PawTunes delay, how often tunebox
checks a station's feed, and how many recently played songs each station
keeps. An empty field uses tunebox's built-in value, shown in grey. Below
that: the station a new device starts on, and the location the player uses
to switch between day and night. Saving works like a station change: it
takes effect at once and shows up in the History, where it can be undone.

The **listening PIN** keeps strangers who find the player's address out of
tunebox. It is off by default. Pick where it asks, type a PIN of 6 to 8
digits, and save:

- **The whole player**: every page of the player shows a keypad until the
  PIN is typed. Nothing of the player, its stations or its titles shows
  before that.
- **Chosen stations**: the player is open, but the stations you marked
  **Needs the PIN** in their editor are left out of its list, titles and
  logos. The station list ends with **More stations with the PIN**, which
  asks for it once and then shows them.

How it behaves:

- A device types the PIN once per browser and stays let in for a year, for
  both modes; moving between the two modes keeps devices let in.
- A device that is playing when the PIN changes, or when the whole player
  gets locked, keeps playing; the keypad appears within ten seconds, and the
  titles come back once the PIN is typed.
- A new PIN, or switching it on from off, asks every device again.
- Over the player, the keypad shows **Stop** while music plays, so the
  driver can silence it before typing. Tab stays inside the keypad.
- When the keypad says "Busy, trying again...", many addresses are guessing
  at once and the try was not checked; the keypad sends it once more after
  2 seconds by itself.
- After 10 wrong PINs from one address, that address waits 15 minutes. When
  wrong PINs pour in from many addresses at once (more than 100 in an hour),
  tunebox checks one try every 2 seconds for everyone together and asks the
  rest to try again. With the CrowdSec files from `examples/crowdsec/`, 20
  wrong PINs ban the address at your proxy (for an hour with the profile in
  their README).
- The admin panel, `/healthz` and `/api/pawtunes` stay open (PawTunes keeps
  working, hidden stations included).
- With the whole player locked, an embed of it on another site stops
  working: browsers do not send the PIN's cookie to a page inside another
  site's frame.
- The PIN keeps tunebox private, not the audio: the streams are the
  stations' own public addresses, and anyone who knows one can play it
  without tunebox.

The PIN is not stored; tunebox keeps a keyed hash of it. PIN changes do not
appear in the History (they cannot be undone there); the log records each
change as `admin_change` with `kind` `pin`, never the PIN itself. Marking a
station **Needs the PIN** is a station change like any other and does appear
in the History.

### Your account

**Account** lists the browsers that are logged in, with their address and
when they were last used; **Sign out** ends one of them. (To end your own,
use Log out.)

The same page changes the password, moves the login codes to a new
authenticator app, and makes new recovery codes. Each of these asks for a
code from your authenticator app first (or a recovery code), unless you
typed one in the last 5 minutes, and each signs every other browser out. New
recovery codes are shown once; the old ones stop working. A new
authenticator is confirmed with a code from the new app; the old app's codes
stop working then.

A wrong code there counts towards the same limits as a wrong code at login
(5 per address in 15 minutes), but it is logged as `admin_reauth_failed`,
which the CrowdSec files do not ban on.

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

With the admin panel on, change the stations there (see
[Manage the stations](#manage-the-stations)); the station settings
below are the same fields as its edit form. Without the panel, edit
`data/tunebox.toml` and restart, as described here.

### Without the admin panel

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

tunebox keeps its stations in its database (`data/tunebox.db`). With the
admin panel off, it compares them with `data/tunebox.toml` at start-up and
takes the file's stations when they differ, so editing the file and
restarting works as described here. A file without `[[channels]]` keeps the
stations tunebox already has. With the panel on, the file only fills an
empty database (see [Set up the admin panel](#set-up-the-admin-panel)).

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

- **Update** to the latest release. Copy `data/tunebox.db` first: a newer
  tunebox may upgrade the database, and an older one then refuses to start
  on it (`settings_unavailable` in the log), so going back means putting
  the copy back. This release asks every browser for the PIN again (when
  one is set); the PIN itself stays valid. Browsers the admin login
  knew lose their shortcut in its pace limit until they next log in; the
  login asks for a code as before.

  ```sh
  cp data/tunebox.db data/tunebox.db.before-update
  docker compose pull && docker compose up -d
  ```

- **Back up**: everything worth keeping is in the `data` folder:
  `tunebox.toml`, and `tunebox.db`, which holds the stations, settings and
  change history (with the admin panel on, the only copy of them), the
  admin account and its sessions, the listening PIN, the covers and the
  recent lists. Copy it while tunebox is stopped, or copy
  `tunebox.db-wal` along with it. `data/logos/blobs/` holds the logos
  uploaded in the panel, the only copy of those images, so back it up with
  the database.
- **Reset** the recent lists and covers: stop tunebox, delete
  `data/tunebox.db`, `data/tunebox.db-wal` and `data/tunebox.db-shm`, and
  start it again. History refills from the stations and covers are looked
  up again. This also deletes everything else the database holds: with the
  admin panel on, the stations start again from `tunebox.toml`, and the
  settings, the history, the admin account (set it up again with
  `python -m tunebox admin setup`) and the listening PIN are gone. To keep
  the stations, export them first and import the file after the reset (see
  [Export and import](#manage-the-stations)).
- **Logos** are kept in `data/logos/`, as downloaded. A PNG logo larger than
  128 px also gets a small copy in `data/logos/small/`, made at start-up
  (`logo_shrunk` in the log), which the station list shows; the original is
  still used as the cover of a station without `default_image`. JPEG, WebP,
  SVG and interlaced PNG logos are used as they are (`logo_not_shrunk` says
  why). The player loads list logos the first time a listener opens the
  station list, once per browser. Delete everything in `data/logos/` except
  `blobs/` to download all logo links again at the next start; `blobs/`
  holds the uploaded logos, which cannot be downloaded again.

## Troubleshooting

| What you see | Likely cause | What to do |
|---|---|---|
| The container stops right away; the log says `tunebox: <something>: <reason>` | A mistake in `data/tunebox.toml`, or the file is missing. | Fix what the message names, then `docker compose up -d`. |
| The container stops right away; the log says `settings_unavailable` | The `data` folder or `data/tunebox.db` is not writable by user id 1000, or `tunebox.db` is damaged or comes from a newer tunebox. | Most often it is the owner: `sudo chown -R 1000:1000 data` and restart. Otherwise restore `tunebox.db` from a back-up, or move it aside (with its `-wal` and `-shm` files): tunebox then starts with a new database and takes the stations from `tunebox.toml`. |
| `untrusted_forwarding` in the log | A request carries `X-Forwarded-For` but its sender is not in `trusted_proxies`: tunebox is behind a proxy it does not know (every visitor then has the proxy's address), or a client sends the header itself. Logged on any path, at most once an hour per sender. | Set `trusted_proxies` in `[server]` to the proxy's address or network and restart. |
| The admin login says "Login failed" with the right password | The code was already used, the phone's clock is off, or a recovery code was typed with a mistake. The login goes back to the password page because tunebox does not say which of the two was wrong. | Press Next with the password still filled in, wait for the next code and check the phone's time. After five failures the address is locked for 15 minutes. |
| The admin page says "Open the admin panel at the address set as public_origin" | It was opened by another address, such as the IP address instead of the host name, or http instead of https. | Open it at `public_origin`, exactly as written there. |
| `/admin/` answers 404 although the panel is on, and the log says `admin_not_nearby` | `public_origin` is plain http and the request did not come from the home network: from the internet, or through a proxy that is not in `trusted_proxies`. | Open the panel directly at `public_origin` from the home network or the VPN. Through a proxy, set up https instead. |
| `config_file_ignored` in the log | The admin panel is on, so tunebox does not read the `parts` it names from `tunebox.toml` (`channels`, `defaults`, `player`); the database has its own, and they differ. | Change stations in the panel. For `defaults` or `player`, see [Set up the admin panel](#set-up-the-admin-panel). To stop the warning, make the file match or remove those parts from it. |
| Saving in the admin panel says "Changed elsewhere" | Someone saved in another tab or browser first. | On the station list, check the stations it now shows and make your change again if it is still needed. In the edit form, check it and save again. |
| The edit form says "tunebox cannot store the image right now" after an upload (`logo_store_failed` in the log) | `data/logos/blobs/` cannot be written: the disk is full or the folder is not owned by user id 1000. | Free disk space or fix the owner (`sudo chown -R 1000:1000 data`), then upload the image again. |
| `logo_missing` in the log; a station shows no logo | The station names an uploaded logo (`blob:...`) whose file is gone from `data/logos/blobs/`, for example after that folder was deleted or the database was restored without it. | Edit the station, upload the logo again and save. Back up `data/logos/blobs/` together with `tunebox.db`. |
| Saving a title rule is refused | The pattern could stall tunebox (see [Manage the stations](#manage-the-stations)). | Simplify it as the message says: no repeat inside a repeated group, such as `(\w+\s?)*`, and no three repeats in a row that take the same characters, such as `.*-.*-.*` (use `[^-]*` for the parts between separators). |
| The admin page says "No admin account yet" | No account was set up. | Run the setup command in [Set up the admin panel](#set-up-the-admin-panel). |
| The log says `tunebox: cannot listen on 0.0.0.0:<port>` | `port` in `[server]` was changed to one that is not free inside the container. | Remove `port` from `[server]` (8080 is the default) and change the published port in `compose.yaml` instead. |
| `docker compose up` complains the port is already allocated | Another program on the machine uses that port. | Choose a free port as in [Install](#install) step 3. |
| `store_unavailable` in the log | tunebox could open its database but the cover store could not, or a later write failed (a full disk, for example). | Check free disk space and that `data` is owned by user id 1000, then restart. Until then tunebox plays, but forgets covers and recent titles on restart. |
| The status page shows no covers for some songs | The song is in neither Deezer nor iTunes (common for unreleased tracks), or the catalogues could not be reached. | Nothing to do for unreleased tracks. A failed lookup is retried after 10 minutes, a song that was not found after 3 days. Look for `search_failed` in the log. |
| A station's name on the status page says "(feed unreachable)" | The station's now-playing feed does not answer. | Nothing to do: tunebox keeps showing the last known songs and keeps trying, at most every 2 minutes. |
| The title changes too early or too late | The delay does not fit this station or player. Stations other than the Tomorrowland channels start at the default and often need their own value. | See [Fine-tune when titles change](#fine-tune-when-titles-change). |
| No sound, and the ring around the stop button keeps spinning ("Reconnecting...") | The stream cannot be reached from this device or network, or the browser cannot play it. | The player keeps trying. Try another quality on the stations page; open `/check.html` on the device to see which formats it plays. To see why, open the player with `?debug=1` added to its address (for example `https://radio.example.com/?debug=1`) and press play: a panel lists what the browser reports about the stream, with its own error text and the player's reconnect decisions. Copy puts the list on the clipboard. The panel stays in that browser; nothing is sent to tunebox. |
| "This browser cannot play AAC" (or MP3) | The browser lacks that codec (common for AAC in some Linux browsers). | Pick a quality with the other codec, usually "Compatible" (MP3). |
| No visualizer | It starts after the first tap or key press on the page. A stream served without the cross-origin permission plays without it ("No visualizer for this stream"). | Tap the page once. Nothing to do for such a stream. |
| A station shows only its name, no song | Its feed answered slowly or not at all when you opened it (tunebox does not show a song it cannot confirm is on now). | Wait for the next update (10 seconds). If it stays empty, `/healthz` asked on the host itself shows `stale_since` for that station (see [HTTP API](#http-api)). |
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
| `trusted_proxies` | none | Addresses or networks of your reverse proxy, such as `["172.18.0.0/16"]`. Only connections from these may name the visitor's address (`X-Forwarded-For`). With a plain-http panel, a connection from these that names no visitor is refused, so keep the range to the proxy and leave out your own devices. |
| `log_file` | none | Also write the log to this file, for a log reader that cannot read the container's output (such as CrowdSec without access to Docker). A relative path is inside `data_dir`, such as `"logs/tunebox.log"`. tunebox starts a new file at 10 MB and keeps five old ones (`tunebox.log.1` to `.5`). If the file cannot be written, tunebox logs `log_file_unavailable` and runs on without it. |

`[admin]` (optional; see [Set up the admin panel](#set-up-the-admin-panel))

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Turns the admin panel on. Off, every `/admin` address answers 404. |
| `public_origin` | none, required when enabled | The address you open the panel at, without a path: `https://radio.example`, or plain http on a home network, such as `http://tunebox.home.arpa:8083`. The panel accepts changes only from there. On plain http it answers only clients on the home network (see [Set up the admin panel](#set-up-the-admin-panel)). |
| `setup_page` | `false` | Opens the one-time setup page while no account exists. |

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
| `latitude`, `longitude` | none | Where day/night "automatic" takes the sun's height from (dark below 4 degrees). Give both or neither. Without them "automatic" follows the browser's dark mode setting. |
| `default_channel` | the first channel | The channel a first-time listener starts on. |

`[[groups]]` (optional)

| Key | Required | Meaning |
|---|---|---|
| `name` | yes | A group heading. Without `[[groups]]` the groups are the ones the stations name, in the order they first appear. With it, list every group a station names, in the order to show them; a group may have no stations yet. An export always lists them. |

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
| `logo` | no | `https://` address of the station logo, or an upload from the admin panel (`blob:` and 64 characters; see [Logos](#logos)). tunebox downloads a link once into the data folder and serves it from there, so listeners' browsers never contact that address. PNG, JPEG, WebP or SVG, up to 2 MB. |
| `enabled` | no, default `true` | `false` keeps the station stored but off the player, as the admin panel's switch does. |
| `always` | no, default `false` | Check the station even while nobody listens. |
| `needs_pin` | no, default `false` | With the listening PIN on "Chosen stations": hide this station from devices that have not typed the PIN. |

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
or when none is configured) and `logo_small` (the same for the small copy
under `/logos/small/`, or `null` when there is none: use `logo` then).
Ungrouped stations come first, then each group in order. The answer carries
the configuration version in an `X-Tunebox-Version` header and is sent with
`Cache-Control: no-cache`.

**`GET /logos/<file>`** and **`GET /logos/small/<file>`** serve a
downloaded logo and its small copy. Browsers may keep them for a
year: the `v` in the path from `/api/channels` changes whenever the logo is
downloaded again, so use that path as given. SVG logos are sent with a
policy that keeps them from running script.

**`GET /api/channels/<id>/now?buffer_s=<seconds>&stream=<name>`** answers what is playing:

```json
{"current": {"artist": "CAMELPHAT, JOSH GIGANTE, KUUDA",
             "artists": ["CAMELPHAT", "JOSH GIGANTE", "KUUDA"],
             "title": "So Good", "played_by": null,
             "started_at": 1792217024107,
             "cover": "https://cdn-images.dzcdn.net/images/cover/.../1000x1000-000000-80-0-0.jpg",
             "cover_kind": "album"},
 "upcoming": {"track": {"...": "..."}, "in_s": 14},
 "history": [{"...": "..."}],
 "stale_since": null,
 "version": 1,
 "stream": "High"}
```

- `buffer_s` (0 to 120, optional) is how far the player has fallen behind
  the moment it asked for the stream: the seconds since it set the audio
  source, minus `audio.currentTime`. Start the clock again on every
  reconnect. tunebox adds it to `delay_s`. An invalid value is a `400`
  with `{"error": "bad_buffer_s"}`. (`audio.buffered` is no measure of
  this: Chrome keeps only a couple of seconds there.)
- `stream` (optional) is the name of the stream playing, from the
  channel's `streams` list; a number is still accepted as its position.
  tunebox then adds the burst of earlier audio that stream sends on
  connect, which it measures itself; the first answer for a new stream
  comes without it. Leave it out while nothing plays. A stream the channel
  does not have is a `400` with `{"error": "bad_stream"}`: fetch
  `/api/channels` again then.
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
- `version` is the configuration version. When it differs from the one
  `/api/channels` gave, fetch that list again: stations or streams changed.
- `stream` is the name of the stream tunebox used, or `null`.
- A `404` with `{"error": "unknown_channel"}` means the station is gone;
  fetch `/api/channels` again.
- Answers carry an `ETag`. Send it back as `If-None-Match` and an unchanged
  answer is a `304` without a body. The countdown is not part of the
  comparison, so on a `304` keep your own deadline. A new configuration
  version changes the ETag.

**`GET /api/pawtunes/<id>`** answers in the format PawTunes' "custom" stats
source reads: `artist`, `title` (with its credit), `artwork` (optional) and
`history` (up to 20 entries with `artist`, `title`, `played_at` in
milliseconds and optional `artwork`).

**`GET /healthz`** is `200` with `{"status": "ok"}` while the server runs.
Asked on the host itself (a connection from loopback without proxy
headers, as Docker's healthcheck makes), it also gives each station's last
successful check, `stale_since` and `active`: run
`docker exec tunebox python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/healthz').read().decode())"`.
An unreachable station does not make it fail. `active` is `false` for a station nobody has listened to in
the last ten minutes (and that has no `always = true`); tunebox does not
check its feed then, so an old `last_poll_ok` is expected.

**`GET /api/player`** gives the player's settings from `[player]`:
`latitude`, `longitude` (both `null` when not set) and `default_channel`
(the configured one, else the first station). It may be cached for five
minutes.

**`POST /api/pin`** with `{"pin": "123456"}` and `Content-Type:
application/json` passes the listening PIN; a post from another site (any
other content type, or a `Sec-Fetch-Site` other than `same-origin`) is a
`403` and counts as nothing, so a stranger's page cannot lock you out. A
right PIN answers `204` and sets the `tunebox_grant` cookie (one year;
`Secure` when your proxy says the visit came over https); a wrong one is a
`401` with `{"error": "pin_wrong"}`; an address that must wait gets `429`
with `{"error": "slow_down"}`; a try refused unchecked while many
addresses are guessing gets `429` with `{"error": "busy"}` and
`Retry-After: 2`. While the PIN is off it answers `204` without a cookie.
While the PIN is on:

- With **the whole player** locked and no grant, every page address
  answers with the PIN page, and `/api/*` (except `/api/pin` and
  `/api/pawtunes`) and `/logos/` answer `401` with
  `{"error": "pin_required"}`.
- With **chosen stations** and no grant, `/api/channels` leaves out the
  stations with `needs_pin` and adds the header `X-Tunebox-Pin: more`;
  their `/now` and logos answer `404`, as for a station that does not
  exist, and `/api/player` gives no `default_channel` that is hidden.
- Answers that depend on the grant are sent with `Cache-Control: private`
  and `Vary: Cookie`, `304` answers included.
- The PIN revision (`<mode>.<generation>`, changed by every mode change and
  every new PIN) is in the `pin` field of `/now` and in the
  `X-Tunebox-Pin-Revision` header of `/api/channels`, so a player notices
  a change.

Everything under `/admin` belongs to the admin panel (when it is on) and
is not meant for other clients: its API needs the panel's session cookie,
and every change also its CSRF token from the same origin.

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

The admin check starts its own tunebox from `.venv` on a free port with a
temporary folder and two stations, creates an account, and drives the
panel in headless Chrome: logging in with a TOTP code, switching a station
off, reordering, editing, a refused title rule, restoring from the history
and logging out, each checked against `/api/channels`. It needs no network:

```sh
node web/tests/browser/admin.mjs /tmp/admin-shots
```

The car's browser is Chromium 120, and the Chrome on a development machine
is much newer: it accepts CSS and web APIs the car does not have. To run
either check on Chromium 120, fetch Chrome for Testing 120 once (about
150 MB, checked against a pinned SHA-256 and kept in `~/.cache/tunebox`)
and pass it as `CHROME`:

```sh
CHROME=$(.venv/bin/python scripts/chrome_120.py) CHROME_NO_SANDBOX=1 \
  node web/tests/browser/layout.mjs http://127.0.0.1:8080
```

`CHROME_NO_SANDBOX=1` runs it without Chrome's sandbox, which an unpacked
Chrome cannot use where the system blocks it (Ubuntu 24.04 does). The
checks only load tunebox's own pages.

Tests do not use the network, except those marked `live`
(`.venv/bin/pytest -m live`).

The player is plain ES modules in `web/js/`. Keep to ES2019 syntax (no `?.`,
`??`, class fields): the car runs Chromium 120, and
`web/tests/syntax.test.js` enforces it. CSS and web APIs newer than
Chromium 120 need a feature check and a fallback. Vendored files: see
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
