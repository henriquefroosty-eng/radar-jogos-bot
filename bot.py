import os
import json
import logging
import asyncio
import anthropic
from datetime import datetime
from zoneinfo import ZoneInfo
from telegram import Bot
from telegram.constants import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TIMEZONE = "America/Sao_Paulo"

client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)


def today_str():
    now = datetime.now(ZoneInfo(TIMEZONE))
    return now.strftime("%d/%m/%Y"), now.strftime("%Y-%m-%d"), now.strftime("%A, %d de %B de %Y")


def escape_md(text):
    text = str(text)
    chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for c in chars:
        text = text.replace(c, f'\\{c}')
    return text


def score_match(m):
    s = 0
    total = float(m.get("mg_casa", 0)) + float(m.get("mg_fora", 0))
    if total >= 3.5:
        s += 3
    elif total >= 2.8:
        s += 2
    elif total >= 2.2:
        s += 1
    ov_avg = (float(m.get("ov_casa", 0)) + float(m.get("ov_fora", 0))) / 2
    if ov_avg >= 65:
        s += 3
    elif ov_avg >= 50:
        s += 2
    elif ov_avg >= 35:
        s += 1
    h2h = int(m.get("h2h", 0))
    if h2h >= 4:
        s += 2
    elif h2h >= 3:
        s += 1
    imp = int(m.get("importance", 1))
    if imp == 2:
        s += 1
    elif imp == 0:
        s -= 1
    return max(-1, s)


def recommend_market(m):
    total = float(m.get("mg_casa", 0)) + float(m.get("mg_fora", 0))
    ov_avg = (float(m.get("ov_casa", 0)) + float(m.get("ov_fora", 0))) / 2
    h2h = int(m.get("h2h", 0))
    if total >= 3.0 and ov_avg >= 50:
        principal = "Over 3.5 FT"
    elif total >= 2.2:
        principal = "Over 2.5 FT"
    else:
        principal = "Under 2.5 FT"
    if h2h >= 3 and ov_avg >= 40:
        alternativa = "Over 0.5 HT"
    elif total >= 2.5:
        alternativa = "Over 1.5 HT"
    else:
        alternativa = "Under 1.5 HT"
    return principal, alternativa


def verdict(score):
    if score >= 6:
        return "edge"
    if score >= 4:
        return "razoavel"
    return "pular"


def fetch_matches(today_br, today_iso, region):
    if region == "europe":
        log.info("Buscando jogos europeus...")
        prompt = (
            "Today is " + today_br + " (" + today_iso + "). "
            "Search the web for football matches today " + today_iso + ". "
            "Search for: football fixtures " + today_iso + " Champions League Premier League Bundesliga. "
            "Return a JSON array of matches. Each object must have these exact fields: "
            "home (string), away (string), league (string), time (string HH:MM BRT), "
            "mg_casa (float avg goals home team), mg_fora (float avg goals away team), "
            "ov_casa (int percent home games over 3.5 goals), ov_fora (int percent away games over 3.5 goals), "
            "h2h (int 0-5 how many of last 5 meetings had over 3.5 goals), "
            "importance (int 0=meaningless 1=normal 2=cup or title or relegation), "
            "note (string one sentence about goal potential). "
            "Estimate stats if not found. Return ONLY the JSON array, nothing else."
        )
    else:
        log.info("Buscando jogos brasileiros...")
        prompt = (
            "Today is " + today_br + " (" + today_iso + "). "
            "Search the web for Brazilian football matches today " + today_br + ". "
            "Search for: jogos futebol hoje " + today_br + " Brasileirao Serie B Copa Brasil. "
            "Return a JSON array of matches. Each object must have these exact fields: "
            "home (string), away (string), league (string), time (string HH:MM BRT), "
            "mg_casa (float avg goals home team), mg_fora (float avg goals away team), "
            "ov_casa (int percent home games over 3.5 goals), ov_fora (int percent away games over 3.5 goals), "
            "h2h (int 0-5 how many of last 5 meetings had over 3.5 goals), "
            "importance (int 0=meaningless 1=normal 2=cup or title or relegation), "
            "note (string one sentence about goal potential). "
            "Estimate stats if not found. Return ONLY the JSON array, nothing else."
        )

    msg = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=4000,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        system="You are a football data assistant. Respond with ONLY a valid JSON array starting with [ and ending with ]. No markdown, no explanation, no backticks.",
        messages=[{"role": "user", "content": prompt}]
    )

    raw = "".join(b.text for b in msg.content if b.type == "text")
    log.info("Raw response (%s): %s", region, raw[:300])
    si = raw.find("[")
    ei = raw.rfind("]")
    if si == -1 or ei == -1:
        log.warning("No JSON array found for %s", region)
        return []
    try:
        return json.loads(raw[si:ei + 1])
    except Exception as e:
        log.error("JSON parse error (%s): %s", region, e)
        return []


