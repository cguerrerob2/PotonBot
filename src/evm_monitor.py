import asyncio

import aiohttp

from .logutil import log as _log

# Etherscan V2 (BASE/BSC) + JSON-RPC directo para Robinhood Chain (Blockscout bloquea datacenters)
ETHERSCAN_V2 = "https://api.etherscan.io/v2/api"
HOOD_RPC = "https://rpc.mainnet.chain.robinhood.com"

# keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

CHAINS = {
    "8453": {"name": "BASE", "explorer": "https://basescan.org/tx/", "api": ETHERSCAN_V2, "needs_key": True},
    "56": {"name": "BSC", "explorer": "https://bscscan.com/tx/", "api": ETHERSCAN_V2, "needs_key": True},
    "4663": {"name": "HOOD", "explorer": "https://robinhoodchain.blockscout.com/tx/", "mode": "rpc", "rpc": HOOD_RPC},
}

# Stables/wrapped excluded per chain (lowercase addresses)
EXCLUDED_TOKENS = {
    "8453": {
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
        "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",  # USDbC
        "0x4200000000000000000000000000000000000006",  # WETH
    },
    "56": {
        "0x55d398326f99059ff775485246999027b3197955",  # USDT
        "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",  # USDC
        "0xe9e7cea3dedca5984780bafc599bd69add087d56",  # BUSD
        "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",  # WBNB
    },
    "4663": set(),  # simbolos filtrados abajo
}

# Simbolos excluidos en TODAS las chains EVM (stables/wrapped/gas tokens)
EXCLUDED_SYMBOLS = {
    "USDC", "USDT", "USDBC", "DAI", "WETH", "WBNB", "WBTC", "BUSD",
    "USDG", "SPUSDG", "USDS", "USDE", "WSTETH", "STETH",
}


def log(msg: str):
    _log("EVM", msg)


