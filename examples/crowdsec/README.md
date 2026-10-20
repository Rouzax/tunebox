# CrowdSec for the tunebox admin panel

tunebox logs every refused admin attempt with the client's address. These
files let CrowdSec turn repeated attempts into a ban at your reverse proxy's
bouncer. They complement CrowdSec's stock HTTP scenarios on the proxy's own
log, and count only tunebox events, so the two do not add up on the same
few typos.

## What you need

- CrowdSec reading Docker logs (the `docker` data source) on the host that
  runs tunebox, and a bouncer in front of tunebox, at the reverse proxy.
- `trusted_proxies` set in `tunebox.toml` to your proxy's address, so the
  logged `client` is the visitor and not the proxy. tunebox logs
  `trusted_proxies_unset` when a proxy header arrives without it.

## Install

1. Copy `acquis.yaml` to `/etc/crowdsec/acquis.d/tunebox.yaml` and set
   `container_name` to your container's name.
2. Copy `parsers/s01-parse/tunebox-logs.yaml` to
   `/etc/crowdsec/parsers/s01-parse/`.
3. Copy the files in `scenarios/` to `/etc/crowdsec/scenarios/`.
4. Reload CrowdSec: `sudo systemctl reload crowdsec`.
5. Check with the sample lines:

   ```sh
   sudo cscli explain --file sample.log --type tunebox
   ```

   Each line should come out parsed, with `log_type` `tunebox_<event>`
   and a `source_ip`.

## What gets banned

| Scenario | Trigger |
|---|---|
| `tunebox/admin-bf` | 5 failed logins, wrong setup tokens or refusals for an address that is locked, from one address within about 10 minutes |
| `tunebox/admin-totp-bf` | 3 wrong codes (from the app or recovery codes) after a correct password within about 10 minutes |
| `tunebox/listener-pin-bf` | 20 wrong listening PINs, or tries while locked out, from one address within about 10 minutes |

Wrong codes typed to confirm a change in the panel (`admin_reauth_failed`)
are logged but not banned on: they come from someone already logged in,
most likely you. PIN tries refused while tunebox paces a flood from many
addresses (`listener_pin_paced`) are not counted either: they are someone
else's flood, not this address's guess.

Refused cross-site requests (`admin_csrf_rejected`) are logged but never
counted: any web page can make a visitor's browser send one, so counting
them would let a stranger's page get your own address banned.

The scenarios count per address. tunebox itself counts IPv6 visitors per
/64, so a visitor who rotates addresses inside one /64 is still locked by
tunebox after five failures, but does not fill a CrowdSec bucket.

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