def format_telegram(matches, today_fmt):
    scored = sorted(
        [{"match": m, "score": score_match(m)} for m in matches],
        key=lambda x: -x["score"]
    )
    edge = [x for x in scored if verdict(x["score"]) == "edge"]
    razo = [x for x in scored if verdict(x["score"]) == "razoavel"]
    pular = [x for x in scored if verdict(x["score"]) == "pular"]

    lines = []
    lines.append("⚽ *Radar de Jogos — " + escape_md(today_fmt) + "*")
    lines.append("📊 " + str(len(matches)) + " jogos · " + str(len(edge)) + " com edge · " + str(len(razo)) + " razoáveis · " + str(len(pular)) + " pular")
    lines.append("")

    if edge:
        lines.append("🟢 *COM EDGE — Prioridade de entrada*")
        for x in edge:
            m = x["match"]
            total = float(m.get("mg_casa", 0)) + float(m.get("mg_fora", 0))
            principal, alternativa = recommend_market(m)
            lines.append("━━━━━━━━━━━━")
            lines.append("🏟 *" + escape_md(str(m.get("home", "?"))) + " × " + escape_md(str(m.get("away", "?"))) + "*")
            lines.append("🏆 " + escape_md(str(m.get("league", ""))) + "  🕐 " + escape_md(str(m.get("time", "?"))) + " BRT")
            lines.append("📈 Score: *" + str(x["score"]) + "/9 pts*")
            lines.append("⚡ Média total: " + escape_md(str(round(total, 1))) + " gols/jogo")
            lines.append("📊 Over 3\\.5%: casa " + str(int(m.get("ov_casa", 0))) + "% · fora " + str(int(m.get("ov_fora", 0))) + "%")
            lines.append("🔄 H2H over 3\\.5: " + str(m.get("h2h", 0)) + "/5 jogos")
            lines.append("🎯 Entrada: *" + escape_md(principal) + "* \\| Alt: " + escape_md(alternativa))
            if m.get("note"):
                lines.append("💡 _" + escape_md(str(m["note"])) + "_")
        lines.append("")

    if razo:
        lines.append("🟡 *RAZOÁVEIS — Confirmar ao vivo*")
        for x in razo:
            m = x["match"]
            principal, _ = recommend_market(m)
            lines.append(
                "• *" + escape_md(str(m.get("home", "?"))) + " × " + escape_md(str(m.get("away", "?"))) + "* \\(" +
                escape_md(str(m.get("league", ""))) + "\\) " + escape_md(str(m.get("time", "?"))) + " BRT — " +
                str(x["score"]) + "pts — " + escape_md(principal)
            )
        lines.append("")

    if pular:
        lines.append("🔴 *PULAR HOJE*")
        for x in pular:
            m = x["match"]
            lines.append(
                "• " + escape_md(str(m.get("home", "?"))) + " × " + escape_md(str(m.get("away", "?"))) +
                " \\(" + escape_md(str(m.get("league", ""))) + "\\) — " + str(x["score"]) + "pts"
            )
        lines.append("")

    lines.append("━━━━━━━━━━━━")
    lines.append("📋 Stake: R$200 · Stop diário: R$200 · Máx 2 entradas/dia")
    lines.append("🚫 Bloqueados: \\+/\\-0,5 e \\+/\\-4,5 gols")
    return "\n".join(lines)


async def send_analysis():
    today_br, today_iso, today_fmt = today_str()
    bot = Bot(token=TELEGRAM_TOKEN)
    log.info("Iniciando análise — %s", today_br)

    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text="⏳ Buscando jogos de *" + escape_md(today_br) + "*\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2
    )

    try:
        matches_eu = fetch_matches(today_br, today_iso, "europe")
        matches_br = fetch_matches(today_br, today_iso, "brazil")
        matches = matches_eu + matches_br
        log.info("Total: %d jogos (%d Europa + %d Brasil)", len(matches), len(matches_eu), len(matches_br))

        if len(matches) == 0:
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text="⚠️ Nenhum jogo encontrado hoje\\. Pode ser dia sem jogos ou erro na busca\\.",
                parse_mode=ParseMode.MARKDOWN_V2
            )
            return

        text = format_telegram(matches, today_fmt)
        if len(text) > 4096:
            mid = text[:4096].rfind("\n")
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text[:mid], parse_mode=ParseMode.MARKDOWN_V2)
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text[mid:], parse_mode=ParseMode.MARKDOWN_V2)
        else:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode=ParseMode.MARKDOWN_V2)

    except Exception as e:
        log.error("Erro: %s", e)
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text="❌ Erro: " + str(e)
        )


async def main():
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(send_analysis, "cron", hour=8, minute=0)
    scheduler.start()
    log.info("Bot iniciado. Enviando análise de teste...")
    await send_analysis()
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