class EvmMonitor:
    """Monitor for EVM wallets (0x...) on BASE & BSC (Etherscan V2) + HOOD (raw JSON-RPC logs)."""

    def __init__(self, api_key: str, wallets: list, tracker, state, cfg: dict):
        self.api_key = api_key
        self.wallets = wallets
        self.tracker = tracker
        self.state = state
        self.interval = cfg.get("poll_interval_sec", 15)
        self.per_cycle = cfg.get("wallets_per_cycle", 5)
        self.txs_limit = cfg.get("txs_limit", 10)
        self.offset = 0
        self.session: aiohttp.ClientSession | None = None
        self._meta_cache: dict = {}      # token -> (symbol, decimals)
        self._contract_cache: dict = {}  # address -> bool (es contrato)

    # ---------------- JSON-RPC (HOOD) ----------------

    async def _rpc_call(self, rpc_url: str, method: str, params: list):
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        for attempt in range(3):
            try:
                async with self.session.post(rpc_url, json=payload) as resp:
                    if resp.status in (403, 429):
                        await asyncio.sleep(3 + attempt * 4)
                        continue
                    resp.raise_for_status()
                    data = await resp.json()
                    return data.get("result")
            except Exception as e:
                log(f"RPC error {method}: {e}")
                await asyncio.sleep(3 + attempt * 4)
        return None

    @staticmethod
    def _decode_symbol(hexstr) -> str:
        if not hexstr or hexstr == "0x":
            return "?"
        h = hexstr[2:]
        try:
            if len(h) >= 128:  # ABI string: offset + len + data
                strlen = int(h[64:128], 16)
                s = bytes.fromhex(h[128:128 + strlen * 2]).decode("utf-8", errors="ignore").strip()
                if s:
                    return s
            if len(h) >= 64:  # bytes32 variant
                s = bytes.fromhex(h[:64]).rstrip(b"\x00").decode("utf-8", errors="ignore").strip()
                if s:
                    return s
        except Exception:
            pass
        return "?"

    async def _token_meta(self, rpc_url: str, token: str) -> tuple:
        if token in self._meta_cache:
            return self._meta_cache[token]
        sym_res, dec_res = await asyncio.gather(
            self._rpc_call(rpc_url, "eth_call", [{"to": token, "data": "0x95d89b41"}, "latest"]),
            self._rpc_call(rpc_url, "eth_call", [{"to": token, "data": "0x313ce567"}, "latest"]),
        )
        symbol = self._decode_symbol(sym_res)
        try:
            decimals = int(dec_res, 16) if dec_res else 18
        except (TypeError, ValueError):
            decimals = 18
        self._meta_cache[token] = (symbol, decimals)
        return self._meta_cache[token]

    async def _poll_rpc_wallet(self, chain_id: str, chain: dict, w: dict, latest_block: int):
        addr = w["address"].lower()
        last = self.state.get_evm_last_block(addr, chain_id)
        if last == 0:
            # Never initialized -> baseline silently
            self.state.set_evm_last_block(addr, chain_id, latest_block)
            self.state.save()
            return
        if latest_block <= last:
            return
        padded = "0x" + "0" * 24 + addr[2:]
        logs = await self._rpc_call(chain["rpc"], "eth_getLogs", [{
            "fromBlock": hex(last + 1),
            "toBlock": hex(latest_block),
            "topics": [TRANSFER_TOPIC, None, padded],
        }])
        self.state.set_evm_last_block(addr, chain_id, latest_block)
        self.state.save()
        for lg in logs or []:
            await self._process_log(chain_id, chain, w, lg)

    async def _is_contract(self, rpc_url: str, address: str) -> bool:
        if address in self._contract_cache:
            return self._contract_cache[address]
        code = await self._rpc_call(rpc_url, "eth_getCode", [address, "latest"])
        is_c = bool(code and code != "0x")
        self._contract_cache[address] = is_c
        return is_c

    async def _process_log(self, chain_id: str, chain: dict, wallet: dict, lg: dict):
        addr = wallet["address"].lower()
        token = (lg.get("address") or "").lower()
        topics = lg.get("topics") or []
        if len(topics) < 3 or not token:
            return
        sender = ("0x" + topics[1][-40:]).lower()
        if sender == addr:
            return
        # Solo compras reales: tokens desde un CONTRATO (pool/router DEX).
        # Si vienen de una EOA es airdrop/transferencia -> fuera.
        if not await self._is_contract(chain["rpc"], sender):
            return
        if token in EXCLUDED_TOKENS.get(chain_id, set()):
            return
        symbol, decimals = await self._token_meta(chain["rpc"], token)
        if symbol.upper() in EXCLUDED_SYMBOLS:
            return
        try:
            amount = int(lg.get("data", "0x0"), 16) / (10 ** decimals)
        except (TypeError, ValueError, ZeroDivisionError):
            amount = 0.0
        count, score_sum = await self.tracker.add_buy(chain["name"], token, wallet["address"], {
            "name": wallet["rename"],
            "emoji": wallet.get("emoji", ""),
            "wallet": wallet["address"],
            "amount": amount,
            "symbol": symbol,
            "tx_hash": lg.get("transactionHash") or "",
            "score": wallet.get("score", 0.3),
        })
        n = abs(count)
        log(f"[{chain['name']}] {wallet.get('emoji','')} {wallet['rename']} (score {wallet.get('score', 0.3):.2f}) received {symbol} ({n} wallets, combined score {score_sum:.2f})")

    async def run(self):
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            self.session = session
            await self._init_state()
            log(f"Monitor started: {len(self.wallets)} wallets x {len(CHAINS)} chains")
            while True:
                try:
                    await self._cycle()
                except Exception as e:
                    log(f"Cycle error: {e}")
                await asyncio.sleep(self.interval)

    async def _fetch_tokentx(self, chain_id: str, address: str) -> list:
        chain = CHAINS[chain_id]
        params = {
            "module": "account",
            "action": "tokentx",
            "address": address,
            "page": 1,
            "offset": self.txs_limit,
            "sort": "desc",
        }
        if chain["needs_key"]:
            params["chainid"] = chain_id
            params["apikey"] = self.api_key
        for attempt in range(3):
            try:
                async with self.session.get(chain["api"], params=params) as resp:
                    if resp.status == 429:
                        await asyncio.sleep(2 + attempt * 3)
                        continue
                    resp.raise_for_status()
                    data = await resp.json()
                    if data.get("status") == "1" and isinstance(data.get("result"), list):
                        return data["result"]
                    return []  # status 0 = no transactions (or error message)
            except Exception as e:
                log(f"API error chain {chain_id} {address[:8]}...: {e}")
                await asyncio.sleep(2 + attempt * 3)
        return []

    async def _init_state(self):
        log("Initializing state (no alerts)...")
        # RPC chains: baseline = bloque actual (una sola llamada para todas las wallets)
        for chain_id, chain in CHAINS.items():
            if chain.get("mode") != "rpc":
                continue
            latest = await self._rpc_call(chain["rpc"], "eth_blockNumber", [])
            if latest:
                n = int(latest, 16)
                for w in self.wallets:
                    if self.state.get_evm_last_block(w["address"].lower(), chain_id) == 0:
                        self.state.set_evm_last_block(w["address"].lower(), chain_id, n)
                log(f"[{chain['name']}] baseline at block {n}")
        # API chains (Etherscan V2)
        for w in self.wallets:
            addr = w["address"].lower()
            for chain_id, chain in CHAINS.items():
                if chain.get("mode") == "rpc":
                    continue
                if self.state.get_evm_last_ts(addr, chain_id) > 0:
                    continue
                txs = await self._fetch_tokentx(chain_id, addr)
                if txs:
                    self.state.set_evm_last_ts(addr, chain_id, int(txs[0]["timeStamp"]))
                await asyncio.sleep(0.3)  # free tier: ~5 req/s
        self.state.save()
        log("Initial state ready.")

    def _next_chunk(self) -> list:
        n = len(self.wallets)
        if n == 0:
            return []
        chunk = [self.wallets[(self.offset + j) % n] for j in range(min(self.per_cycle, n))]
        self.offset = (self.offset + self.per_cycle) % n
        return chunk

    async def _cycle(self):
        chunk = self._next_chunk()
        if not chunk:
            return
        # Latest block de las chains RPC (una llamada por ciclo)
        latest_blocks = {}
        for chain_id, chain in CHAINS.items():
            if chain.get("mode") == "rpc":
                res = await self._rpc_call(chain["rpc"], "eth_blockNumber", [])
                if res:
                    latest_blocks[chain_id] = int(res, 16)
        for w in chunk:
            addr = w["address"].lower()
            for chain_id, chain in CHAINS.items():
                if chain.get("mode") == "rpc":
                    if chain_id in latest_blocks:
                        await self._poll_rpc_wallet(chain_id, chain, w, latest_blocks[chain_id])
                    await asyncio.sleep(0.3)
                    continue
                txs = await self._fetch_tokentx(chain_id, addr)
                if not txs:
                    await asyncio.sleep(0.3)
                    continue
                last_ts = self.state.get_evm_last_ts(addr, chain_id)
                if last_ts == 0:
                    # Never initialized (init failed) -> baseline silently, never alert old txs
                    self.state.set_evm_last_ts(addr, chain_id, int(txs[0]["timeStamp"]))
                    self.state.save()
                    await asyncio.sleep(0.3)
                    continue
                new_txs = [t for t in txs if int(t.get("timeStamp", 0)) > last_ts]
                if new_txs:
                    self.state.set_evm_last_ts(addr, chain_id, int(txs[0]["timeStamp"]))
                    self.state.save()
                    # Oldest first
                    for t in reversed(new_txs):
                        await self._process_tx(chain_id, chain, w, t)
                await asyncio.sleep(0.3)

    async def _process_tx(self, chain_id: str, chain: dict, wallet: dict, t: dict):
        addr = wallet["address"].lower()
        token = (t.get("contractAddress") or "").lower()
        # Only tokens flowing INTO the wallet (buy/receive), not out
        if (t.get("to") or "").lower() != addr:
            return
        if (t.get("from") or "").lower() == addr:
            return
        if token in EXCLUDED_TOKENS.get(chain_id, set()):
            return
        if (t.get("tokenSymbol") or "").upper() in EXCLUDED_SYMBOLS:
            return
        try:
            amount = float(t.get("value", 0)) / (10 ** int(t.get("tokenDecimal", 18)))
        except (TypeError, ValueError, ZeroDivisionError):
            amount = 0.0
        count, score_sum = await self.tracker.add_buy(chain["name"], token, wallet["address"], {
            "name": wallet["rename"],
            "emoji": wallet.get("emoji", ""),
            "wallet": wallet["address"],
            "amount": amount,
            "symbol": t.get("tokenSymbol") or "",
            "tx_hash": t.get("hash") or "",
            "score": wallet.get("score", 0.3),
        })
        n = abs(count)
        log(f"[{chain['name']}] {wallet.get('emoji','')} {wallet['rename']} (score {wallet.get('score', 0.3):.2f}) received {t.get('tokenSymbol','?')} ({n} wallets, combined score {score_sum:.2f})")
