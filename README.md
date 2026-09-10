# PotonBot (PotonCalled)

Multichain Discord bot that watches a list of tracked wallets and pings you when **5+ wallets ape the same coin** within a time window — with a full token dashboard (MCap, FDV, volume, 5m stats, top holders, dev, links).

## Features

- **Solana**: detects real DEX buys (pump.fun, PumpSwap, Raydium, Jupiter, Orca, Meteora, Moonshot...) via token-balance deltas
- **EVM multichain (ETH / BASE / BSC)**: monitors any `0x...` wallet via Etherscan V2 (one API key for all chains)
- **Alert dashboard** (Rick-style): `CA @you` + embed with FDV/MCap, 24h volume, 5m buys/sells, top holders, pump.fun curve %, dev, and quick links (DEX · AXI · PHO · GMGN · EXP · X)
- Configurable threshold, time window and cooldown in `config.json`
- Tracked wallets in `wallets.json` (`address`, `rename`, `emoji`)

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in your values
python bot.py
```

Required env vars: `DISCORD_TOKEN`, `DISCORD_CHANNEL_ID`, `DISCORD_PING_USER_ID`.
Recommended: `SOLANA_RPC_URL` (free [Helius](https://helius.dev) key — the public RPC is heavily rate-limited).
Optional: `ETHERSCAN_API_KEY` (only if you track EVM wallets).

## Deploy on Railway

1. Push this repo to GitHub (`.env`, `data/`, `logs/` are gitignored)
2. Railway → New Project → Deploy from GitHub repo
3. Add the env vars in the **Variables** tab
4. Nixpacks builds it, the `Procfile` runs `worker: python bot.py` 24/7
