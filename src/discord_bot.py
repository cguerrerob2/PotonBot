import asyncio

import discord

from .dexscreener import (
    EXPLORERS, CHAIN_SLUGS, fetch_sol_price, fetch_token_info,
    fmt_usd, fmt_amount, fmt_age, fmt_price,
)
from .logutil import log

RICK_GREEN = 0xB5E36B  # the lime bar of Rick's cards
CHAIN_NAMES = {"SOL": "Solana", "HOOD": "Robinhood", "BASE": "Base", "BSC": "BSC"}
CHAIN_ICONS = {"SOL": "🟣", "HOOD": "🟢", "BASE": "🔵", "BSC": "🟡"}
CHAIN_QUOTES = {"SOL": "SOL", "HOOD": "ETH", "BASE": "ETH", "BSC": "BNB"}
DEX_NAMES = {
    "pumpfun": "Pump 🔥", "pumpswap": "PumpSwap", "raydium": "Raydium", "launchlab": "LaunchLab",
    "meteora": "Meteora", "meteoradbc": "Meteora DBC", "orca": "Orca", "moonshot": "Moonshot",
    "uniswap": "Uniswap", "aerodrome": "Aerodrome", "pancakeswap": "PancakeSwap",
}
TX_EXPLORERS = {
    "SOL": "https://solscan.io/tx/",
    "BASE": "https://basescan.org/tx/",
    "BSC": "https://bscscan.com/tx/",
    "HOOD": "https://robinhoodchain.blockscout.com/tx/",
}

GMGN_SLUGS = {"SOL": "sol", "BASE": "base", "BSC": "bsc", "HOOD": "robinhood"}


