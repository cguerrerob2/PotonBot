import time

import aiohttp

CHAIN_SLUGS = {"SOL": "solana", "ETH": "ethereum", "BASE": "base", "BSC": "bsc"}

EXPLORERS = {
    "SOL": "https://solscan.io/token/",
    "ETH": "https://etherscan.io/token/",
    "BASE": "https://basescan.org/token/",
    "BSC": "https://bscscan.com/token/",
}


async def fetch_token_info(chain: str, token: str, timeout: int = 8) -> dict:
    """Datos completos del token desde Dexscreener (par con mas liquidez de su chain)."""
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
