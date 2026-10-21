# CrowdSec for the tunebox admin panel

tunebox logs every refused admin attempt with the client's address. These
files let CrowdSec turn repeated attempts into a ban at your reverse proxy's
bouncer. They complement CrowdSec's stock HTTP scenarios on the proxy's own
log, and count only tunebox events, so the two do not add up on the same
few typos.

## What you need

- CrowdSec able to read tunebox's log, and a bouncer in front of tunebox,
  at the reverse proxy. Either CrowdSec reads Docker's logs (the `docker`
  data source, `acquis.yaml`), or tunebox writes its log to a file as well
  (`log_file` in `[server]`) and CrowdSec reads that file
  (`acquis-file.yaml`). The file needs no access to Docker, which a
  CrowdSec that faces the internet should not have.
- `trusted_proxies` set in `tunebox.toml` to your proxy's address, so the
  logged `client` is the visitor and not the proxy. tunebox logs
  `untrusted_forwarding` (any path) when a forwarding header arrives from a peer outside it.

## Install

1. Copy `acquis.yaml` to `/etc/crowdsec/acquis.d/tunebox.yaml` and set
   `container_name` to your container's name. With the log file instead,
   set `log_file = "logs/tunebox.log"` in `tunebox.toml`, mount that
   folder read-only into CrowdSec, copy `acquis-file.yaml` as
   `tunebox.yaml` and set its path to where CrowdSec sees the file.
2. Copy `parsers/s01-parse/tunebox-logs.yaml` to
   `/etc/crowdsec/parsers/s01-parse/`.
3. Copy the files in `scenarios/` to `/etc/crowdsec/scenarios/`.
4. Reload CrowdSec: `sudo systemctl reload crowdsec`.
5. Check with the sample lines:

   ```sh
   sudo cscli explain --file sample.log --type tunebox
   ```

   Each line should come out parsed, with `log_type` `tunebox_<event>`
   and a `source_ip`; the lines that count toward a ban also have a
   `client_range`.

## What gets banned

| Scenario | Trigger |
|---|---|
| `tunebox/admin-bf` | 5 failed logins, wrong setup tokens or refusals for an address that is locked, from one address within about 10 minutes |
| `tunebox/admin-totp-bf` | 3 wrong codes (from the app or recovery codes) after a correct password within about 10 minutes |
| `tunebox/listener-pin-bf` | Twenty wrong listening PINs (or tries while locked out) from one address at once, or a steady stream faster than one every 30 seconds (a leaky bucket of 20 that drains one every 30 s) |

Wrong codes typed to confirm a change in the panel (`admin_reauth_failed`,
and `admin_rate_limited` with `why` `reauth` once the address is locked)
are logged but not banned on: they come from someone already logged in,
most likely you. PIN tries refused while tunebox paces a flood from many
addresses (`listener_pin_paced`) are not counted either: they are someone
else's flood, not this address's guess. PIN posts that another site's
page made a visitor's browser send (`listener_pin_cross_site`) are refused
and not counted, so such a page cannot get your own address banned.

Refused cross-site requests (`admin_csrf_rejected`) are logged but never
counted: any web page can make a visitor's browser send one, so counting
them would let a stranger's page get your own address banned.

The scenarios count and ban per range: the address itself for IPv4, its
/64 for IPv6, the same unit tunebox counts in. tunebox writes the range as
`range` on every event the scenarios count, and the parser keeps it as
`client_range`, so a visitor who rotates addresses inside one /64 fills one
bucket and is banned as a whole. A locked-out address writes at most one
WARNING a second per address group (the rest are at DEBUG), so the log
stays quiet however fast it retries.

These examples have not been run against a live CrowdSec. Check them on the
host with `cscli explain` (step 5) before they replace a working setup.

The ban length comes from your CrowdSec profiles: 4 hours with the default
profile. To ban the second scenario for 24 hours, add this profile before
the default one in `/etc/crowdsec/profiles.yaml`:

```yaml
name: tunebox_totp
filters:
  - Alert.Remediation == true && Alert.GetScenario() == "tunebox/admin-totp-bf"
decisions:
  - type: ban
    duration: 24h
on_success: break
---
```

A listening PIN is a door against strangers, not an account, so its ban
can be shorter; for one hour, add:

```yaml
name: tunebox_pin
filters:
  - Alert.Remediation == true && Alert.GetScenario() == "tunebox/listener-pin-bf"
decisions:
  - type: ban
    duration: 1h
on_success: break
---
```

Without these profiles, every scenario bans for the default length.