def _short(value) -> str:
    """Rick-style compact USD: 190K, 21.5K, 1.54M."""
    out = fmt_usd(value)
    return out[:-3] + out[-1] if out[-3:-1] == ".0" and out[-1] in "KMB" else out


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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

    async def send_startup(self, n_sol: int, n_evm: int, threshold: int,
                           score_threshold: float, window_min: int, rpc_ok: bool = True):
        msg = (
            f"🟢 **Poton online** — watching **{n_sol}** SOL wallets"
            + (f" and **{n_evm}** EVM wallets (BASE/BSC/HOOD)" if n_evm else "")
            + f".\nAlert when **{threshold}+** wallets ape the same coin within **{window_min} min**,"
            + f" or **2+** smart traders with combined score ≥ **{score_threshold:g}** 🧠"
            + f"\n⚡ Realtime (WebSocket). Pings **@everyone only on buy alerts**."
        )
        if not rpc_ok:
            msg += "\n⚠️ **Solana RPC DOWN/invalid — SOL monitor can't work. Fix `SOLANA_RPC_URL`!**"
        try:
            await self._channel.send(msg, allowed_mentions=discord.AllowedMentions.none())
        except Exception as e:
            log("DISCORD", f"Could not send startup message: {e}")

    async def send_rpc_warning(self, rpc_url: str):
        try:
            await self._channel.send(
                f"⚠️ Solana RPC unreachable: `{rpc_url[:60]}`\n"
                "No buys will be detected until you fix `SOLANA_RPC_URL`. "
                "Helius format: `https://mainnet.helius-rpc.com/?api-key=YOUR_KEY`"
            )
        except Exception as e:
            log("DISCORD", f"Could not send RPC warning: {e}")

    # ---------------- ALERT CARD (Rick-style) ----------------

    @staticmethod
    def build_header(chain: str, token: str, info: dict, buys: list) -> str:
        """First line of the message, like Rick: 💊 Name [FDV/1h%] - SYMBOL/SOL"""
        symbol = info.get("symbol") or (buys[-1].get("symbol") if buys else "") or "?"
        name = info.get("name") or symbol
        chg1h = _num(info.get("chg1h"))
        chg = f"{chg1h:+.1f}%".replace("+", "") if chg1h is not None else "?"
        quote = CHAIN_QUOTES.get(chain, chain)
        return f"💊 **{name} [{_short(info.get('fdv'))}/{chg}] - {symbol}/{quote}**"

    def build_embed(self, chain: str, token: str, count: int, score_sum: float,
                    buys: list, info: dict, extra: dict | None = None):
        extra = extra or {}
        slug = CHAIN_SLUGS.get(chain, "")
        pump = extra.get("pump") or {}
        lines = []

        # --- Chain @ venue ---
        dex_id = (info.get("dex_id") or "").lower()
        if chain == "SOL" and (pump or dex_id == "pumpfun"):
            if pump.get("complete"):
                venue = f"{DEX_NAMES.get(dex_id, dex_id.title() or 'PumpSwap')} 🎓"
            elif pump.get("progress") is not None:
                venue = f"Pump 🔥 `{pump['progress']:.0f}%`"
            else:
                venue = "Pump 🔥"
        else:
            venue = DEX_NAMES.get(dex_id, dex_id.title() or "?")
        lines.append(f"{CHAIN_ICONS.get(chain, '🔗')} {CHAIN_NAMES.get(chain, chain)} @ {venue}")

        # --- Price / FDV / Liq ---
        lines.append(f"💰 USD: `{fmt_price(info.get('price_usd'))}`")
        fdv, mcap = _short(info.get("fdv")), _short(info.get("mcap"))
        lines.append(f"💎 FDV: `{fdv}`" + (f" · MC: `{mcap}`" if mcap not in (fdv, "?") else ""))
        liq, fdv_n = _num(info.get("liquidity")), _num(info.get("fdv"))
        ratio = f" `[x{fdv_n / liq:.0f}]`" if liq and fdv_n else ""
        lines.append(f"💦 Liq: `{_short(liq)}`{ratio}")

        # --- Volume / age / 1H ---
        lines.append(f"📊 Vol: `{_short(info.get('vol24'))}` · Age: `{fmt_age(info.get('created_ms'))}`")
        chg1h = _num(info.get("chg1h"))
        if chg1h is not None or info.get("buys1h") is not None:
            icon = "💩" if (chg1h or 0) < 0 else "🚀"
            chg = f"{chg1h:+.1f}%" if chg1h is not None else "?"
            lines.append(f"{icon} 1H: `{chg}` 🅑 `{fmt_amount(info.get('buys1h') or 0)}` "
                         f"Ⓢ `{fmt_amount(info.get('sells1h') or 0)}`")

        # --- Holders / dev ---
        th = extra.get("holders") or {}
        if th.get("top5"):
            lines.append("")
            pcts = "·".join(f"[{p:.1f}](https://solscan.io/account/{a})" for a, p in th["top5"])
            lines.append(f"👥 TH: {pcts} `[{th['sum10']:.0f}%]`")
        if pump.get("creator"):
            c = pump["creator"]
            lines.append(f"👨‍💻 Dev: [{c[:4]}…{c[-4:]}](https://solscan.io/account/{c})")

        # --- Chart / More ---
        lines.append("")
        charts = []
        if slug:
            charts.append(f"[DEX](https://dexscreener.com/{slug}/{token})")
        pair = info.get("pair_address")
        if chain == "SOL" and pair:
            charts.append(f"[DEF](https://www.defined.fi/sol/{pair})")
        if charts:
            lines.append("💹 Chart: " + " · ".join(charts))
        more = []
        if chain == "SOL":
            more.append(f"[BM](https://app.bubblemaps.io/sol/token/{token})")
        if info.get("web"):
            more.append(f"[Web]({info['web']})")
        if info.get("twitter"):
            more.append(f"[𝕏]({info['twitter']})")
        if info.get("telegram"):
            more.append(f"[TG]({info['telegram']})")
        if more:
            lines.append("🧰 More: " + " · ".join(more))

        # --- CA + trade links ---
        lines.append("")
        lines.append(f"`{token}`")
        trade = []
        gmgn_slug = GMGN_SLUGS.get(chain)
        if gmgn_slug:
            trade.append(f"[GMG](https://gmgn.ai/{gmgn_slug}/token/{token})")
        if chain == "SOL":
            trade.append(f"[PHO](https://photon-sol.tinyastro.io/en/lp/{token})")
            trade.append(f"[AXI](https://axiom.trade/meme/{token})")
        exp = EXPLORERS.get(chain)
        if exp:
            trade.append(f"[EXP]({exp}{token})")
        trade.append(f"[TW](https://x.com/search?q={token}&f=live)")
        lines.append("·".join(trade))

        # --- Who aped (instead of Rick's "You are first") ---
        sol_price = extra.get("sol_price")
        token_price = info.get("price_usd")
        total_usd = 0.0
        wl = []
        for b in buys:
            usd = None
            if b.get("usd") is not None:
                usd = float(b["usd"])
            elif chain == "SOL" and b.get("sol_spent") and sol_price:
                usd = float(b["sol_spent"]) * float(sol_price)
            elif token_price and b.get("amount"):
                usd = (_num(b["amount"]) or 0) * (_num(token_price) or 0) or None
            if usd:
                total_usd += usd
            emoji = (b.get("emoji") or "").strip()
            label = f"{emoji} **{b['name']}**" if emoji else f"**{b['name']}**"
            if int(b.get("wallets_count", 1)) > 1:
                label += f" ×{int(b['wallets_count'])}"
            spent = f"`${usd:,.0f}`" if usd is not None else f"`{fmt_amount(b.get('amount'))}`"
            txh = b.get("tx_hash") or ""
            tx_url = f"{TX_EXPLORERS.get(chain, '')}{txh}" if txh else ""
            wl.append(f"{label} ⭐`{float(b.get('score', 0.3)):.2f}` {spent}" + (f" [tx]({tx_url})" if tx_url else ""))
        lines.append("")
        aped = f" · `${_short(total_usd)}` aped" if total_usd > 0 else ""
        lines.append(f"👀 **{count} trader{'s' if count != 1 else ''} in** @ `{fdv}`{aped} · score `{score_sum:.2f}`")
        wallets_txt = "\n".join(wl)
        if len(wallets_txt) > 1500:
            wallets_txt = wallets_txt[:1500] + "\n..."
        lines.append(wallets_txt)

        return discord.Embed(description="\n".join(lines), color=RICK_GREEN)

    async def send_alert(self, chain: str, token: str, count: int, score_sum: float,
                         buys: list, info: dict, extra: dict | None = None):
        embed = self.build_embed(chain, token, count, score_sum, buys, info, extra)
        view = DashboardView(self, chain, token, count, score_sum, buys, extra or {})
        # The only message that pings: a buy alert, for everyone in the channel
        content = f"{self.build_header(chain, token, info, buys)} @everyone"
        mentions = discord.AllowedMentions(everyone=True, roles=False, users=False)
        try:
            await self._channel.send(content=content, embed=embed, view=view, allowed_mentions=mentions)
            log("ALERT", f"{count} traders -> ${info.get('symbol') or '?'} ({chain}) {token}")
        except Exception as e:
            log("DISCORD", f"Error sending alert: {e}")


