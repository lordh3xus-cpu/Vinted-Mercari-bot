#!/usr/bin/env python3
"""
Vinted + Mercari Monitor
========================
Polls one or more saved searches on Vinted and/or Mercari Japan and alerts
you (console + local log file + Discord) whenever a new listing shows up.

Neither site offers a public API, so this uses the same internal endpoints
their websites call:
  - Vinted:  api/v2/catalog/items  (needs a session cookie, grabbed by
             hitting the homepage once)
  - Mercari: api.mercari.jp/v2/entities:search  (needs a per-request DPoP
             proof JWT, generated locally from a throwaway EC key)

Both are unofficial and undocumented -- they can change or start blocking
automated traffic at any time, and heavy polling can get your IP
rate-limited or banned. Keep the interval reasonable (this defaults to
every 3 minutes with jitter) and only run this for your own personal use.
Mercari also geo-restricts hard; from outside Japan you may get errors or
empty results.

Setup
-----
    pip install requests cryptography

`cryptography` is only needed if you monitor Mercari searches.

Usage
-----
    python vinted_monitor.py

Edit the SEARCHES list below to define what you want to track.
"""

import base64
import json
import os
import random
import re
import time
import logging
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse, parse_qs

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    _HAVE_CRYPTO = True
except ImportError:  # only required for Mercari searches
    _HAVE_CRYPTO = False

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Which Vinted domain to use (vinted.com, vinted.fr, vinted.de, vinted.co.uk,
# vinted.pt, vinted.it, vinted.es, vinted.pl, vinted.lt, vinted.cz, etc.)
DOMAIN = "www.vinted.pt"

# Each entry is one saved search. The easiest way to build one: go to the
# site (Vinted or Mercari), set up the filters you want in its own UI --
# keyword, brand, size, condition, price range, whatever -- then copy the
# full URL from your browser's address bar and paste it as `url`. The bot
# reads whatever filters are baked into that link, so you never have to know
# either site's internal filter IDs.
#
# Keys per entry:
#   "name"     - required, unique. Used for logging and de-dup state.
#   "url"      - required. The copied search URL.
#   "provider" - "vinted" (default) or "mercari".
#   "webhook"  - optional. Send this search's alerts to its own Discord
#                channel. Without it, alerts fall back to DISCORD_WEBHOOK_URL.
#
# The "webhook" values below are intentionally blank so this file is safe to
# push to a public repo. Supply the real list at runtime via EITHER:
#   - a `searches.json` file next to this script (keep it git-ignored), or
#   - a SEARCHES_JSON environment variable holding the same JSON
#     (this is what the GitHub Action uses -- stored as a repo secret).
# Whichever is present wins; otherwise the blank-webhook defaults are used.
_DEFAULT_SEARCHES = [
    {
        "name": "Golden Goose (tamanhos selecionados)",
        "url": "https://www.vinted.pt/catalog?search_text=golden%20goose&size_ids[]=780&size_ids[]=781&size_ids[]=783&size_ids[]=782&size_ids[]=784&size_ids[]=785&size_ids[]=786&size_ids[]=787&size_ids[]=788&size_ids[]=789&size_ids[]=790",
        "webhook": "",
    },
    {
        "name": "Zegna (tamanhos selecionados)",
        "url": "https://www.vinted.pt/catalog?search_text=zegna&size_ids[]=780&size_ids[]=781&size_ids[]=783&size_ids[]=782&size_ids[]=784&size_ids[]=785&size_ids[]=786&size_ids[]=787&size_ids[]=788&size_ids[]=789&size_ids[]=790",
        "webhook": "",
    },
    {
        "name": "Omega x Swatch",
        "url": "https://www.vinted.pt/catalog?search_text=omega%20x%20swatch",
        "webhook": "",
    },
    {
        "name": "Golden Goose (Mercari JP)",
        "provider": "mercari",
        "url": "https://jp.mercari.com/en/search?keyword=golden%20goose&f42ae390-04ff-46ea-808b-f5d97cb45db4=b960227d-d0b4-4234-9585-7f1ae6650102&sort=created_time&order=desc&status=on_sale",
        "webhook": "",
    },
    {
        "name": "Maison Margiela (Mercari JP)",
        "provider": "mercari",
        "url": "https://jp.mercari.com/en/search?keyword=maison%20margiela&f42ae390-04ff-46ea-808b-f5d97cb45db4=b960227d-d0b4-4234-9585-7f1ae6650102&sort=created_time&order=desc&status=on_sale",
        "webhook": "",
    },
    {
        "name": "Loro Piana (Mercari JP)",
        "provider": "mercari",
        "url": "https://jp.mercari.com/en/search?keyword=loro%20piana&f42ae390-04ff-46ea-808b-f5d97cb45db4=b960227d-d0b4-4234-9585-7f1ae6650102&sort=created_time&order=desc&status=on_sale",
        "webhook": "",
    },
    {
        "name": "Zegna (Mercari JP)",
        "provider": "mercari",
        "url": "https://jp.mercari.com/en/search?keyword=zegna&f42ae390-04ff-46ea-808b-f5d97cb45db4=b960227d-d0b4-4234-9585-7f1ae6650102&sort=created_time&order=desc&status=on_sale",
        "webhook": "",
    },
]


def _load_searches() -> list:
    raw = os.environ.get("SEARCHES_JSON", "").strip()
    src = "SEARCHES_JSON"
    if not raw:
        local = Path(__file__).parent / "searches.json"
        if local.exists():
            raw, src = local.read_text(encoding="utf-8").strip(), "searches.json"
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            sys.stderr.write(f"{src} não é JSON válido ({e}); a usar os padrões do ficheiro.\n")
        else:
            if isinstance(data, list) and data:
                return data
            sys.stderr.write(f"{src} tem de ser uma lista JSON não-vazia; a usar os padrões.\n")
    return _DEFAULT_SEARCHES


SEARCHES = _load_searches()

# When truthy (the GitHub Action sets RUN_ONCE=1), do a single polling pass
# and exit instead of looping forever -- so the whole bot is one cron job.
RUN_ONCE = os.environ.get("RUN_ONCE", "").strip().lower() not in ("", "0", "false", "no")

POLL_INTERVAL_SECONDS = 180      # base interval between polling rounds
JITTER_SECONDS = 45              # random +/- added to avoid a robotic fixed cadence
REQUEST_TIMEOUT = 15
STATE_FILE = Path(__file__).parent / "vinted_seen.json"
LOG_FILE = Path(__file__).parent / "vinted_monitor.log"

# Discord webhook URL (Server Settings -> Integrations -> Webhooks -> New
# Webhook -> Copy URL). Leave as None to disable Discord notifications and
# just log to console/file.
DISCORD_WEBHOOK_URL: Optional[str] = None

# How many of the listing's photos to include in the Discord alert. Discord
# renders 4 images per message (embeds that share one URL get merged into a
# grid), so anything above 4 is split across follow-up messages on the same
# webhook. Vinted's search response already carries the full gallery; for
# Mercari the bot makes one detail request per new regular item anyway (for
# the seller name), and reuses it for the photo gallery.
MAX_PHOTOS = 3

# The first time a search is seen (no de-dup state for it yet), just record
# whatever is currently listed instead of firing an alert for every one of
# them. Stops a burst of dozens of alerts when you add a new search.
PRIME_ON_FIRST_RUN = True

# Safety valve: if a single poll turns up more than this many new items for
# one search (e.g. the bot was offline for hours), alert only the newest N
# and silently record the rest.
MAX_ALERTS_PER_CYCLE = 15

# Politeness pause between per-item Mercari calls (detail fetch + Discord).
MERCARI_ITEM_DELAY = 1.0

# How many recently-seen item IDs to remember per search. Once exceeded, the
# oldest are forgotten first. Keep it well above a single page of results so
# a busy search can't "wrap around" and re-alert things.
SEEN_CAP = 1000


def _build_session(extra_headers: Optional[dict] = None) -> requests.Session:
    """A requests session that retries transient failures (dropped
    connections, timeouts, 429/5xx) a couple of times with a short backoff,
    instead of losing a whole poll cycle to one blip."""
    session = requests.Session()
    session.headers["User-Agent"] = _UA
    if extra_headers:
        session.headers.update(extra_headers)
    retry = Retry(
        total=3, connect=3, read=3, backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=None,      # retry POST too (all our calls are safe to repeat)
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def params_from_search_url(search_url: str) -> dict:
    """Take a URL copied straight from Vinted's search page (with whatever
    brand/size/condition/price filters you picked on the site) and turn its
    query string into params for the catalog API."""
    query = urlparse(search_url).query
    parsed = parse_qs(query, keep_blank_values=False)
    params = {}
    for key, values in parsed.items():
        # requests will repeat a key for every item in a list value, which
        # matches how Vinted expects repeated keys like size_ids[]=1&size_ids[]=2
        params[key] = values if len(values) > 1 else values[0]
    params.setdefault("order", "newest_first")
    return params


# Mercari web search URL params -> API searchCondition enum values.
_MERCARI_SORT = {
    "created_time": "SORT_CREATED_TIME",
    "price": "SORT_PRICE",
    "num_likes": "SORT_NUM_LIKES",
    "score": "SORT_SCORE",
    "size": "SORT_SIZE",
}
_MERCARI_ORDER = {"asc": "ORDER_ASC", "desc": "ORDER_DESC"}
_MERCARI_STATUS = {
    "on_sale": "STATUS_ON_SALE",
    "sold_out": "STATUS_SOLD_OUT",
    "trading": "STATUS_TRADING",
}
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)


def mercari_condition_from_url(search_url: str) -> dict:
    """Turn a jp.mercari.com/search URL into the API's `searchCondition`
    object. Mercari's sidebar filters land in the URL as `<facetUUID>=
    <valueUUID>` query params; those are passed straight through as dynamic
    attribute conditions, so any filter you set on the site is honoured
    without needing to know its meaning."""
    raw = parse_qs(urlparse(search_url).query, keep_blank_values=False)
    # normalise `foo[]` -> `foo`
    q: dict[str, list] = {}
    for key, values in raw.items():
        q.setdefault(key[:-2] if key.endswith("[]") else key, []).extend(values)

    def first(key, default=None):
        return q[key][0] if q.get(key) else default

    cond = {
        "keyword": first("keyword", ""),
        "excludeKeyword": first("exclude_keyword", ""),
        "sort": _MERCARI_SORT.get(first("sort", "created_time"), "SORT_CREATED_TIME"),
        "order": _MERCARI_ORDER.get(first("order", "desc"), "ORDER_DESC"),
        "status": [_MERCARI_STATUS[s] for s in q.get("status", ["on_sale"])
                   if s in _MERCARI_STATUS] or ["STATUS_ON_SALE"],
        "sizeId": q.get("size_id", []),
        "categoryId": q.get("category_id", []),
        "brandId": q.get("brand_id", []),
        "sellerId": [],
        "priceMin": int(first("price_min", 0) or 0),
        "priceMax": int(first("price_max", 0) or 0),
        "itemConditionId": q.get("item_condition_id", []),
        "shippingPayerId": q.get("shipping_payer_id", []),
        "shippingFromArea": q.get("shipping_from_area", []),
        "shippingMethod": [],
        "colorId": q.get("color_id", []),
        "hasCoupon": False,
        "attributes": [],
        "itemTypes": [],
        "skuIds": [],
        "shopIds": [],
    }
    attrs: dict[str, list] = {}
    for key, values in q.items():
        if _UUID_RE.match(key):
            attrs.setdefault(key, []).extend(values)
    cond["attributes"] = [{"id": k, "values": v} for k, v in attrs.items()]
    return cond


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

# Mercari titles are Japanese; make sure a legacy-codepage Windows console
# (cp1252 etc.) doesn't blow up when logging them.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("vinted_monitor")


def _json_items(resp, source: str) -> list[dict]:
    """Pull the item list out of a search response, tolerating the case
    where the site answers HTTP 200 with an HTML page (a bot check or a
    maintenance page) instead of JSON. That just means "no results this
    round" -- log it and move on, don't let it crash the whole bot."""
    try:
        data = resp.json()
    except ValueError:
        log.warning(
            "%s respondeu algo que não é JSON (%s) -- a ignorar esta ronda.",
            source, resp.headers.get("Content-Type", "?"),
        )
        return []
    return data.get("items", []) if isinstance(data, dict) else []


# --------------------------------------------------------------------------
# Vinted client
# --------------------------------------------------------------------------

@dataclass
class VintedClient:
    domain: str
    session: requests.Session = field(
        default_factory=lambda: _build_session({"Accept": "application/json, text/plain, */*"})
    )

    def __post_init__(self):
        self._prime_session()

    def _prime_session(self):
        """Vinted requires a valid session/CSRF cookie before the API will
        answer; hitting the homepage once picks that up automatically."""
        resp = self.session.get(f"https://{self.domain}/", timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()

    def search(self, params: dict, per_page: int = 20) -> list[dict]:
        url = f"https://{self.domain}/api/v2/catalog/items"
        query = {"per_page": per_page, **params}
        resp = self.session.get(url, params=query, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 401 or resp.status_code == 403:
            # session/CSRF likely expired -- refresh and retry once
            self._prime_session()
            resp = self.session.get(url, params=query, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return _json_items(resp, "Vinted")


# --------------------------------------------------------------------------
# Mercari Japan client
# --------------------------------------------------------------------------

MERCARI_SEARCH_URL = "https://api.mercari.jp/v2/entities:search"
MERCARI_ITEM_URL = "https://api.mercari.jp/items/get"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@dataclass
class MercariClient:
    session: requests.Session = field(default_factory=lambda: _build_session({
        "Accept": "*/*",
        "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.8",
        "X-Platform": "web",
        "Origin": "https://jp.mercari.com",
        "Referer": "https://jp.mercari.com/",
    }))

    def __post_init__(self):
        if not _HAVE_CRYPTO:
            raise RuntimeError(
                "Mercari searches need the 'cryptography' package "
                "(pip install cryptography)."
            )
        # Throwaway EC P-256 key, reused for every DPoP proof this process
        # signs. Mercari's anonymous search doesn't bind it to anything, it
        # just wants a valid signature over the request.
        self._key = ec.generate_private_key(ec.SECP256R1())
        nums = self._key.public_key().public_numbers()
        self._jwk = {
            "kty": "EC", "crv": "P-256",
            "x": _b64url(nums.x.to_bytes(32, "big")),
            "y": _b64url(nums.y.to_bytes(32, "big")),
        }

    def _dpop(self, method: str, url: str) -> str:
        header = {"typ": "dpop+jwt", "alg": "ES256", "jwk": self._jwk}
        payload = {
            "iat": int(time.time()),
            "jti": str(uuid.uuid4()),
            "htu": url,
            "htm": method,
            "uuid": str(uuid.uuid4()),
        }
        signing_input = (
            _b64url(json.dumps(header, separators=(",", ":")).encode())
            + "."
            + _b64url(json.dumps(payload, separators=(",", ":")).encode())
        )
        der = self._key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        sig = _b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
        return signing_input + "." + sig

    def search(self, condition: dict, page_size: int = 60) -> list[dict]:
        body = {
            "userId": "",
            "pageSize": page_size,
            "pageToken": "",
            "searchSessionId": str(uuid.uuid4()),
            "indexRouting": "INDEX_ROUTING_UNSPECIFIED",
            "thumbnailTypes": [],
            "searchCondition": condition,
            "defaultDatasets": [],
            "serviceFrom": "suruga",
            "withItemBrand": True,
            "withItemSize": True,
            "withItemPromotions": False,
            "withItemSizes": False,
            "useDynamicAttribute": True,
            "withSuggestedItems": False,
            "withOfferPricePromotion": False,
            "withProductSuggest": False,
            "withParentProducts": False,
            "withProductArticles": False,
            "withSearchProductByShop": False,
        }
        headers = {
            "DPoP": self._dpop("POST", MERCARI_SEARCH_URL),
            "Content-Type": "application/json",
        }
        resp = self.session.post(
            MERCARI_SEARCH_URL, headers=headers, json=body, timeout=REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        return _json_items(resp, "Mercari")

    def item_detail(self, item_id: str) -> dict:
        """Mercari search results carry only the main photo and no seller
        name. This hits the single-item endpoint for the full gallery
        (original-size URLs) and the seller's display name. Only works for
        regular `m...` items, not Mercari Shops products. Returns {} on any
        failure so the caller can fall back to search data."""
        url = f"{MERCARI_ITEM_URL}?id={item_id}"
        try:
            resp = self.session.get(
                url, headers={"DPoP": self._dpop("GET", url)}, timeout=REQUEST_TIMEOUT
            )
            if not resp.ok or "json" not in resp.headers.get("Content-Type", ""):
                log.warning(
                    "mercari item_detail(%s): HTTP %s -- using search data only",
                    item_id, resp.status_code,
                )
                return {}
            try:
                data = resp.json().get("data", {}) or {}
            except ValueError:
                log.warning(
                    "mercari item_detail(%s): resposta não-JSON -- a usar só os "
                    "dados da busca.", item_id,
                )
                return {}
            return {
                "photos": [p for p in (data.get("photos") or []) if isinstance(p, str)],
                "seller": (data.get("seller") or {}).get("name", ""),
            }
        except requests.RequestException as e:
            log.warning("mercari item_detail(%s) failed: %s", item_id, e)
            return {}


# --------------------------------------------------------------------------
# Seen-item tracking (so we only alert on genuinely new listings)
# --------------------------------------------------------------------------

def load_seen() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("State file was corrupted, starting fresh.")
    return {}


def save_seen(seen: dict):
    # Write to a temp file then atomically swap it in, so a crash mid-write
    # can't leave a truncated state file (which would re-alert everything).
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


# --------------------------------------------------------------------------
# Per-provider: raw API item -> common listing shape
# --------------------------------------------------------------------------

def vinted_listing(item: dict, client) -> dict:
    price = item.get("price", {})
    photo_urls = [p.get("url") for p in (item.get("photos") or []) if p.get("url")]
    if not photo_urls:
        main_photo = (item.get("photo") or {}).get("url")
        if main_photo:
            photo_urls = [main_photo]
    user = item.get("user") or {}
    seller_url = user.get("profile_url") or (
        f"https://{DOMAIN}/member/{user['id']}" if user.get("id") else ""
    )
    return {
        "id": str(item.get("id")),
        "title": item.get("title") or "(sem título)",
        "amount": price.get("amount") if isinstance(price, dict) else item.get("price"),
        "currency": price.get("currency_code", "") if isinstance(price, dict) else "",
        "brand": item.get("brand_title", ""),
        "size": item.get("size_title", ""),
        "seller": user.get("login", ""),
        "seller_url": seller_url,
        "listed_at": None,  # Vinted's catalog payload has no reliable listing time
        "url": item.get("url") or f"https://{DOMAIN}/items/{item.get('id')}",
        "photo_urls": photo_urls,
    }


def mercari_listing(item: dict, client: "MercariClient") -> dict:
    item_id = str(item.get("id"))
    is_shop = not item_id.startswith("m")  # Mercari Shops / retail products
    if is_shop:
        url = f"https://jp.mercari.com/shops/product/{item_id}"
    else:
        url = f"https://jp.mercari.com/item/{item_id}"

    # Regular items: one detail call gets the full gallery + seller name.
    # Shops products: neither is available, fall back to the search payload.
    detail = client.item_detail(item_id) if not is_shop else {}
    photo_urls = detail.get("photos") or [
        p.get("uri") for p in (item.get("photos") or []) if p.get("uri")
    ]
    seller = detail.get("seller") or item.get("shopName") or ""
    seller_id = item.get("sellerId")
    seller_url = (
        f"https://jp.mercari.com/user/profile/{seller_id}"
        if seller_id and not is_shop else ""
    )
    listed_at = None
    if item.get("created"):
        try:
            listed_at = datetime.fromtimestamp(
                int(item["created"]), tz=timezone.utc
            ).isoformat()
        except (TypeError, ValueError, OSError):
            pass

    return {
        "id": item_id,
        "title": item.get("name") or "(sem título)",
        "amount": item.get("price"),
        "currency": "JPY",
        "brand": (item.get("itemBrand") or {}).get("name", ""),
        "size": (item.get("itemSize") or {}).get("name", ""),
        "seller": seller,
        "seller_url": seller_url,
        "listed_at": listed_at,
        "url": url,
        "photo_urls": photo_urls,
    }


_LISTING_BUILDERS = {"vinted": vinted_listing, "mercari": mercari_listing}


# --------------------------------------------------------------------------
# Alerting -- swap this out for a webhook, Telegram bot, email, etc.
# --------------------------------------------------------------------------

def notify(search: dict, listing: dict):
    provider = search.get("provider", "vinted")
    name = search["name"]
    source = "Mercari JP" if provider == "mercari" else "Vinted"

    log.info(
        "NOVO [%s] %s | %s %s | %s | %s | %s",
        name, listing["title"], listing["amount"], listing["currency"],
        listing["brand"], listing["size"], listing["url"],
    )

    target_webhook = search.get("webhook") or DISCORD_WEBHOOK_URL
    if not target_webhook:
        return

    send_discord_alert(target_webhook, listing, search_name=name, source=source)


# Discord only renders 4 images per message, and only when several embeds
# share the same `url` (it merges them into a single grid). So each outgoing
# message carries at most this many photos; the rest follow in extra
# messages on the same webhook.
IMAGES_PER_MESSAGE = 4

# Label for the link button under each alert (jumps straight to the listing).
LINK_BUTTON_LABEL = "Abrir produto original"

# Accent colour of the embed, per source, so channels are recognisable at a
# glance (Vinted teal / Mercari red).
SOURCE_COLOR = {"Vinted": 0x09B1BA, "Mercari JP": 0xFF0211}
_DEFAULT_COLOR = 0xC9A24A


def _price_field(amount, currency):
    """(field name, field value) for the price. Yen gets a ¥ + thousands
    separator and no decimals; everything else keeps its native currency."""
    if currency == "JPY":
        try:
            return "Preço em ienes", f"¥ {int(float(amount)):,}"
        except (TypeError, ValueError):
            return "Preço em ienes", f"¥ {amount}"
    return "Preço", f"{amount} {currency}".strip()


def send_discord_alert(webhook_url, listing, *, search_name, source):
    url = listing["url"]
    photo_urls = listing["photo_urls"][:MAX_PHOTOS]
    username = f"{source} Monitor"
    price_name, price_value = _price_field(listing["amount"], listing["currency"])

    # Info embed: seller as the author line, listing name as the linked
    # title, stacked (non-inline) fields, main photo, and a footer that
    # Discord renders as "<source> | <time>" thanks to `timestamp` (the
    # listing's own time where the site gives us one, else now).
    info_embed = {
        "title": (listing["title"] or "")[:256],
        "url": url,
        "color": SOURCE_COLOR.get(source, _DEFAULT_COLOR),
        "fields": [
            {"name": price_name, "value": price_value or "—", "inline": False},
            {"name": "Busca", "value": search_name, "inline": False},
            {"name": "Marca", "value": listing["brand"] or "—", "inline": False},
            {"name": "Tamanho", "value": listing["size"] or "—", "inline": False},
        ],
        "footer": {"text": source},
        "timestamp": listing.get("listed_at") or datetime.now(timezone.utc).isoformat(),
    }
    if listing.get("seller"):
        author = {"name": str(listing["seller"])[:256]}
        if listing.get("seller_url"):
            author["url"] = listing["seller_url"]
        info_embed["author"] = author
    if photo_urls:
        info_embed["image"] = {"url": photo_urls[0]}

    first_msg_embeds = [info_embed] + [
        {"url": url, "image": {"url": u}} for u in photo_urls[1:IMAGES_PER_MESSAGE]
    ]
    # A single link (style 5) button row -- jumps to the listing. If the
    # webhook isn't application-owned Discord returns 400; _post_discord
    # then retries the same message without the button.
    button_row = [{
        "type": 1,
        "components": [
            {"type": 2, "style": 5, "label": LINK_BUTTON_LABEL, "url": url},
        ],
    }]
    messages = [{
        "username": username,
        "embeds": first_msg_embeds,
        "components": button_row,
    }]

    # Remaining photos: image-only embeds, all sharing the item URL so each
    # message renders as its own 4-up grid.
    for start in range(IMAGES_PER_MESSAGE, len(photo_urls), IMAGES_PER_MESSAGE):
        chunk = photo_urls[start:start + IMAGES_PER_MESSAGE]
        messages.append({
            "username": username,
            "embeds": [{"url": url, "image": {"url": u}} for u in chunk],
        })

    log.debug("Discord: %d mensagem(ns), %d foto(s)", len(messages), len(photo_urls))
    for idx, payload in enumerate(messages):
        _post_discord(webhook_url, payload)
        if idx < len(messages) - 1:
            time.sleep(0.5)  # stay under the per-webhook rate limit


def _post_discord(webhook_url, payload):
    try:
        resp = requests.post(webhook_url, json=payload, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 400 and "components" in payload:
            # Plain incoming webhooks can't attach buttons -- resend the same
            # message without the component row.
            log.warning("Discord rejeitou o botão (400); a reenviar sem botão.")
            payload = {k: v for k, v in payload.items() if k != "components"}
            resp = requests.post(webhook_url, json=payload, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 429:
            # Discord rate limit -- back off for the time it tells us to
            retry_after = resp.json().get("retry_after", 2)
            log.warning("Discord rate-limited, waiting %.1fs", retry_after)
            time.sleep(retry_after)
            resp = requests.post(webhook_url, json=payload, timeout=REQUEST_TIMEOUT)

        if not resp.ok:
            log.error("Discord webhook falhou (%s): %s", resp.status_code, resp.text[:500])
    except requests.RequestException as e:
        log.error("Discord webhook error: %s", e)


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def _fetch(provider: str, query, clients: dict) -> list[dict]:
    return clients[provider].search(query)


def _plan_searches() -> list[tuple]:
    """Parse every search URL once, at startup. Returns
    (search, provider, query) tuples, skipping any entry with a URL that
    won't parse (logged, not fatal)."""
    plan = []
    for search in SEARCHES:
        provider = search.get("provider", "vinted")
        parser = mercari_condition_from_url if provider == "mercari" else params_from_search_url
        try:
            query = parser(search["url"])
        except Exception as e:
            log.error("Busca '%s' tem um URL inválido (%s) -- ignorada.", search["name"], e)
            continue
        plan.append((search, provider, query))
    return plan


def _trim_seen(previous: list, added: list) -> list:
    """Old IDs first, then the ones added this round; drop duplicates while
    keeping order, then cap at SEEN_CAP (oldest fall off the front)."""
    return list(dict.fromkeys([*previous, *added]))[-SEEN_CAP:]


def run():
    seen = load_seen()
    plan = _plan_searches()
    if not plan:
        log.error("Nenhuma busca válida configurada. A sair.")
        return

    clients: dict = {}
    for provider in {p for _, p, _ in plan}:
        clients[provider] = VintedClient(domain=DOMAIN) if provider == "vinted" else MercariClient()

    log.info(
        "Monitor iniciado. %d busca(s), ronda a cada ~%ds, até %d foto(s) por alerta.",
        len(plan), POLL_INTERVAL_SECONDS, MAX_PHOTOS,
    )
    for search, provider, _ in plan:
        channel_note = (
            "canal próprio" if search.get("webhook")
            else ("canal padrão" if DISCORD_WEBHOOK_URL else "SEM Discord")
        )
        log.info("  - [%s] %s -> %s", provider, search["name"], channel_note)

    try:
        while True:
            round_start = time.monotonic()
            for search, provider, query in plan:
                name = search["name"]
                previous = seen.get(name, [])
                seen_set = set(previous)
                first_time = name not in seen

                try:
                    items = _fetch(provider, query, clients)
                except requests.RequestException as e:
                    log.error("Busca '%s' falhou: %s", name, e)
                    continue
                except Exception as e:
                    log.exception("Erro inesperado na busca '%s': %s", name, e)
                    continue

                new_items = [it for it in items if str(it.get("id")) not in seen_set]
                added: list[str] = []

                if first_time and PRIME_ON_FIRST_RUN:
                    added = [str(it.get("id")) for it in new_items]
                    log.info(
                        "Primeira verificação de '%s': %d item(s) registados sem alertar.",
                        name, len(added),
                    )
                else:
                    to_alert = new_items
                    if len(new_items) > MAX_ALERTS_PER_CYCLE:
                        log.warning(
                            "'%s': %d itens novos de uma vez; a alertar os %d mais "
                            "recentes, o resto só registado.",
                            name, len(new_items), MAX_ALERTS_PER_CYCLE,
                        )
                        to_alert = new_items[:MAX_ALERTS_PER_CYCLE]
                        added += [str(it.get("id")) for it in new_items[MAX_ALERTS_PER_CYCLE:]]

                    build = _LISTING_BUILDERS[provider]
                    for item in reversed(to_alert):  # oldest-of-the-new first
                        try:
                            notify(search, build(item, clients.get(provider)))
                        except Exception as e:
                            log.exception("Falha ao processar um item de '%s': %s", name, e)
                        added.append(str(item.get("id")))  # mark seen either way
                        if provider == "mercari":
                            time.sleep(MERCARI_ITEM_DELAY)

                seen[name] = _trim_seen(previous, added)

            save_seen(seen)

            elapsed = time.monotonic() - round_start
            if RUN_ONCE:
                log.info("RUN_ONCE: ronda concluída em %.0fs, a sair.", elapsed)
                break
            sleep_for = max(
                30,
                POLL_INTERVAL_SECONDS
                + random.uniform(-JITTER_SECONDS, JITTER_SECONDS)
                - elapsed,
            )
            log.info("Ronda completa em %.0fs. Próxima em %.0fs.", elapsed, sleep_for)
            time.sleep(sleep_for)

    except KeyboardInterrupt:
        log.info("Parado pelo utilizador.")


if __name__ == "__main__":
    run()
