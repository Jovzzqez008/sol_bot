import asyncio
import json
import base64
import requests
import time
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from solders.pubkey import Pubkey
from solana.rpc.websocket_api import connect
from solders.rpc.config import RpcTransactionLogsFilterMentions

# --- ⚙️ CONFIGURACIÓN GLOBAL ⚙️ ---
HELIUS_RPC_URL = "wss://mainnet.helius-rpc.com/?api-key=ac3de0dc-4108-489d-a8f8-96ab2f0ce341"
BIRDEYE_API_KEY = "c6aed78e1e7a4338a153da1d33ef462f" 
GOPLUS_API_KEY = "wAwCMJerMnNUyMS4pSpnFjwbAjSeuBX2"
TELEGRAM_BOT_TOKEN = "8066398402:AAEmj8mLhqM2yTyi7xO9Uf-GwAE3PTCjx1w"

RAYDIUM_LP_V4 = Pubkey.from_string('675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8')
WATCHLIST_FILE = "watchlist.json"
watchlist = {}

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Funciones de Persistencia ---
def save_watchlist():
    with open(WATCHLIST_FILE, 'w') as f: json.dump(watchlist, f, indent=2)

def load_watchlist():
    global watchlist
    try:
        with open(WATCHLIST_FILE, 'r') as f: watchlist = json.load(f)
        logger.info(f"Watchlist cargada con {len(watchlist)} candidatos.")
    except FileNotFoundError: logger.info("No se encontró watchlist. Empezando de cero.")

# --- Funciones de Alerta y Análisis ---
def enviar_alerta_telegram_sync(mensaje, chat_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": mensaje, "parse_mode": "Markdown", "disable_web_page_preview": True}
    try:
        requests.post(url, json=payload)
        logger.info(f"Alerta enviada al chat {chat_id}.")
    except Exception as e: logger.error(f"Error enviando a Telegram: {e}")

def get_security_report(token_address):
    url = f"https://api.gopluslabs.io/api/v1/token_security/1?contract_addresses={token_address}"
    headers = {"Authorization": f"Bearer {GOPLUS_API_KEY}"}
    try:
        res = requests.get(url, headers=headers); res.raise_for_status()
        data = res.json().get('result', {}).get(token_address.lower())
        if not data: return "❓ Reporte de seguridad no disponible.", False
        is_safe = True; report = []
        if data.get('is_honeypot') == '1': report.append("- 🚨 ¡ALTO RIESGO! Posible Honeypot."); is_safe = False
        else: report.append("- ✅ No parece ser Honeypot.")
        total_lp_locked_pct = sum(float(lp.get('percent', 0)) for lp in data.get('lp_holders', []) if lp.get('is_locked') == 1)
        report.append(f"- 💧 Liquidez Bloqueada: {total_lp_locked_pct*100:.2f}%")
        if total_lp_locked_pct < 0.9: report.append("- 🚩 ALERTA: Menos del 90% de liquidez bloqueada."); is_safe = False
        return "\n".join(report), is_safe
    except Exception: return "❓ Error en GoPlus.", False

async def analizar_candidato_inicial(token_address, chat_id):
    logger.info(f"Fase 1: Analizando candidato {token_address}")
    reporte_seguridad, es_seguro = get_security_report(token_address)
    if not es_seguro: logger.info(f"  - ❌ DESCARTADO (GoPlus): {reporte_seguridad}"); return
    
    birdeye_url = f"https://public-api.birdeye.so/defi/token_overview?address={token_address}"
    headers_birdeye = {"X-API-KEY": BIRDEYE_API_KEY}
    try:
        res = requests.get(birdeye_url, headers=headers_birdeye); res.raise_for_status()
        data = res.json()
        if not data.get("success") or not data.get("data"): logger.info("  - ❌ DESCARTADO (Birdeye): Sin datos de mercado."); return
        token_data = data["data"]
        symbol = token_data.get("symbol", "N/A"); liquidity = token_data.get("liquidity", 0); holders = token_data.get("holders", 0)
        if not (liquidity > 1000 and holders > 50): logger.info(f"  - ❌ DESCARTADO (Birdeye): No cumple liquidez/holders mínimos."); return
        
        logger.info(f"  - ✅ ¡APROBADO! {symbol} añadido a la watchlist.")
        watchlist[token_address] = {'found_at': time.time(), 'symbol': symbol, 'status': 'new', 'initial_liquidity': liquidity, 'initial_holders': holders}
        save_watchlist()
        alerta = (f"🕵️‍♂️ *NUEVO CANDIDATO A VIGILAR*\n\n*{symbol}* ({token_address})\n\nHa pasado los filtros iniciales. Se añade a la watchlist para seguimiento en 24h.\n\n*Reporte de Seguridad:*\n{reporte_seguridad}")
        enviar_alerta_telegram_sync(alerta, chat_id)
    except Exception as e: logger.error(f"  - ❌ Error en análisis de mercado: {e}")

async def analizar_superviviente(token_address, initial_data, hours, chat_id):
    logger.info(f"Fase 2: Analizando superviviente de {hours}h: {initial_data['symbol']}")
    # (Lógica de análisis de superviviente idéntica a la anterior)
    # ...

async def hunter_task(chat_id):
    logger.info("Iniciando tarea del Cazador...")
    while True:
        try:
            async with connect(HELIUS_RPC_URL) as websocket:
                await websocket.logs_subscribe(RpcTransactionLogsFilterMentions(RAYDIUM_LP_V4))
                first_resp = await websocket.recv()
                logger.info(f"Cazador conectado. ID: {first_resp[0].result}")
                async for msg in websocket:
                    for log_message in msg:
                        logs = log_message.result.value.logs
                        for log in logs:
                            if "initialize2" in log:
                                try:
                                    data = base64.b64decode(log.split()[-1])[8:]
                                    token_b = str(Pubkey(data[297:329]))
                                    if token_b and token_b not in watchlist:
                                        await analizar_candidato_inicial(token_b, chat_id)
                                except: continue
        except asyncio.CancelledError: logger.info("Tarea del Cazador detenida."); break
        except Exception as e: logger.error(f"Error en el Cazador: {e}. Reiniciando..."); await asyncio.sleep(30)

async def watcher_task(chat_id):
    logger.info("Iniciando tarea del Vigía...")
    # (Lógica del Vigía idéntica a la anterior, solo que ahora necesita el chat_id)
    # ...

# --- COMANDOS DE TELEGRAM ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 ¡Bienvenido al Bot Cazador PRO!\n\nUsa /cazar para iniciar la búsqueda.\nUsa /parar para detenerla.\nUsa /status para ver el estado.")

async def hunt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    if context.bot_data.get('tasks'): await update.message.reply_text("🤔 El bot ya está cazando."); return
    await update.message.reply_text("🏹 ¡Iniciando la caza! El Cazador y el Vigía han sido desplegados.")
    load_watchlist()
    hunter = asyncio.create_task(hunter_task(chat_id))
    watcher = asyncio.create_task(watcher_task(chat_id))
    context.bot_data['tasks'] = [hunter, watcher]

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.bot_data.get('tasks'): await update.message.reply_text("🤔 El bot no está cazando actualmente."); return
    for task in context.bot_data['tasks']: task.cancel()
    context.bot_data['tasks'] = []
    await update.message.reply_text("🛑 ¡Caza detenida! El Cazador y el Vigía han vuelto a la base.")
    # Esperamos a que las tareas se cancelen de verdad
    await asyncio.sleep(1) 

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.bot_data.get('tasks'):
        status_msg = f"✅ El bot está **Activo**.\n🕵️‍♂️ Hay **{len(watchlist)}** candidatos en la lista de vigilancia."
    else:
        status_msg = f"🛑 El bot está **Detenido**."
    await update.message.reply_text(status_msg)

def main():
    print("--- 🤖 Iniciando Bot de Telegram... ---")
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("cazar", hunt_command))
    application.add_handler(CommandHandler("parar", stop_command))
    application.add_handler(CommandHandler("status", status_command))
    print("--- 🎧 El bot está escuchando a Telegram... ---")
    application.run_polling()

