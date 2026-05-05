import asyncio
import aiohttp
import logging
from datetime import datetime, timezone
from telegram import Bot
from telegram.error import TelegramError
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ============================================================
# CONFIGURATION
# ============================================================
BOT_TOKEN = "8653278785:AAFGMfORho-hmypahP2ZrV3smwPcFFFPgsg"
CHANNEL_ID = "@SolEarlyAvengers"

# Signal filters
MIN_LIQUIDITY_USD = 10_000
MIN_VOLUME_1H_USD = 5_000
MIN_PRICE_CHANGE_1H = 10.0

# Scheduling
SCAN_INTERVAL_MINUTES = 10
SUMMARY_INTERVAL_HOURS = 6

# ============================================================
# GLOBALS
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

posted_tokens = set()
signal_log = []


# ============================================================
# DEXSCREENER API
# ============================================================
async def fetch_trending_solana_tokens():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.dexscreener.com/token-boosts/top/v1",
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return [t for t in data if t.get("chainId") == "solana"]
    except Exception as e:
        logger.error(f"Error fetching tokens: {e}")
        return []


async def fetch_pair_details(token_address: str):
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                pairs = data.get("pairs", [])
                if not pairs:
                    return None
                return max(pairs, key=lambda p: p.get("liquidity", {}).get("usd", 0))
    except Exception as e:
        logger.error(f"Error fetching pair: {e}")
        return None


# ============================================================
# SIGNAL FORMATTING
# ============================================================
def format_signal(pair: dict, token_address: str) -> str:
    base = pair.get("baseToken", {})
    name = base.get("name", "Unknown")
    symbol = base.get("symbol", "???")
    price_usd = pair.get("priceUsd", "N/A")
    liquidity = pair.get("liquidity", {}).get("usd", 0)
    volume_1h = pair.get("volume", {}).get("h1", 0)
    volume_24h = pair.get("volume", {}).get("h24", 0)
    price_change_1h = pair.get("priceChange", {}).get("h1", 0)
    price_change_24h = pair.get("priceChange", {}).get("h24", 0)
    dex = pair.get("dexId", "Unknown").capitalize()
    pair_url = pair.get("url", "")

    if price_change_1h >= 50:
        trend_emoji = "🚀🚀🚀"
    elif price_change_1h >= 20:
        trend_emoji = "🚀🚀"
    elif price_change_1h >= 10:
        trend_emoji = "🚀"
    else:
        trend_emoji = "📈"

    now = datetime.now(timezone.utc).strftime("%H:%M UTC")

    return (
        f"⚡ *SOL EARLY AVENGERS SIGNAL* ⚡\n"
        f"{trend_emoji} *{name} (${symbol})*\n\n"
        f"💰 *Price:* ${float(price_usd):.8f}\n"
        f"📊 *1H Change:* {price_change_1h:+.2f}%\n"
        f"📊 *24H Change:* {price_change_24h:+.2f}%\n"
        f"💧 *Liquidity:* ${liquidity:,.0f}\n"
        f"📦 *Volume 1H:* ${volume_1h:,.0f}\n"
        f"📦 *Volume 24H:* ${volume_24h:,.0f}\n"
        f"🏦 *DEX:* {dex}\n\n"
        f"🔗 [View Chart]({pair_url})\n"
        f"`{token_address}`\n\n"
        f"🕐 {now}\n"
        f"#Solana #SolEarlyAvengers #{symbol}"
    )


def format_summary() -> str:
    if not signal_log:
        return "📋 *SOL EARLY AVENGERS — SUMMARY*\n\nNo signals posted in this period."

    now = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")
    lines = [
        f"📋 *SOL EARLY AVENGERS — {SUMMARY_INTERVAL_HOURS}H SUMMARY*\n",
        f"🕐 _{now}_\n",
        f"Total signals: *{len(signal_log)}*\n",
        "━━━━━━━━━━━━━━━━━━━━\n"
    ]

    for i, s in enumerate(signal_log, 1):
        change = s.get("price_change_1h", 0)
        emoji = "🟢" if change >= 0 else "🔴"
        lines.append(
            f"{i}. {emoji} *${s['symbol']}* — {change:+.2f}% (1H) | "
            f"Liq: ${s['liquidity']:,.0f}\n"
        )

    lines.append("\n━━━━━━━━━━━━━━━━━━━━")
    lines.append("\n⚡ _Sol Early Avengers — Assemble!_ ⚡")
    return "".join(lines)


# ============================================================
# TELEGRAM POSTING
# ============================================================
async def post_signal(bot: Bot, message: str):
    try:
        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=message,
            parse_mode="Markdown",
            disable_web_page_preview=False
        )
        logger.info("Signal posted.")
    except TelegramError as e:
        logger.error(f"Telegram error: {e}")


async def post_summary(bot: Bot):
    message = format_summary()
    try:
        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=message,
            parse_mode="Markdown"
        )
        logger.info("Summary posted.")
        signal_log.clear()
    except TelegramError as e:
        logger.error(f"Telegram error: {e}")


# ============================================================
# MAIN SCAN JOB
# ============================================================
async def scan_and_post(bot: Bot):
    logger.info("Scanning Dexscreener...")
    tokens = await fetch_trending_solana_tokens()
    if not tokens:
        return

    new_signals = 0
    for token in tokens[:20]:
        address = token.get("tokenAddress")
        if not address or address in posted_tokens:
            continue

        pair = await fetch_pair_details(address)
        if not pair:
            continue

        liquidity = pair.get("liquidity", {}).get("usd", 0)
        volume_1h = pair.get("volume", {}).get("h1", 0)
        price_change_1h = pair.get("priceChange", {}).get("h1", 0)

        if liquidity < MIN_LIQUIDITY_USD:
            continue
        if volume_1h < MIN_VOLUME_1H_USD:
            continue
        if price_change_1h < MIN_PRICE_CHANGE_1H:
            continue

        message = format_signal(pair, address)
        await post_signal(bot, message)

        symbol = pair.get("baseToken", {}).get("symbol", "???")
        signal_log.append({
            "symbol": symbol,
            "address": address,
            "price_change_1h": price_change_1h,
            "liquidity": liquidity,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })

        posted_tokens.add(address)
        new_signals += 1
        await asyncio.sleep(1)

    logger.info(f"Done. {new_signals} new signals posted.")


# ============================================================
# ENTRY POINT
# ============================================================
async def main():
    bot = Bot(token=BOT_TOKEN)
    me = await bot.get_me()
    logger.info(f"Bot connected: @{me.username}")

    await bot.send_message(
        chat_id=CHANNEL_ID,
        text=(
            "⚡ *SOL EARLY AVENGERS IS LIVE* ⚡\n\n"
            "🦸 Avengers, assemble! Your Solana early signal bot is now active.\n"
            f"🔍 Scanning every {SCAN_INTERVAL_MINUTES} minutes\n"
            f"📋 Summary every {SUMMARY_INTERVAL_HOURS} hours\n\n"
            "_Early signals. Real alpha. Sol Early Avengers._"
        ),
        parse_mode="Markdown"
    )

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(scan_and_post, "interval", minutes=SCAN_INTERVAL_MINUTES, args=[bot])
    scheduler.add_job(post_summary, "interval", hours=SUMMARY_INTERVAL_HOURS, args=[bot])
    scheduler.start()

    await scan_and_post(bot)

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
