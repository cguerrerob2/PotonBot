from datetime import datetime, timezone

import aiohttp

# GeckoTerminal: fuente fallback (indexa venues que Dexscreener no cubre: Pons, Stonks.fun...)
GT_NETWORKS = {"SOL": "solana", "BASE": "base", "BSC": "bsc", "HOOD": "robinhood"}

GT_API = "https://api.geckoterminal.com/api/v2"


def _to_ms(iso: str):
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return None


async def fetch_token_info_gt(chain: str, token: str, timeout: int = 10) -> dict:
    """Datos del token desde GeckoTerminal (mismo formato que fetch_token_info)."""
    network = GT_NETWORKS.get(chain)
    if not network:
        return {}
    headers = {"Accept": "application/json"}
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(f"{GT_API}/networks/{network}/tokens/{token}",
                                   timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status != 200:
                    return {}
                token_data = await resp.json()
            async with session.get(f"{GT_API}/networks/{network}/tokens/{token}/pools",
                                   timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                pools_data = await resp.json() if resp.status == 200 else {}

        attrs = (token_data.get("data") or {}).get("attributes") or {}
        pools = pools_data.get("data") or []
        # mejor pool = mayor reserva (liquidez)
        best = None
        best_reserve = -1.0
        for p in pools:
            pa = p.get("attributes") or {}
            try:
                r = float(pa.get("reserve_in_usd") or 0)
            except (TypeError, ValueError):
                r = 0
            if r > best_reserve:
                best_reserve = r
                best = p
        pa = (best or {}).get("attributes") or {}
        vol = attrs.get("volume_usd") or {}
        txns = pa.get("transactions") or {}
        m5 = txns.get("m5") or {}
        chg = pa.get("price_change_percentage") or {}
        pool_addr = pa.get("address") or ""
        dex_id = (((best or {}).get("relationships") or {}).get("dex") or {}).get("data", {}).get("id", "")

        pair_url = f"https://www.geckoterminal.com/{network}/pools/{pool_addr}" if pool_addr else ""

        return {
            "symbol": attrs.get("symbol") or "",
            "name": attrs.get("name") or "",
            "price_usd": attrs.get("price_usd"),
            "mcap": attrs.get("market_cap_usd") or attrs.get("fdv_usd"),
            "fdv": attrs.get("fdv_usd"),
            "liquidity": best_reserve if best_reserve >= 0 else None,
            "pair_url": pair_url,
            "pair_address": pool_addr,
            "dex_id": dex_id,
            "vol24": vol.get("h24"),
            "vol5": pa.get("volume_usd", {}).get("m5"),
            "chg5": chg.get("m5"),
            "chg1h": chg.get("h1"),
            "buys5": m5.get("buys"),
            "sells5": m5.get("sells"),
            "created_ms": _to_ms(pa.get("pool_created_at") or ""),
            "image": attrs.get("image_url") or "",
            "twitter": "",
            "telegram": "",
            "web": "",
        }
    except Exception:
        return {}
