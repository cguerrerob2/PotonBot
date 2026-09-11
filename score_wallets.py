"""
Score wallets by real on-chain PNL (Helius Enhanced Transactions API).

For every SOL wallet: fetches the last N swaps, computes net SOL flow
(SOL received - SOL sent, including fees/tips) as a realized-PNL proxy,
then percentile-normalizes to a 0.05-1.0 score.

EVM wallets get the default score (edit scores.json "_overrides" to pin any wallet).

Usage:  python score_wallets.py
Output: scores.json  (+ leaderboard printed to console)
Re-runnable: wallets already scored are skipped (delete scores.json to rescore all).
"""

import asyncio
import json
import os
import sys
import time

import aiohttp
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WSOL = "So11111111111111111111111111111111111111112"

SWAPS_PER_WALLET = 100
DEFAULT_SCORE_EVM = 0.35
DEFAULT_SCORE_NO_DATA = 0.20
MIN_SWAPS_FOR_SCORE = 3


def log(msg: str):
    print(f"{time.strftime('%H:%M:%S')} [SCORER] {msg}", flush=True)


def helius_key() -> str:
    load_dotenv(os.path.join(BASE_DIR, ".env"))
    url = os.getenv("SOLANA_RPC_URL", "")
    if "api-key=" in url:
        return url.split("api-key=")[-1].split("&")[0].strip()
    return url.strip()


def swap_net_sol(tx: dict, wallet: str) -> float:
    """Net SOL gained by the wallet in this swap (WSOL SPL legs + native legs)."""
    wsol_in = wsol_out = 0.0
    for t in tx.get("tokenTransfers") or []:
        if t.get("mint") != WSOL:
            continue
        try:
            amt = float(t.get("tokenAmount") or 0)
        except (TypeError, ValueError):
            continue
        if t.get("toUserAccount") == wallet:
            wsol_in += amt
        if t.get("fromUserAccount") == wallet:
            wsol_out += amt
    n_in = n_out = 0.0
    for n in tx.get("nativeTransfers") or []:
        amt = (n.get("amount") or 0) / 1e9
        if n.get("toUserAccount") == wallet:
            n_in += amt
        if n.get("fromUserAccount") == wallet:
            n_out += amt
    return (wsol_in + n_in) - (wsol_out + n_out)


async def fetch_swaps(session: aiohttp.ClientSession, key: str, wallet: str) -> list:
    url = f"https://api.helius.xyz/v0/addresses/{wallet}/transactions?api-key={key}&type=SWAP&limit={SWAPS_PER_WALLET}"
    delay = 2
    for _ in range(4):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=25)) as r:
                if r.status == 429:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 20)
                    continue
                if r.status != 200:
                    return []
                data = await r.json()
                return data if isinstance(data, list) else []
        except Exception:
            await asyncio.sleep(delay)
            delay = min(delay * 2, 20)
    return []


async def main():
    wallets = json.load(open(os.path.join(BASE_DIR, "wallets.json"), encoding="utf-8"))
    sol_wallets = [w for w in wallets if not w["address"].startswith("0x")]
    evm_wallets = [w for w in wallets if w["address"].startswith("0x")]

    scores_path = os.path.join(BASE_DIR, "scores.json")
    scores = {}
    if os.path.exists(scores_path):
        try:
            scores = json.load(open(scores_path, encoding="utf-8"))
        except Exception:
            scores = {}
    overrides = scores.get("_overrides", {})

    key = helius_key()
    if not key:
        log("No Helius API key found in SOLANA_RPC_URL")
        sys.exit(1)

    log(f"Scoring {len(sol_wallets)} SOL wallets (last {SWAPS_PER_WALLET} swaps each)...")
    t0 = time.time()
    done = 0
    async with aiohttp.ClientSession() as session:
        for w in sol_wallets:
            addr = w["address"]
            if addr in scores:  # resume support
                done += 1
                continue
            txs = await fetch_swaps(session, key, addr)
            pnl = 0.0
            wins = 0
            for tx in txs:
                net = swap_net_sol(tx, addr)
                pnl += net
                if net > 0:
                    wins += 1
            scores[addr] = {
                "name": w.get("rename", ""),
                "pnl_sol": round(pnl, 3),
                "swaps": len(txs),
                "win_txs": wins,
                "chain": "SOL",
            }
            done += 1
            if done % 25 == 0:
                _save(scores_path, scores, overrides)
                log(f"{done}/{len(sol_wallets)} scored...")
            await asyncio.sleep(0.5)  # ~2 req/s

    # EVM wallets: default score (no PNL source on free tier)
    for w in evm_wallets:
        scores.setdefault(w["address"], {
            "name": w.get("rename", ""),
            "pnl_sol": None,
            "swaps": 0,
            "win_txs": 0,
            "chain": "EVM",
        })

    # ---- Percentile -> score (only SOL wallets with enough data) ----
    scored = [(a, d) for a, d in scores.items()
              if not a.startswith("_") and d.get("chain") == "SOL" and (d.get("swaps") or 0) >= MIN_SWAPS_FOR_SCORE]
    scored.sort(key=lambda x: x[1]["pnl_sol"])
    n = len(scored)
    for rank, (addr, d) in enumerate(scored, start=1):
        pct = (rank - 1) / max(n - 1, 1)
        d["score"] = round(0.05 + 0.95 * pct, 3)  # 0.05 .. 1.0
    for addr, d in scores.items():
        if addr.startswith("_"):
            continue
        if "score" not in d:
            d["score"] = DEFAULT_SCORE_EVM if d.get("chain") == "EVM" else DEFAULT_SCORE_NO_DATA

    # Manual overrides always win
    for addr, sc in overrides.items():
        if addr in scores:
            scores[addr]["score"] = sc
            scores[addr]["overridden"] = True

    _save(scores_path, scores, overrides)
    dt = time.time() - t0
    log(f"DONE in {dt:.0f}s -> scores.json ({len(scores) - 1} wallets)")

    # Leaderboard
    board = sorted(((a, d) for a, d in scores.items() if not a.startswith("_")),
                   key=lambda x: -(x[1].get("score") or 0))
    print("\n===== TOP 25 TRADERS BY REAL PNL =====")
    for addr, d in board[:25]:
        pnl = d.get("pnl_sol")
        pnl_txt = f"{pnl:+.1f} SOL" if pnl is not None else "  n/a (EVM)"
        print(f"  {d['score']:.2f}  {d.get('name','')[:22]:<22} {pnl_txt:<14} swaps:{d.get('swaps',0)}")


def _save(path, scores, overrides):
    scores["_overrides"] = overrides
    scores["_meta"] = {"updated": time.strftime("%Y-%m-%d %H:%M:%S"), "swaps_per_wallet": SWAPS_PER_WALLET}
    json.dump(scores, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    asyncio.run(main())
