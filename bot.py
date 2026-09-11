import asyncio
import json
import os
import re
import sys

import aiohttp
from dotenv import load_dotenv

from src.tracker import BuyTracker
from src.state import State
from src.solana_monitor import SolanaMonitor
from src.evm_monitor import EvmMonitor
from src.discord_bot import PotonCalledBot
from src.dexscreener import fetch_token_info
from src.solana_extras import get_top_holders, get_pump_info
from src.logutil import log as _log

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SOL_RPC = "https://api.mainnet-beta.solana.com"


def normalize_rpc(url: str) -> tuple[str, str | None]:
    """Returns (rpc_url, warning). Accepts full URLs or bare Helius API keys."""
    u = (url or "").strip()
    if not u:
        return DEFAULT_SOL_RPC, "SOLANA_RPC_URL empty — using public RPC (heavy rate limits, get a free Helius key)"
    if u.startswith("http://") or u.startswith("https://"):
        return u, None
    # Bare Helius API key (UUID) without URL
    if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", u):
        return f"https://mainnet.helius-rpc.com/?api-key={u}", "SOLANA_RPC_URL was a bare Helius key — auto-built full URL"
    return DEFAULT_SOL_RPC, f"SOLANA_RPC_URL '{u[:30]}' is not a valid URL — using public RPC (slow!)"


async def check_rpc_health(rpc_url: str) -> bool:
    """Quick getSlot call to verify the RPC works before starting monitors."""
    try:
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getSlot", "params": []}
        async with aiohttp.ClientSession() as s:
            async with s.post(rpc_url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return False
                data = await r.json()
                return "result" in data
    except Exception:
        return False


def setup_file_logging():
    """Tee: everything printed also goes to logs/bot.log (simple rotation)."""
    logs_dir = os.path.join(BASE_DIR, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    log_path = os.path.join(logs_dir, "bot.log")
    mode = "a"
    if os.path.exists(log_path) and os.path.getsize(log_path) > 5 * 1024 * 1024:
        mode = "w"  # reset log if it grows past 5 MB
    logfile = open(log_path, mode, encoding="utf-8", buffering=1)

    class Tee:
        def __init__(self, *streams):
            self.streams = [s for s in streams if s is not None]

        def write(self, s):
            for st in self.streams:
                try:
                    st.write(s)
                except Exception:
                    pass
            return len(s)

        def flush(self):
            for st in self.streams:
                try:
                    st.flush()
                except Exception:
                    pass

    sys.stdout = Tee(sys.__stdout__, logfile)
    sys.stderr = Tee(sys.__stderr__, logfile)


def log(msg: str):
    _log("MAIN", msg)


def load_json(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


async def main():
    load_dotenv(os.path.join(BASE_DIR, ".env"))

    token = os.getenv("DISCORD_TOKEN", "").strip()
    channel_id = os.getenv("DISCORD_CHANNEL_ID", "").strip()
    ping_user = os.getenv("DISCORD_PING_USER_ID", "").strip()
    etherscan_key = os.getenv("ETHERSCAN_API_KEY", "").strip()
    sol_rpc_env = os.getenv("SOLANA_RPC_URL", "").strip()

    if not token or not channel_id or not ping_user:
        log("Missing DISCORD_TOKEN, DISCORD_CHANNEL_ID or DISCORD_PING_USER_ID.")
        log("Fill them in .env (locally) or set them as Variables in Railway.")
        sys.exit(1)

    cfg = load_json(os.path.join(BASE_DIR, "config.json"))
    wallets = load_json(os.path.join(BASE_DIR, "wallets.json"))

    # Classify wallets: 0x... -> EVM (ETH/BASE/BSC), everything else -> Solana
    sol_wallets = [w for w in wallets if not w["address"].startswith("0x")]
    evm_wallets = [w for w in wallets if w["address"].startswith("0x")]
    log(f"Wallets loaded: {len(sol_wallets)} SOL, {len(evm_wallets)} EVM")

    # ---- Discord ----
    bot = PotonCalledBot(token, int(channel_id), ping_user)
    await bot.start()

    # ---- Tracker + alerts ----
    sol_rpc, rpc_warning = normalize_rpc(sol_rpc_env)
    if rpc_warning:
        log(f"WARNING: {rpc_warning}")

    # Health check: if the RPC doesn't respond, warn loudly (and on Discord)
    rpc_ok = await check_rpc_health(sol_rpc)
    if not rpc_ok:
        log("FATAL: Solana RPC is NOT reachable. The SOL monitor cannot work. Fix SOLANA_RPC_URL!")
        await bot.send_rpc_warning(sol_rpc)
    else:
        log(f"Solana RPC OK: {sol_rpc[:60]}")

    async def on_alert(chain, token_addr, count, buys):
        # Dashboard: Dexscreener + (SOL) top holders & pump.fun data, all in parallel
        info, holders, pump = await asyncio.gather(
            fetch_token_info(chain, token_addr),
            get_top_holders(sol_rpc, token_addr) if chain == "SOL" else asyncio.sleep(0, result={}),
            get_pump_info(token_addr) if chain == "SOL" else asyncio.sleep(0, result={}),
        )
        # Exclude the pool/pair from top holders
        pair_addr = (info or {}).get("pair_address")
        if chain == "SOL" and pair_addr and holders:
            holders["top5"] = [(a, p) for a, p in holders["top5"] if a != pair_addr][:5]
        extra = {"holders": holders, "pump": pump}
        await bot.send_alert(chain, token_addr, count, buys, info, extra)

    tracker = BuyTracker(
        threshold=cfg.get("threshold", 5),
        window_sec=int(cfg.get("window_minutes", 30) * 60),
        cooldown_sec=int(cfg.get("alert_cooldown_minutes", 10) * 60),
        realert_extra=int(cfg.get("realert_extra_wallets", 2)),
        on_alert=on_alert,
    )

    state = State(os.path.join(BASE_DIR, "data", "state.json"))

    await bot.send_startup(
        n_sol=len(sol_wallets),
        n_evm=len(evm_wallets) if (evm_wallets and etherscan_key) else 0,
        threshold=cfg.get("threshold", 5),
        window_min=cfg.get("window_minutes", 30),
        rpc_ok=rpc_ok,
    )

    # ---- Monitors ----
    tasks = []
    if sol_wallets:
        sol = SolanaMonitor(sol_rpc, sol_wallets, tracker, state, cfg.get("solana", {}))
        tasks.append(asyncio.create_task(sol.run()))

    if evm_wallets and cfg.get("evm", {}).get("enabled", True):
        if etherscan_key:
            evm = EvmMonitor(etherscan_key, evm_wallets, tracker, state, cfg.get("evm", {}))
            tasks.append(asyncio.create_task(evm.run()))
        else:
            log("EVM wallets found but ETHERSCAN_API_KEY is missing — EVM monitor disabled.")

    if not tasks:
        log("No active monitors. Check wallets.json.")
        sys.exit(1)

    log("PotonCalled is watching. Ctrl+C to stop.")
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    setup_file_logging()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("Bot stopped.")
    except Exception as e:
        log(f"FATAL ERROR: {type(e).__name__}: {e}")
