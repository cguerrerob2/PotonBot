import asyncio

import discord

from .dexscreener import EXPLORERS, CHAIN_SLUGS, fmt_usd, fmt_amount, fmt_age
from .logutil import log

CHAIN_COLORS = {
    "SOL": 0x9945FF,   # Solana purple
    "HOOD": 0x00C805,  # Robinhood green
    "BASE": 0x0052FF,  # Base blue
    "BSC": 0xF0B90B,   # BSC yellow
}

GMGN_SLUGS = {"SOL": "sol", "BASE": "base", "BSC": "bsc", "HOOD": "robinhood"}


class PotonBot:
    def __init__(self, token: str, channel_id: int, ping_user_id: str):
        intents = discord.Intents.default()
        self.client = discord.Client(intents=intents)
        self.token = token
        self.channel_id = channel_id
        self.ping_user_id = ping_user_id
        self._ready = asyncio.Event()
        self._channel = None

        @self.client.event
        async def on_ready():
            log("DISCORD", f"Bot connected as {self.client.user}")
            try:
                await self.client.change_presence(activity=discord.Game(name="watching wallets 👀"))
            except Exception:
                pass
            self._ready.set()

    async def start(self):
        task = asyncio.create_task(self.client.start(self.token))
        await self._ready.wait()
        self._channel = self.client.get_channel(self.channel_id)
        if self._channel is None:
            self._channel = await self.client.fetch_channel(self.channel_id)
        return task

    async def send_startup(self, n_sol: int, n_evm: int, threshold: int, window_min: int, rpc_ok: bool = True):
        msg = (
            f"🟢 **Poton online** — watching **{n_sol}** SOL wallets"
            + (f" and **{n_evm}** EVM wallets (BASE/BSC/HOOD)" if n_evm else "")
            + f".\nAlert when **{threshold}+** wallets ape the same coin within **{window_min} min**,"
            + f" or **2+** smart wallets with combined score ≥ **1.76** 🧠"
            + f"\nAlerts ping: **@everyone**"
        )
        if not rpc_ok:
            msg += "\n⚠️ **Solana RPC DOWN/invalid — SOL monitor can't work. Fix `SOLANA_RPC_URL`!**"
        try:
            await self._channel.send(msg)
        except Exception as e:
            log("DISCORD", f"Could not send startup message: {e}")

    async def send_rpc_warning(self, rpc_url: str):
        try:
            await self._channel.send(
                f"⚠️ @everyone Solana RPC unreachable: `{rpc_url[:60]}`\n"
                "No buys will be detected until you fix `SOLANA_RPC_URL`. "
                "Helius format: `https://mainnet.helius-rpc.com/?api-key=YOUR_KEY`"
            )
        except Exception as e:
            log("DISCORD", f"Could not send RPC warning: {e}")

    # ---------------- ALERT DASHBOARD ----------------

    async def send_alert(self, chain: str, token: str, count: int, score_sum: float,
                         buys: list, info: dict, extra: dict | None = None):
        extra = extra or {}
        symbol = info.get("symbol") or (buys[-1].get("symbol") if buys else "") or "?"
        name = info.get("name") or symbol
        slug = CHAIN_SLUGS.get(chain, "")
        pair_url = info.get("pair_url") or (EXPLORERS.get(chain, "") + token)

        embed = discord.Embed(
            title=f"🚀💊 {name} - ${symbol}",
            url=pair_url,
            color=CHAIN_COLORS.get(chain, 0xFF0000),
        )
        if info.get("image"):
            embed.set_thumbnail(url=info["image"])
        elif slug:
            # fallback: CDN de Dexscreener sirve el icono por direccion aunque el par no traiga imagen
            embed.set_thumbnail(url=f"https://dd.dexscreener.com/ds-data/tokens/{slug}/{token}.png")

        lines = []

        # --- pump.fun / DEX line ---
        pump = extra.get("pump") or {}
        if chain == "SOL" and pump:
            if pump.get("complete"):
                lines.append(f"🎓 **Migrated** from Pump → {info.get('dex_id', '?')}")
            elif pump.get("progress") is not None:
                lines.append(f"⏳ **{pump['progress']:.0f}%** @ Pump")
            else:
                lines.append("⏳ Bonding curve @ **Pump**")
        elif info.get("dex_id"):
            lines.append(f"📍 DEX: **{info['dex_id']}** ({chain})")

        # --- FDV / MCap ---
        fdv = fmt_usd(info.get("fdv"))
        mcap = fmt_usd(info.get("mcap"))
        if fdv == mcap:
            lines.append(f"💎 FDV: **{fdv}**")
        else:
            lines.append(f"💎 FDV: **{fdv}** · MCap: **{mcap}**")

        # --- Volume / age ---
        vol24 = fmt_usd(info.get("vol24"))
        age = fmt_age(info.get("created_ms"))
        lines.append(f"📊 Vol 24h: **{vol24}** · Age: **{age}**")

        # --- 5M stats ---
        if info.get("vol5") is not None or info.get("buys5") is not None:
            v5 = fmt_usd(info.get("vol5"))
            chg5 = info.get("chg5")
            chg5_txt = f"{float(chg5):+.1f}%" if chg5 is not None else "?"
            b5 = info.get("buys5") or 0
            s5 = info.get("sells5") or 0
            lines.append(f"📈 5M: **{v5}** · **{chg5_txt}** 🟢 {b5} 🔴 {s5}")

        # --- Top holders (SOL) ---
        th = extra.get("holders") or {}
        if th.get("top5"):
            pcts = "·".join(f"{p:.1f}" for _, p in th["top5"])
            lines.append(f"👥 TH: {pcts} **[{th['sum10']:.0f}%]**")

        # --- Dev ---
        if pump.get("creator"):
            lines.append(f"🧑‍💻 DEV: `{pump['creator']}`")

        # --- USD bought per wallet (SOL: exact via SOL spent; EVM: amount x price) ---
        sol_price = extra.get("sol_price")
        token_price = info.get("price_usd")
        total_usd = 0.0
        usd_of = {}
        for b in buys:
            usd = None
            if b.get("usd") is not None:
                usd = float(b["usd"])
            elif chain == "SOL" and b.get("sol_spent") and sol_price:
                usd = float(b["sol_spent"]) * float(sol_price)
            elif token_price and b.get("amount"):
                try:
                    usd = float(b["amount"]) * float(token_price)
                except (TypeError, ValueError):
                    usd = None
            usd_of[b["wallet"]] = usd
            if usd:
                total_usd += usd
        if total_usd > 0:
            lines.append(f"💰 Total aped: **{fmt_usd(total_usd)}**")

        embed.description = "\n".join(lines)

        # --- Wallets in (score + USD) ---
        wl = []
        for b in buys:
            emoji = (b.get("emoji") or "").strip()
            label = f"{emoji} **{b['name']}**" if emoji else f"**{b['name']}**"
            sc = float(b.get("score", 0.3))
            star = "⭐" if sc >= 0.9 else ""
            usd = usd_of.get(b["wallet"])
            spent = f"**${usd:,.0f}**" if usd is not None else f"`{fmt_amount(b.get('amount'))}`"
            txh = b.get("tx_hash") or ""
            if chain == "SOL" and txh:
                wl.append(f"{label} `{sc:.2f}`{star} — {spent} ([tx](https://solscan.io/tx/{txh}))")
            elif txh:
                exp_tx = {"BASE": "https://basescan.org/tx/",
                          "BSC": "https://bscscan.com/tx/",
                          "HOOD": "https://robinhoodchain.blockscout.com/tx/"}.get(chain, "")
                wl.append(f"{label} `{sc:.2f}`{star} — {spent} ([tx]({exp_tx}{txh}))")
            else:
                wl.append(f"{label} `{sc:.2f}`{star} — {spent}")
        wallets_txt = "\n".join(wl)
        if len(wallets_txt) > 1000:
            wallets_txt = wallets_txt[:1000] + "\n..."
        embed.add_field(name=f"👀 {count} WALLETS IN · SCORE {score_sum:.2f}", value=wallets_txt or "?", inline=False)

        # --- CA ---
        embed.add_field(name="CA", value=f"`{token}`", inline=False)

        # --- Links ---
        links = []
        if slug:
            links.append(f"[DEX](https://dexscreener.com/{slug}/{token})")
        gmgn_slug = GMGN_SLUGS.get(chain)
        if chain == "SOL":
            links.append(f"[AXI](https://axiom.trade/meme/{token})")
            links.append(f"[PHO](https://photon-sol.tinyastro.io/en/lp/{token})")
        if gmgn_slug:
            links.append(f"[GMGN](https://gmgn.ai/{gmgn_slug}/token/{token})")
        exp = EXPLORERS.get(chain)
        if exp:
            links.append(f"[EXP]({exp}{token})")
        if info.get("twitter"):
            links.append(f"[X]({info['twitter']})")
        if info.get("web"):
            links.append(f"[Web]({info['web']})")
        if info.get("telegram"):
            links.append(f"[TG]({info['telegram']})")
        if links:
            embed.add_field(name="🔗 Links", value=" · ".join(links), inline=False)

        if chain == "SOL":
            embed.add_field(name="📈 TIP:", value=f"Trade on [Axiom](https://axiom.trade/meme/{token})", inline=False)

        embed.set_footer(text=f"Poton 🚨 · {count} wallets · score {score_sum:.2f} · {chain}")

        # Content: "CA @everyone"
        content = f"{token} @everyone"
        try:
            await self._channel.send(content=content, embed=embed)
            log("ALERT", f"{count} wallets -> ${symbol} ({chain}) {token}")
        except Exception as e:
            log("DISCORD", f"Error sending alert: {e}")
