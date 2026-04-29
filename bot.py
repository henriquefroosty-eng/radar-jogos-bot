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
    chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for c in chars:
        text = text.replace(c, f'\\{c}')
    return text

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

def recommend_market(m, score):
    total = float(m.get("mg_casa", 0)) + float(m.get("mg_fora", 0))
    ov_avg = (float(m.get("ov_casa", 0)) + float(m.get("ov_fora", 0))) / 2
    h2h = int(m.get("h2h", 0))
    principal = ""
    alternativa = ""
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
    return principal, alternativa

def verdict(score):
    if score >= 6: return "edge"
    if score >= 4: return "razoavel"
    return "pular"

def fetch_matches_europe(today_br, today_iso):
    log.info("Buscando jogos europeus...")
    msg = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=4000,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        system="You are a football data assistant. You MUST respond with ONLY a valid JSON array. Every response must start with [ and end with ]. Never add any text outside the JSON array.",
        messages=[{"role": "user", "content": f"f"Today is {today_br} ({today_iso}). Search the web for 'football fixtures {today_iso}' and 'soccer matches today {today_iso}'. List every football match you find scheduled for {today_iso} in any European competition including Premier League, Bundesliga, La Liga, Serie A, Ligue 1, Champions League, Europa League. For each match create a JSON object. If you cannot find real stats estimate them. Return ONLY a JSON array: [{{\"home\":\"Team A\",\"away\":\"Team B\",\"league\":\"Competition\",\"time\":\"19:00\",\"mg_casa\":1.8,\"mg_fora\":1.5,\"ov_casa\":45,\"ov_fora\":35,\"h2h\":2,\"importance\":1,\"note\":\"Short note here\"}}]"}]
    )
    raw = "".join(b.text for b in msg.content if b.type == "text")
    log.info(f"Europa raw response: {raw[:500]}")
    si, ei = raw.find("["), raw.rfind("]")
    if si == -1 or ei == -1:
        return []
    return json.loads(raw[si:ei+1])

def fetch_matches_brazil(today_br, today_iso):
    log.info("Buscando jogos brasileiros...")
    msg = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=4000,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        system="You are a football data assistant. You MUST respond with ONLY a valid JSON array. Every response must start with [ and end with ]. Never add any text outside the JSON array.",
        messages=[{"role": "user", "content": f"f"Today is {today_br} ({today_iso}). Search the web for 'jogos de futebol hoje {today_br}' and 'futebol brasileiro {today_iso}'. List every Brazilian football match for {today_iso}: Brasileirao, Serie B, Copa do Brasil. For each match create a JSON object with estimated stats. Return ONLY a JSON array: [{{\"home\":\"Time A\",\"away\":\"Time B\",\"league\":\"Brasileirao\",\"time\":\"19:00\",\"mg_casa\":1.5,\"mg_fora\":1.2,\"ov_casa\":30,\"ov_fora\":25,\"h2h\":1,\"importance\":1,\"note\":\"Nota aqui\"}}]"}]
    )
    raw = "".join(b.text for b in msg.content if b.type == "text")
    log.info(f"Brasil raw response: {raw[:500]}")
    si, ei = raw.find("["), raw.rfind("]")
    if si == -1 or ei == -1:
        return []
    return json.loads(raw[si:ei+1])

def format_telegram(matches, today_fmt):
    scored = sorted([{"match": m, "score": score_match(m)} for m in matches], key=lambda x: -x["score"])
    edge = [x for x in scored if verdict(x["score"]) == "edge"]
    razo = [x for x in scored if verdict(x["score"]) == "razoavel"]
    pular = [x for x in scored if verdict(x["score"]) == "pular"]
    lines = []
    lines.append(f"⚽ *Radar de Jogos — {escape_md(today_fmt)}*")
    lines.append(f"📊 {len(matches)} jogos · {len(edge)} com edge · {len(razo)} razoáveis · {len(pular)} pular")
    lines.append("")
    if edge:
        lines.append("🟢 *COM EDGE — Prioridade de entrada*")
        for x in edge:
            m = x["match"]
            total = float(m.get("mg_casa", 0)) + float(m.get("mg_fora", 0))
            principal, alternativa = recommend_market(m, x["score"])
            lines.append("━━━━━━━━━━━━")
            lines.append(f"🏟 *{escape_md(m.get('home','?'))} × {escape_md(m.get('away','?'))}*")
            lines.append(f"🏆 {escape_md(m.get('league',''))}  🕐 {escape_md(m.get('time','?'))} BRT")
            lines.append(f"📈 Score: *{x['score']}/9 pts*")
            lines.append(f"⚡ Média total: {escape_md(str(round(total,1)))} gols/jogo")
            lines.append(f"📊 Over 3\\.5%: casa {int(m.get('ov_casa',0))}% · fora {int(m.get('ov_fora',0))}%")
            lines.append(f"🔄 H2H over 3\\.5: {m.get('h2h',0)}/5 jogos")
            lines.append(f"🎯 Entrada: *{escape_md(principal)}* \\| Alt: {escape_md(alternativa)}")
            if m.get("note"):
                lines.append(f"💡 _{escape_md(m['note'])}_")
        lines.append("")
    if razo:
        lines.append("🟡 *RAZOÁVEIS — Confirmar ao vivo*")
        for x in razo:
            m = x["match"]
            principal, _ = recommend_market(m, x["score"])
            lines.append(f"• *{escape_md(m.get('home','?'))} × {escape_md(m.get('away','?'))}* \\({escape_md(m.get('league',''))}\\) {escape_md(m.get('time','?'))} BRT — {x['score']}pts — {escape_md(principal)}")
        lines.append("")
    if pular:
        lines.append("🔴 *PULAR HOJE*")
        for x in pular:
            m = x["match"]
            lines.append(f"• {escape_md(m.get('home','?'))} × {escape_md(m.get('away','?'))} \\({escape_md(m.get('league',''))}\\) — {x['score']}pts")
        lines.append("")
    lines.append("━━━━━━━━━━━━")
    lines.append("📋 Stake: R$200 · Stop diário: R$200 · Máx 2 entradas/dia")
    lines.append("🚫 Bloqueados: \\+/\\-0,5 e \\+/\\-4,5 gols")
    return "\n".join(lines)

async def send_analysis():
    today_br, today_iso, today_fmt = today_str()
    bot = Bot(token=TELEGRAM_TOKEN)
    log.info(f"Iniciando análise — {today_br}")
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"⏳ Buscando jogos de *{escape_md(today_br)}*\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)
    try:
        matches_eu = fetch_matches_europe(today_br, today_iso)
        matches_br = fetch_matches_brazil(today_br, today_iso)
        matches = matches_eu + matches_br
        log.info(f"Total: {len(matches)} jogos ({len(matches_eu)} Europa + {len(matches_br)} Brasil)")
        if len(matches) == 0:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text="⚠️ Nenhum jogo encontrado hoje. Pode ser dia sem jogos ou erro na busca.")
            return
        text = format_telegram(matches, today_fmt)
        if len(text) > 4096:
            mid = text[:4096].rfind("\n")
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text[:mid], parse_mode=ParseMode.MARKDOWN_V2)
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text[mid:], parse_mode=ParseMode.MARKDOWN_V2)
        else:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        log.error(f"Erro: {e}")
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"❌ Erro: {str(e)}")

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