class DashboardView(discord.ui.View):
    """Rick's buttons: ❌ (delete) · 📋 Copy CA · ♻️ Refresh."""

    def __init__(self, bot: PotonBot, chain: str, token: str, count: int,
                 score_sum: float, buys: list, extra: dict):
        super().__init__(timeout=86400)
        self.bot = bot
        self.chain = chain
        self.token = token
        self.count = count
        self.score_sum = score_sum
        self.buys = buys
        self.extra = extra

    @discord.ui.button(style=discord.ButtonStyle.secondary, emoji="❌")
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await interaction.message.delete()
        self.stop()

    @discord.ui.button(label="Copy CA", style=discord.ButtonStyle.secondary, emoji="📋")
    async def copy_ca(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Plain CA (no code block) so a long-press copies it cleanly on mobile
        await interaction.response.send_message(self.token, ephemeral=True)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, emoji="♻️")
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        info = await fetch_token_info(self.chain, self.token)
        extra = dict(self.extra)
        if self.chain == "SOL":
            extra["sol_price"] = await fetch_sol_price()
        embed = self.bot.build_embed(
            self.chain, self.token, self.count, self.score_sum, self.buys, info, extra)
        content = f"{self.bot.build_header(self.chain, self.token, info, self.buys)} @everyone"
        await interaction.edit_original_response(
            content=content, embed=embed, view=self,
            allowed_mentions=discord.AllowedMentions.none())
