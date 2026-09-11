"""
Poton - PnL score refresher (LOCAL tool, needs residential IP + real browser).

Fetches wallet stats from GMGN (realized PnL 30d) using a headless Chromium
(Cloudflare blocks plain HTTP clients), ranks every tracked wallet by PnL
percentile and bakes a "score" (0.3 - 1.0) into wallets.json.

Run:  python tools/refresh_scores.py
Then commit + push so Railway gets the updated scores.
"""

import asyncio
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WALLETS_PATH = os.path.join(BASE_DIR, "wallets.json")
SNAPSHOT_PATH = os.path.join(BASE_DIR, "data", "scores_snapshot.json")

GMGN_PAGE = "https://gmgn.ai/sol/address/{addr}"
GMGN_API = "/defi/quotation/v1/smartmoney/{chain}/walletNew/{addr}?period=30d"

NO_DATA_SCORE = 0.30          # wallets sin datos en GMGN
MIN_SCORE, MAX_SCORE = 0.30, 1.0
DELAY = 1.1                   # seg entre wallets (suave con GMGN)


async def fetch_wallet(page, chain: str, addr: str) -> dict | None:
    url = GMGN_API.format(chain=chain, addr=addr)
    for attempt in range(3):
        try:
            result = await page.evaluate(
                """async (u) => {
                    const r = await fetch(u, {headers: {'accept': 'application/json'}});
                    const t = await r.text();
                    return {status: r.status, body: t.slice(0, 200000)};
                }""", url)
            if result["status"] == 200:
                data = json.loads(result["body"])
                d = data.get("data")
                return d if isinstance(d, dict) else None
            if result["status"] == 403:
                return {"__reauth__": True}
            if result["status"] == 429:
                await asyncio.sleep(5 + attempt * 5)
                continue
            return None
        except Exception:
            await asyncio.sleep(3)
    return None


def pnl_of(d: dict):
    for k in ("realized_profit_30d", "realized_profit", "total_profit"):
        v = d.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return None


async def main():
    wallets = json.load(open(WALLETS_PATH, encoding="utf-8"))
    print(f"Wallets: {len(wallets)}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 900},
            locale="en-US",
        )
        page = await ctx.new_page()

        async def reauth():
            await page.goto(GMGN_PAGE.format(addr=wallets[0]["address"]), wait_until="domcontentloaded", timeout=45000)
            for _ in range(20):
                if "moment" not in (await page.title()).lower():
                    break
                await asyncio.sleep(2)
            await page.wait_for_timeout(5000)

        await reauth()
        print("Cloudflare OK, extrayendo stats...")

        stats = {}   # address -> raw dict
        t0 = time.time()
        for i, w in enumerate(wallets):
            addr = w["address"]
            chain = "sol" if not addr.startswith("0x") else "eth"
            d = await fetch_wallet(page, chain, addr)
            if d and d.get("__reauth__"):
                await reauth()
                d = await fetch_wallet(page, chain, addr)
            if d and not d.get("__reauth__"):
                stats[addr] = {
                    "pnl_30d": pnl_of(d),
                    "pnl_pct_30d": d.get("pnl_30d"),
                    "winrate": d.get("winrate"),
                    "buys_30d": d.get("buy_30d"),
                    "sells_30d": d.get("sell_30d"),
                    "total_profit": d.get("total_profit"),
                }
            if (i + 1) % 25 == 0 or i == len(wallets) - 1:
                el = time.time() - t0
                print(f"  {i+1}/{len(wallets)} ({el:.0f}s) - con datos: {len(stats)}")
            await asyncio.sleep(DELAY)

        await browser.close()

    # ---- Scoring: percentil por PnL 30d ----
    with_data = [(a, s["pnl_30d"]) for a, s in stats.items() if s["pnl_30d"] is not None]
    with_data.sort(key=lambda x: x[1])  # peor -> mejor
    n = len(with_data)
    scores = {}
    for rank, (addr, pnl) in enumerate(with_data):
        pct = rank / (n - 1) if n > 1 else 1.0
        scores[addr] = round(MIN_SCORE + (MAX_SCORE - MIN_SCORE) * pct, 3)

    for w in wallets:
        w["score"] = scores.get(w["address"], NO_DATA_SCORE)

    json.dump(wallets, open(WALLETS_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    os.makedirs(os.path.dirname(SNAPSHOT_PATH), exist_ok=True)
    json.dump(
        {"generated_at": int(time.time()), "stats": stats},
        open(SNAPSHOT_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1,
    )

    # ---- Leaderboard ----
    name_of = {w["address"]: w.get("rename", "") for w in wallets}
    print(f"\nCon datos GMGN: {len(with_data)}/{len(wallets)} | sin datos -> score {NO_DATA_SCORE}")
    print("\n=== TOP 25 POR PNL 30d ===")
    for addr, pnl in reversed(with_data[-25:]):
        print(f"  {scores[addr]:.2f}  {pnl:>15,.0f} USD  {name_of.get(addr, '?')}  {addr[:8]}...")
    print("\nwallets.json actualizado con scores. Haz commit + push para Railway.")


asyncio.run(main())
