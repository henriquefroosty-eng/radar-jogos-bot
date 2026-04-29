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


def score_match(m):
    s = 0
    total = float(m.get("mg_casa", 0)) + float(m.get("mg_fora", 0))
    if total >= 3.5: s += 3
    elif total >= 2.8: s += 2
    elif total >= 2.2: s += 1

    ov_avg = (float(m.get("ov_casa", 0)) + float(m.get("ov_fora", 0))) / 2
    if ov_avg >= 65: s += 3
    elif ov_avg >= 50: s += 2
    elif ov_avg >= 35: s += 1

    h2h = int(m.get("h2h", 0))
    if h2h >= 4: s += 2
    elif h2h >= 3: s += 1

    imp = int(m.get("importance", 1))
    if imp == 2: s += 1
    elif imp == 0: s -= 1

    return max(-1, s)


def verdict(score):
    if score >= 6: return "edge"
    if score >= 4: return "razoavel"
    return "pular"


def fetch_matches(today_br, today_iso):
    log.info(f"Buscando jogos de {today_br}...")
    msg = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=4000,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        system="You are a football fixture and statistics analyst. Output ONLY a raw JSON array. No markdown, no backticks, no preamble, no explanation. Just the JSON array starting with [ and ending with ].",
        messages=[{
            "role": "user",
            "content": f"""Search for ALL football matches scheduled for TODAY: {today_br} ({today_iso}).

IMPORTANT: Only include matches actually scheduled for {today_br}. Do NOT include yesterday's or tomorrow's matches.

Search these sources:
- sofascore.com (search: "football {today_iso}" or "futebol {today_br}")  
- flashscore.com
- livescore.com

Focus on these competitions: Premier League, Bundesliga, La Liga, Serie A, Ligue 1, Eredivisie, Champions League, Europa League, Conference League, Brasileirao Serie A, Serie B, Copa do Brasil, MLS, Primeira Liga Portugal, Süper Lig.

For EACH match confirmed for {today_br}, find:
- home: home team name
- away: away team name
- league: competition name
- time: kickoff time in Brazil timezone (BRT, UTC-3) as "HH:MM"
- mg_casa: home team average goals scored per game this season (all competitions)
- mg_fora: away team average goals scored per game this season (all competitions)
- ov_casa: percentage of home team's last 6+ home matches with over 3.5 total goals (0-100)
- ov_fora: percentage of away team's last 6+ away matches with over 3.5 total goals (0-100)
- h2h: number of last 5 head-to-head meetings with over 3.5 total goals (0-5)
- importance: 2=title/relegation/cup knockout, 1=normal game, 0=meaningless
- note: one short sentence about why this match is or isn't interesting for over 3.5 goals

Return ONLY the JSON array with no other text whatsoever:
[{{"home":"Team A","away":"Team B","league":"Premier League","time":"16:00","mg_casa":2.1,"mg_fora":1.8,"ov_casa":67,"ov_fora":50,"h2h":3,"importance":1,"note":"Both teams average over 2 goals per game this season."}}]"""
        }]
    )
    raw = "".join(b.text for b in msg.content if b.type == "text")
    si, ei = raw.find("["), raw.rfind("]")
    if si == -1 or ei == -1:
        raise ValueError("Nenhum jogo retornado pela API")
    return json.loads(raw[si:ei+1])


def format_telegram(matches, today_fmt):
    scored = sorted(
        [{"match": m, "score": score_match(m)} for m in matches],
        key=lambda x: -x["score"]
    )

    edge   = [x for x in scored if verdict(x["score"]) == "edge"]
    razo   = [x for x in scored if verdict(x["score"]) == "razoavel"]
    pular  = [x for x in scored if verdict(x["score"]) == "pular"]

    lines = []
    lines.append(f"⚽ *Radar de Jogos — {today_fmt}*")
    lines.append(f"📊 {len(matches)} jogos · {len(edge)} com edge · {len(razo)} razoáveis · {len(pular)} pular")
    lines.append("")

    if edge:
        lines.append("🟢 *COM EDGE — Prioridade de entrada*")
        for x in edge:
            m = x["match"]
            total = float(m.get("mg_casa",0)) + float(m.get("mg_fora",0))
            lines.append(f"━━━━━━━━━━━━")
            lines.append(f"🏟 *{m.get('home','?')} × {m.get('away','?')}*")
            lines.append(f"🏆 {m.get('league','')}  🕐 {m.get('time','?')} BRT")
            lines.append(f"📈 Score: *{x['score']}/9 pts*")
            lines.append(f"⚡ Média total: {total:.1f} gols/jogo")
            lines.append(f"📊 Over 3.5%: casa {int(m.get('ov_casa',0))}% · fora {int(m.get('ov_fora',0))}%")
            lines.append(f"🔄 H2H over 3.5: {m.get('h2h',0)}/5 jogos")
            if m.get("note"):
                lines.append(f"💡 _{m['note']}_")
        lines.append("")

    if razo:
        lines.append("🟡 *RAZOÁVEIS — Confirmar ao vivo*")
        for x in razo:
            m = x["match"]
            lines.append(f"• *{m.get('home','?')} × {m.get('away','?')}* ({m.get('league','')}) {m.get('time','?')} BRT — {x['score']}pts")
        lines.append("")

    if pular:
        lines.append("🔴 *PULAR HOJE*")
        for x in pular:
            m = x["match"]
            lines.append(f"• {m.get('home','?')} × {m.get('away','?')} ({m.get('league','')}) — {x['score']}pts")
        lines.append("")

    lines.append("━━━━━━━━━━━━")
    lines.append("📋 Stake: R$200 · Stop diário: R$200 · Máx 2 entradas/dia")
    lines.append("🚫 Bloqueados: \\+/\\-0,5 e \\+/\\-4,5 gols")

    return "\n".join(lines)


async def send_analysis():
    today_br, today_iso, today_fmt = today_str()
    bot = Bot(token=TELEGRAM_TOKEN)
    log.info(f"Iniciando análise diária — {today_br}")

    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=f"⏳ Buscando e analisando os jogos de *{today_br}*\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2
    )

    try:
        matches = fetch_matches(today_br, today_iso)
        log.info(f"{len(matches)} jogos encontrados")
        text = format_telegram(matches, today_fmt)

        # Telegram tem limite de 4096 chars por mensagem
        if len(text) > 4096:
            mid = text[:4096].rfind("\n")
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text[:mid], parse_mode=ParseMode.MARKDOWN_V2)
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text[mid:], parse_mode=ParseMode.MARKDOWN_V2)
        else:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode=ParseMode.MARKDOWN_V2)

        log.info("Análise enviada com sucesso")

    except Exception as e:
        log.error(f"Erro: {e}")
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"❌ Erro ao buscar jogos: {e}\n\nTentando novamente em 30 minutos...",
        )
        await asyncio.sleep(1800)
        await send_analysis()


async def main():
    log.info("Bot iniciado. Aguardando horário agendado (08:00 BRT)...")

    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(send_analysis, "cron", hour=8, minute=0)
    scheduler.start()

    # Manda análise imediata ao iniciar (pra testar)
    log.info("Enviando análise inicial de teste...")
    await send_analysis()

    # Mantém rodando
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
