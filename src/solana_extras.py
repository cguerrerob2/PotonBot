import asyncio

import aiohttp

from .logutil import log

# System/program accounts that are never real holders
KNOWN_NON_HOLDERS = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",  # pump.fun program
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",  # PumpSwap AMM
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM v4
}


async def _rpc(session: aiohttp.ClientSession, rpc_url: str, method: str, params: list):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        async with session.post(rpc_url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            return data.get("result")
    except Exception:
        return None


async def get_top_holders(rpc_url: str, mint: str, exclude: set | None = None) -> dict:
    """
    Top token holders via RPC.
    Returns {"top5": [(addr, pct)...], "sum10": pct} or {} on failure.
    """
    exclude = (exclude or set()) | KNOWN_NON_HOLDERS
    try:
        async with aiohttp.ClientSession() as session:
            supply_res, largest_res = await asyncio.gather(
                _rpc(session, rpc_url, "getTokenSupply", [mint]),
                _rpc(session, rpc_url, "getTokenLargestAccounts", [mint]),
            )
        if not supply_res or not largest_res:
            return {}
        total = float((supply_res.get("value") or {}).get("uiAmountString") or 0)
        if total <= 0:
            return {}
        holders = []
        for acc in largest_res.get("value") or []:
            addr = acc.get("address")
            if addr in exclude:
                continue
            try:
                amt = float(acc.get("uiAmountString") or 0)
            except (TypeError, ValueError):
                continue
            if amt > 0:
                holders.append((addr, amt / total * 100))
        if not holders:
            return {}
        return {
            "top5": holders[:5],
            "sum10": sum(p for _, p in holders[:10]),
        }
    except Exception as e:
        log("EXTRA", f"get_top_holders failed: {e}")
        return {}


async def get_pump_info(mint: str) -> dict:
    """
    pump.fun info: curve progress, creator, migration status.
    Returns {} if it's not a pump token or the API doesn't respond.
    """
    urls = [
        f"https://frontend-api-v3.pump.fun/coins/{mint}",
        f"https://frontend-api.pump.fun/coins/{mint}",
    ]
    async with aiohttp.ClientSession() as session:
        for url in urls:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                    if not isinstance(data, dict) or "mint" not in data:
                        continue
                    out = {
                        "complete": bool(data.get("complete")),
                        "creator": data.get("creator") or "",
                    }
                    # Approximate curve progress from virtual reserves
                    try:
                        vsol = float(data.get("virtual_sol_reserves") or 0) / 1e9
                        if vsol > 30:
                            out["progress"] = min(100.0, (vsol - 30) / (85 - 30) * 100)
                    except (TypeError, ValueError):
                        pass
                    return out
            except Exception:
                continue
    return {}
