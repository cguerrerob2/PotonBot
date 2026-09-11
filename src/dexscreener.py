import time
from datetime import datetime, timezone

import aiohttp

CHAIN_SLUGS = {"SOL": "solana", "BASE": "base", "BSC": "bsc", "HOOD": "robinhood"}

EXPLORERS = {
    "SOL": "https://solscan.io/token/",
    "BASE": "https://basescan.org/token/",
    "BSC": "https://bscscan.com/token/",
    "HOOD": "https://robinhoodchain.blockscout.com/token/",
}

WSOL_MINT = "So11111111111111111111111111111111111111112"
_sol_price_cache = {"price": None, "ts": 0.0}


async def fetch_sol_price() -> float | None:
    """Precio actual de SOL en USD via Dexscreener (cache 5 min)."""
    now = time.time()
    if _sol_price_cache["price"] and now - _sol_price_cache["ts"] < 300:
        return _sol_price_cache["price"]
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{WSOL_MINT}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    return _sol_price_cache["price"]
                data = await resp.json()
        pairs = data.get("pairs") or []
        best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
        price = float(best.get("priceUsd"))
        _sol_price_cache.update({"price": price, "ts": now})
        return price
    except Exception:
        return _sol_price_cache["price"]


async def fetch_token_info(chain: str, token: str, timeout: int = 8) -> dict:
    """Datos completos del token: Dexscreener primero, GeckoTerminal como fallback (Pons/Stonks/etc)."""
    info = await _fetch_dexscreener(chain, token, timeout)
    if info:
        return info
    return await _fetch_geckoterminal(chain, token, timeout)


GT_NETWORKS = {"SOL": "solana", "BASE": "base", "BSC": "bsc", "HOOD": "robinhood"}


async def _fetch_geckoterminal(chain: str, token: str, timeout: int = 10) -> dict:
    net = GT_NETWORKS.get(chain)
    if not net:
        return {}
    try:
        async with aiohttp.ClientSession() as session:
            t_url = f"https://api.geckoterminal.com/api/v2/networks/{net}/tokens/{token}"
            p_url = f"https://api.geckoterminal.com/api/v2/networks/{net}/tokens/{token}/pools?page=1"
            async with session.get(t_url, timeout=aiohttp.ClientTimeout(total=timeout)) as r1:
                t_data = await r1.json() if r1.status == 200 else {}
            async with session.get(p_url, timeout=aiohttp.ClientTimeout(total=timeout)) as r2:
                p_data = await r2.json() if r2.status == 200 else {}

        t_attrs = ((t_data.get("data") or {}).get("attributes") or {})
        pools = p_data.get("data") or []
        if not pools:
            return {}
        best = max(pools, key=lambda p: float((p.get("attributes") or {}).get("reserve_in_usd") or 0))
        a = best.get("attributes") or {}
        rel = best.get("relationships") or {}

        def f(x):
            try:
                return float(x)
            except (TypeError, ValueError):
                return None

        vol = a.get("volume_usd") or {}
        chg = a.get("price_change_percentage") or {}
        txns = (a.get("transactions") or {}).get("m5") or {}

        created_ms = None
        created_raw = a.get("pool_created_at")
        if created_raw:
            try:
                created_ms = datetime.fromisoformat(created_raw.replace("Z", "+00:00")).timestamp() * 1000
            except (ValueError, TypeError):
                pass

        pool_addr = a.get("address") or ""
        return {
            "symbol": t_attrs.get("symbol") or "",
            "name": t_attrs.get("name") or "",
            "price_usd": a.get("base_token_price_usd") or t_attrs.get("price_usd"),
            "mcap": f(a.get("market_cap_usd")) or f(a.get("fdv_usd")),
            "fdv": f(a.get("fdv_usd")),
            "liquidity": f(a.get("reserve_in_usd")),
            "pair_url": f"https://www.geckoterminal.com/{net}/pools/{pool_addr}" if pool_addr else "",
            "pair_address": pool_addr,
            "dex_id": ((rel.get("dex") or {}).get("data") or {}).get("id") or "",
            "vol24": f(vol.get("h24")),
            "vol5": f(vol.get("m5")),
            "chg5": f(chg.get("m5")),
            "chg1h": f(chg.get("h1")),
            "buys5": txns.get("buys"),
            "sells5": txns.get("sells"),
            "created_ms": created_ms,
            "image": t_attrs.get("image_url") or "",
            "twitter": "", "telegram": "", "web": "",
        }
    except Exception:
        return {}


async def _fetch_dexscreener(chain: str, token: str, timeout: int = 8) -> dict:
    """Dexscreener: par con mas liquidez de su chain. {} si no hay datos."""
    slug = CHAIN_SLUGS.get(chain)
    if not slug:
        return {}
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status != 200:
                    return {}
                data = await resp.json()
        pairs = [p for p in (data.get("pairs") or []) if p.get("chainId") == slug]
        if not pairs:
            return {}
        best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)

        base = best.get("baseToken") or {}
        info = best.get("info") or {}
        volume = best.get("volume") or {}
        txns = best.get("txns") or {}
        m5 = txns.get("m5") or {}
        price_change = best.get("priceChange") or {}

        twitter = ""
        web = ""
        telegram = ""
        for s in info.get("socials") or []:
            t = (s.get("type") or "").lower()
            if t == "twitter":
                twitter = s.get("url") or ""
            elif t == "telegram":
                telegram = s.get("url") or ""
        webs = info.get("websites") or []
        if webs:
            web = webs[0].get("url") or ""

        return {
            "symbol": base.get("symbol") or "",
            "name": base.get("name") or "",
            "price_usd": best.get("priceUsd"),
            "mcap": best.get("marketCap") or best.get("fdv"),
            "fdv": best.get("fdv"),
            "liquidity": (best.get("liquidity") or {}).get("usd"),
            "pair_url": best.get("url"),
            "pair_address": best.get("pairAddress") or "",
            "dex_id": best.get("dexId") or "",
            "vol24": volume.get("h24"),
            "vol5": volume.get("m5"),
            "chg5": price_change.get("m5"),
            "chg1h": price_change.get("h1"),
            "buys5": m5.get("buys"),
            "sells5": m5.get("sells"),
            "created_ms": best.get("pairCreatedAt"),
            "image": info.get("imageUrl") or "",
            "twitter": twitter,
            "telegram": telegram,
            "web": web,
        }
    except Exception:
        return {}


def fmt_usd(value) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "?"
    if v >= 1_000_000_000:
        return f"{v/1_000_000_000:.2f}B"
    if v >= 1_000_000:
        return f"{v/1_000_000:.2f}M"
    if v >= 1_000:
        return f"{v/1_000:.1f}K"
    return f"{v:,.2f}"


def fmt_amount(value) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "?"
    if v >= 1_000_000_000:
        return f"{v/1_000_000_000:.2f}B"
    if v >= 1_000_000:
        return f"{v/1_000_000:.2f}M"
    if v >= 1_000:
        return f"{v/1_000:.1f}K"
    return f"{v:,.4g}"


def fmt_age(created_ms) -> str:
    """Edad del par: '5m', '3h', '2d'..."""
    try:
        age_sec = max(0, time.time() - float(created_ms) / 1000)
    except (TypeError, ValueError):
        return "?"
    if age_sec < 3600:
        return f"{int(age_sec // 60)}m"
    if age_sec < 86400:
        return f"{int(age_sec // 3600)}h"
    return f"{int(age_sec // 86400)}d"
