# Vinted + Mercari Monitor

A small polling bot that watches one or more Vinted and/or Mercari Japan
searches and logs/alerts you (console + log file + Discord) when a new
listing shows up.

## Heads-up before using this

Neither site publishes a public API. This script talks to the same internal
endpoints their websites call in your browser:

- **Vinted** — `/api/v2/catalog/items` (needs a session cookie, grabbed by
  hitting the homepage once).
- **Mercari JP** — `api.mercari.jp/v2/entities:search` (needs a per-request
  DPoP proof JWT, generated locally from a throwaway EC key). Mercari also
  geo-restricts hard — from outside Japan you may get errors or empty
  results.

Both are unofficial, undocumented, and can change or start blocking
automated requests without notice. Using them may be against each site's
Terms of Service. Keep it to personal, low-frequency use (this defaults to
checking every ~3 minutes), don't hammer it, and expect to have to patch
things when a site changes.

## Setup

```bash
pip install requests cryptography
```

`cryptography` is only needed for Mercari searches.

## Configure your searches

Open the script and edit the `SEARCHES` list. Each entry is one saved
search:

| key        | required | notes |
|------------|----------|-------|
| `name`     | yes      | unique; used for logging and de-dup state |
| `url`      | yes      | a search URL copied straight from the site |
| `provider` | no       | `"vinted"` (default) or `"mercari"` |
| `webhook`  | no       | per-search Discord channel; falls back to `DISCORD_WEBHOOK_URL` |

The easiest way to build the `url`: go to Vinted or Mercari, set up the
filters you want in the site's own UI (keyword, brand, size, condition,
price range, whatever), then copy the full URL from the address bar. The
bot reads whatever filters are baked into that link — including Mercari's
`<facetUUID>=<valueUUID>` sidebar filters — so you never have to know
either site's internal filter IDs.

If you're on a different Vinted domain (vinted.fr, vinted.de, etc.), change
`DOMAIN` at the top of the file. Mercari is always `jp.mercari.com`.

On the **first** poll for a newly added search the bot just records what's
currently listed (no alert storm); it alerts on genuinely new listings from
then on. Set `PRIME_ON_FIRST_RUN = False` to alert on everything instead.

## Run it

```bash
python vinted_monitor.py
```

It will:
- Print new matches to the console
- Also write them to `vinted_monitor.log`
- Track what it's already alerted on in `vinted_seen.json`, so restarting
  the script won't re-notify you about the same listings

## Hooking up Discord notifications

1. In your Discord server: **Server Settings → Integrations → Webhooks → New Webhook**.
2. Pick the channel you want alerts in, then **Copy Webhook URL**.
3. Paste it into `DISCORD_WEBHOOK_URL` near the top of `vinted_monitor.py`:

   ```python
   DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/xxxx/xxxx"
   ```

That's it — new matches will post as an embed (title, price, brand, size,
thumbnail photo, and a link straight to the listing) in that channel. Leave
it as `None` to keep it console/log-only.

If you'd rather use Slack or Telegram instead/as well, the `notify()`
function is the place to add another branch — a couple of quick examples:

**Slack webhook:**
```python
requests.post(SLACK_WEBHOOK_URL, json={"text": f"🆕 {title} - {amount}{currency}\n{url}"})
```

**Telegram bot:**
```python
requests.post(
    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
    json={"chat_id": CHAT_ID, "text": f"{title} - {amount}{currency}\n{url}"}
)
```

## Running it continuously

For actual "always on" monitoring you'll want this running somewhere other
than your own laptop terminal:

- **Simplest**: a small always-on machine (Raspberry Pi, old laptop, cheap
  VPS) running the script inside `tmux`/`screen`, or as a `systemd` service
  / cron-restarted process.
- **Docker**: wrap it in a container with a restart policy so it survives
  crashes and reboots.

A minimal `systemd` unit, for example:

```ini
[Unit]
Description=Vinted Monitor

[Service]
ExecStart=/usr/bin/python3 /path/to/vinted_monitor.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```