if __name__ == '__main__':
    # Pegando las funciones completas que faltaban
    async def analizar_superviviente(token_address, initial_data, hours, chat_id):
        logger.info(f"Fase 2: Analizando superviviente de {hours}h: {initial_data['symbol']}")
        birdeye_url = f"https://public-api.birdeye.so/defi/token_overview?address={token_address}"
        headers_birdeye = {"X-API-KEY": BIRDEYE_API_KEY}
        try:
            res = requests.get(birdeye_url, headers=headers_birdeye); res.raise_for_status()
            data = res.json()
            if not data.get("success") or not data.get("data"): return
            token_data = data["data"]
            current_liquidity = token_data.get("liquidity", 0); current_holders = token_data.get("holders", 0)
            liquidity_change = ((current_liquidity - initial_data['initial_liquidity']) / initial_data['initial_liquidity']) * 100 if initial_data['initial_liquidity'] > 0 else 0
            holders_change = ((current_holders - initial_data['initial_holders']) / initial_data['initial_holders']) * 100 if initial_data['initial_holders'] > 0 else 0
            if liquidity_change > -50 and holders_change > -10:
                alerta = (f"📈 *REPORTE DE SUPERVIVENCIA ({hours}H)*\n\n*{initial_data['symbol']}* ({token_address})\n\n*Progreso:*\n- Liquidez: `${current_liquidity:,.2f}` ({liquidity_change:+.2f}%)\n- Holders: *{current_holders:,}* ({holders_change:+.2f}%)\n\n[Ver en Birdeye](https://birdeye.so/token/{token_address}?chain=solana)")
                enviar_alerta_telegram_sync(alerta, chat_id)
        except Exception as e: logger.error(f"  - Error analizando superviviente: {e}")
    async def watcher_task(chat_id):
        logger.info("Iniciando tarea del Vigía...")
        while True:
            try:
                await asyncio.sleep(3600)
                logger.info("Vigía despertando para revisar la watchlist...")
                current_time = time.time()
                survivors_to_check = []
                for addr, data in list(watchlist.items()):
                    age_seconds = current_time - data.get('found_at', 0)
                    status = data.get('status', 'new')
                    if status == 'new' and age_seconds > 86400: survivors_to_check.append((addr, data, 24))
                    elif status == 'checked_24h' and age_seconds > 172800: survivors_to_check.append((addr, data, 48))
                if survivors_to_check:
                    logger.info(f"  - {len(survivors_to_check)} superviviente(s) encontrado(s) para análisis.")
                    for addr, data, hours in survivors_to_check:
                        await analizar_superviviente(addr, data, hours, chat_id)
                        watchlist[addr]['status'] = f'checked_{hours}h'
                    save_watchlist()
                else: logger.info("  - Ningún candidato cumple 24/48h todavía.")
            except asyncio.CancelledError: logger.info("Tarea del Vigía detenida."); break
            except Exception as e: logger.error(f"Error en el Vigía: {e}")
    main()
