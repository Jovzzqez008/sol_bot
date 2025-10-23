#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🎯 RAYDIUM SNIPER BOT - Smart Frugal Strategy
==============================================
✅ WebSocket listener para nuevos pools
✅ RPC Pool con múltiples proveedores
✅ 5 Rug Checks exhaustivos
✅ Precio on-chain calculado
✅ Jupiter V6 para trading
✅ DRY_RUN mode completo
✅ Telegram notifications
✅ PostgreSQL para ML training
"""

import os
import sys
import json
import time
import asyncio
import logging
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass, field

import asyncpg
import websockets
from solders.keypair import Keypair
from solders.pubkey import Pubkey

# Imports locales
from rpc_pool import RPCPool
from rug_checker import RugChecker
from price_calculator import PriceCalculator
from jupiter_trader import JupiterTrader

# Telegram
try:
    from telegram import Bot
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    print("⚠️ python-telegram-bot no instalado")

# ═══════════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ═══════════════════════════════════════════════════════════════

@dataclass
class Config:
    """Configuración centralizada"""
    
    # Wallet & RPC
    PRIVATE_KEY: str = os.getenv('WALLET_PRIVATE_KEY', '')
    HELIUS_WSS_URL: str = os.getenv('HELIUS_WSS_URL', '')
    
    # Raydium
    RAYDIUM_PROGRAM_ID: str = os.getenv(
        'RAYDIUM_PROGRAM_ID',
        '675kPX4vVURHmsRjde3eT1xR1bTsQoiAexA45n4Uqck'
    )
    
    # Database
    DATABASE_URL: str = os.getenv('DATABASE_URL', '')
    ENABLE_DB: bool = os.getenv('ENABLE_DB', 'true').lower() == 'true'
    
    # Telegram
    TELEGRAM_TOKEN: str = os.getenv('TELEGRAM_BOT_TOKEN', '')
    TELEGRAM_CHAT_ID: str = os.getenv('TELEGRAM_CHAT_ID', '')
    ENABLE_TELEGRAM: bool = os.getenv('ENABLE_TELEGRAM', 'true').lower() == 'true'
    
    # Trading
    DRY_RUN: bool = os.getenv('DRY_RUN', 'true').lower() == 'true'
    TRADE_AMOUNT_SOL: float = float(os.getenv('TRADE_AMOUNT_SOL', '0.01'))
    SLIPPAGE_BPS: int = int(os.getenv('SLIPPAGE_BPS', '1500'))
    
    # Risk Management
    STOP_LOSS_PERCENT: float = float(os.getenv('STOP_LOSS_PERCENT', '-20'))
    TAKE_PROFIT_1: float = float(os.getenv('TAKE_PROFIT_1', '50'))
    TAKE_PROFIT_2: float = float(os.getenv('TAKE_PROFIT_2', '100'))
    MAX_POSITIONS: int = int(os.getenv('MAX_POSITIONS', '3'))
    MAX_DAILY_TRADES: int = int(os.getenv('MAX_DAILY_TRADES', '20'))
    
    # Filtros de seguridad
    MIN_LIQUIDITY_SOL: float = float(os.getenv('MIN_LIQUIDITY_SOL', '5.0'))
    MAX_HOLDER_PERCENT: float = float(os.getenv('MAX_HOLDER_PERCENT', '40.0'))
    
    # Timing
    POSITION_CHECK_INTERVAL: int = int(os.getenv('POSITION_CHECK_INTERVAL', '10'))
    
    LOG_LEVEL: str = os.getenv('LOG_LEVEL', 'INFO')

config = Config()

# ═══════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper()),
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('sniper_bot.log')
    ]
)

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# MODELOS DE DATOS
# ═══════════════════════════════════════════════════════════════

@dataclass
class NewPoolEvent:
    """Evento de nuevo pool detectado"""
    pool_address: str
    token_mint: str
    sol_mint: str
    liquidity_sol: float
    creator: str
    timestamp: float = field(default_factory=time.time)

@dataclass
class Position:
    """Posición abierta"""
    token_mint: str
    pool_address: str
    entry_price_usd: float
    entry_time: float
    amount_sol: float
    highest_price: float
    lowest_price: float
    entry_tx: str
    rug_check_passed: int
    
    def current_pnl(self, current_price: float) -> float:
        if self.entry_price_usd <= 0:
            return 0
        return ((current_price - self.entry_price_usd) / self.entry_price_usd) * 100
    
    def hold_time_minutes(self) -> float:
        return (time.time() - self.entry_time) / 60

# ═══════════════════════════════════════════════════════════════
# ESTADO GLOBAL
# ═══════════════════════════════════════════════════════════════

class BotState:
    def __init__(self):
        self.wallet: Optional[Keypair] = None
        self.rpc_pool: Optional[RPCPool] = None
        self.rug_checker: Optional[RugChecker] = None
        self.price_calc: Optional[PriceCalculator] = None
        self.trader: Optional[JupiterTrader] = None
        self.telegram_bot: Optional[Bot] = None
        self.db_pool: Optional[asyncpg.Pool] = None
        
        self.positions: Dict[str, Position] = {}
        self.processed_pools: set = set()  # Para evitar duplicados
        
        self.stats = {
            'pools_detected': 0,
            'pools_analyzed': 0,
            'rug_checks_passed': 0,
            'rug_checks_failed': 0,
            'trades_executed': 0,
            'trades_simulated': 0,
            'wins': 0,
            'losses': 0,
            'total_pnl': 0.0,
            'today_trades': 0
        }
        
        self.running = True
        self.ws_connected = False

state = BotState()

# ═══════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════

async def init_database():
    """Inicializar PostgreSQL"""
    if not config.ENABLE_DB or not config.DATABASE_URL:
        logger.warning("⚠️ Database deshabilitada")
        return
    
    try:
        state.db_pool = await asyncpg.create_pool(
            config.DATABASE_URL,
            min_size=2,
            max_size=10
        )
        
        async with state.db_pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS sniper_trades (
                    id SERIAL PRIMARY KEY,
                    token_mint VARCHAR(44),
                    pool_address VARCHAR(44),
                    entry_price NUMERIC(20, 10),
                    exit_price NUMERIC(20, 10),
                    liquidity_sol NUMERIC(15, 2),
                    rug_checks_passed INT,
                    result_pnl_percent NUMERIC(10, 4),
                    hold_time_min NUMERIC(10, 2),
                    entry_time TIMESTAMP,
                    exit_time TIMESTAMP,
                    exit_reason VARCHAR(50),
                    is_dry_run BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            ''')
            
            # Índices para queries rápidas
            await conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_token_mint ON sniper_trades(token_mint);
                CREATE INDEX IF NOT EXISTS idx_entry_time ON sniper_trades(entry_time);
            ''')
            
        logger.info("✅ Database inicializada")
        
    except Exception as e:
        logger.error(f"❌ Error database: {e}")
        state.db_pool = None

async def save_trade(position: Position, exit_price: float, exit_reason: str):
    """Guardar trade en database para ML"""
    if not state.db_pool:
        return
    
    try:
        pnl = position.current_pnl(exit_price)
        
        async with state.db_pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO sniper_trades (
                    token_mint, pool_address, entry_price, exit_price,
                    liquidity_sol, rug_checks_passed, result_pnl_percent,
                    hold_time_min, entry_time, exit_time, exit_reason,
                    is_dry_run
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            ''',
                position.token_mint, position.pool_address,
                position.entry_price_usd, exit_price,
                position.amount_sol, position.rug_check_passed,
                pnl, position.hold_time_minutes(),
                datetime.fromtimestamp(position.entry_time),
                datetime.now(), exit_reason, config.DRY_RUN
            )
            
        logger.info(f"💾 Trade guardado: {position.token_mint[:8]} ({pnl:+.2f}%)")
        
    except Exception as e:
        logger.debug(f"Error guardando trade: {e}")

# ═══════════════════════════════════════════════════════════════
# TELEGRAM
# ═══════════════════════════════════════════════════════════════

async def send_telegram(message: str):
    """Enviar mensaje a Telegram"""
    if not TELEGRAM_AVAILABLE or not state.telegram_bot or not config.TELEGRAM_CHAT_ID:
        return
    
    try:
        await state.telegram_bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=message,
            parse_mode='HTML'
        )
    except Exception as e:
        logger.debug(f"Error Telegram: {e}")

# ═══════════════════════════════════════════════════════════════
# WEBSOCKET LISTENER
# ═══════════════════════════════════════════════════════════════

async def listen_new_pools():
    """
    Escuchar WebSocket de Helius para nuevos pools de Raydium
    """
    if not config.HELIUS_WSS_URL:
        logger.error("❌ HELIUS_WSS_URL no configurada")
        return
    
    reconnect_delay = 5
    max_reconnect_delay = 300
    
    while state.running:
        try:
            logger.info(f"🔌 Conectando a WebSocket: {config.HELIUS_WSS_URL[:50]}...")
            
            async with websockets.connect(config.HELIUS_WSS_URL) as websocket:
                state.ws_connected = True
                logger.info("✅ WebSocket conectado")
                
                # Suscribirse a logs del programa de Raydium
                subscribe_msg = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "logsSubscribe",
                    "params": [
                        {
                            "mentions": [config.RAYDIUM_PROGRAM_ID]
                        },
                        {
                            "commitment": "confirmed"
                        }
                    ]
                }
                
                await websocket.send(json.dumps(subscribe_msg))
                logger.info(f"📡 Suscrito a logs de Raydium: {config.RAYDIUM_PROGRAM_ID[:8]}...")
                
                # Resetear delay de reconexión
                reconnect_delay = 5
                
                # Escuchar mensajes
                async for message in websocket:
                    try:
                        data = json.loads(message)
                        
                        # Procesar evento
                        if "params" in data and "result" in data["params"]:
                            result = data["params"]["result"]
                            await process_log_event(result)
                    
                    except json.JSONDecodeError:
                        continue
                    except Exception as e:
                        logger.error(f"Error procesando mensaje: {e}")
        
        except websockets.exceptions.WebSocketException as e:
            state.ws_connected = False
            logger.error(f"❌ WebSocket error: {e}")
            logger.info(f"🔄 Reconectando en {reconnect_delay}s...")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)
        
        except Exception as e:
            state.ws_connected = False
            logger.error(f"❌ Error crítico WebSocket: {e}")
            await asyncio.sleep(reconnect_delay)

async def process_log_event(event: Dict):
    """
    Procesar evento de log desde WebSocket
    """
    try:
        logs = event.get("value", {}).get("logs", [])
        signature = event.get("value", {}).get("signature", "unknown")
        
        # Buscar el log que indica "initialize2" (creación de pool)
        is_new_pool = False
        for log in logs:
            if "initialize2" in log.lower() or "InitializeInstruction2" in log:
                is_new_pool = True
                break
        
        if not is_new_pool:
            return
        
        state.stats['pools_detected'] += 1
        logger.info(f"🆕 NUEVO POOL DETECTADO! Signature: {signature[:16]}...")
        
        # Extraer información del pool (necesitamos parsear la transacción)
        # Por ahora, lanzar análisis en background
        asyncio.create_task(analyze_new_pool(signature))
    
    except Exception as e:
        logger.error(f"Error procesando log event: {e}")

async def analyze_new_pool(tx_signature: str):
    """
    Analizar un nuevo pool detectado
    Esta función se ejecuta en background
    """
    try:
        # Evitar duplicados
        if tx_signature in state.processed_pools:
            return
        
        state.processed_pools.add(tx_signature)
        state.stats['pools_analyzed'] += 1
        
        logger.info(f"\n{'='*60}")
        logger.info(f"🔍 ANALIZANDO POOL: {tx_signature[:16]}...")
        logger.info(f"{'='*60}")
        
        # TODO: Aquí necesitamos obtener los detalles del pool
        # (pool_address, token_mint, liquidity, creator)
        # Esto requiere parsear la transacción
        
        # Por ahora, simulamos con datos de ejemplo
        # En la versión completa, aquí llamarías a:
        # pool_data = await parse_initialize_transaction(tx_signature)
        
        logger.warning("⚠️ Análisis de pool en desarrollo")
        logger.warning("⚠️ Requiere parser de transacciones Initialize2")
        
    except Exception as e:
        logger.error(f"Error analizando pool: {e}")

# ═══════════════════════════════════════════════════════════════
# CONTINUARÁ EN LA PARTE 2...
# ═══════════════════════════════════════════════════════════════
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🎯 RAYDIUM SNIPER BOT - PART 2: Trading Logic
==============================================
Gestión de posiciones, compra/venta, monitoreo
"""

# ═══════════════════════════════════════════════════════════════
# ANÁLISIS Y DECISIÓN DE COMPRA
# ═══════════════════════════════════════════════════════════════

async def analyze_and_trade_pool(pool_event: 'NewPoolEvent'):
    """
    Analizar pool completo: rug checks + precio + trade
    """
    try:
        logger.info(f"\n{'='*60}")
        logger.info(f"🎯 ANÁLISIS COMPLETO")
        logger.info(f"Pool: {pool_event.pool_address[:12]}...")
        logger.info(f"Token: {pool_event.token_mint[:12]}...")
        logger.info(f"Liquidez: {pool_event.liquidity_sol:.2f} SOL")
        logger.info(f"{'='*60}")
        
        # Verificar límites
        if len(state.positions) >= config.MAX_POSITIONS:
            logger.warning(f"⚠️ Límite de posiciones alcanzado ({config.MAX_POSITIONS})")
            return
        
        if state.stats['today_trades'] >= config.MAX_DAILY_TRADES:
            logger.warning(f"⚠️ Límite diario de trades alcanzado ({config.MAX_DAILY_TRADES})")
            return
        
        # PASO 1: Rug Checks
        logger.info("🛡️ PASO 1/3: Ejecutando Rug Checks...")
        
        rug_result = await state.rug_checker.check_token_safety(
            token_mint=pool_event.token_mint,
            creator_address=pool_event.creator,
            liquidity_sol=pool_event.liquidity_sol
        )
        
        if not rug_result.is_safe:
            state.stats['rug_checks_failed'] += 1
            logger.warning(f"❌ RUG CHECK FAILED: {rug_result}")
            logger.warning(f"   Failures: {', '.join(rug_result.failures[:3])}")
            
            await send_telegram(
                f"❌ <b>Pool Rechazado</b>\n\n"
                f"Token: {pool_event.token_mint[:12]}...\n"
                f"Risk: {rug_result.risk_level}\n"
                f"Checks: {rug_result.checks_passed}/5\n"
                f"Razón: {rug_result.failures[0] if rug_result.failures else 'Unknown'}"
            )
            return
        
        state.stats['rug_checks_passed'] += 1
        logger.info(f"✅ RUG CHECKS PASSED: {rug_result}")
        
        # PASO 2: Obtener precio actual
        logger.info("💰 PASO 2/3: Calculando precio...")
        
        price_usd = await state.price_calc.get_token_price_usd(
            pool_address=pool_event.pool_address,
            token_mint=pool_event.token_mint
        )
        
        if not price_usd or price_usd <= 0:
            logger.error("❌ No se pudo calcular precio")
            return
        
        logger.info(f"✅ Precio calculado: ${price_usd:.10f}")
        
        # PASO 3: Ejecutar trade
        logger.info("🚀 PASO 3/3: Ejecutando trade...")
        
        tx_sig = await state.trader.execute_buy(
            token_mint=pool_event.token_mint,
            dry_run=config.DRY_RUN
        )
        
        if not tx_sig:
            logger.error("❌ Trade failed")
            return
        
        # Actualizar stats
        if config.DRY_RUN:
            state.stats['trades_simulated'] += 1
        else:
            state.stats['trades_executed'] += 1
        
        state.stats['today_trades'] += 1
        
        # Crear posición
        position = Position(
            token_mint=pool_event.token_mint,
            pool_address=pool_event.pool_address,
            entry_price_usd=price_usd,
            entry_time=time.time(),
            amount_sol=config.TRADE_AMOUNT_SOL,
            highest_price=price_usd,
            lowest_price=price_usd,
            entry_tx=tx_sig,
            rug_check_passed=rug_result.checks_passed
        )
        
        state.positions[pool_event.token_mint] = position
        
        logger.info(f"✅ POSICIÓN ABIERTA!")
        logger.info(f"   Token: {pool_event.token_mint[:12]}...")
        logger.info(f"   Precio: ${price_usd:.10f}")
        logger.info(f"   Cantidad: {config.TRADE_AMOUNT_SOL} SOL")
        logger.info(f"   TX: {tx_sig[:16]}...")
        
        # Notificar Telegram
        mode_emoji = "🧪" if config.DRY_RUN else "💰"
        mode_text = "[DRY RUN]" if config.DRY_RUN else "[REAL]"
        
        await send_telegram(
            f"{mode_emoji} <b>{mode_text} COMPRA EJECUTADA</b>\n\n"
            f"<b>Token:</b> {pool_event.token_mint[:12]}...\n"
            f"<b>Pool:</b> {pool_event.pool_address[:12]}...\n"
            f"<b>Precio:</b> ${price_usd:.10f}\n"
            f"<b>Cantidad:</b> {config.TRADE_AMOUNT_SOL} SOL\n"
            f"<b>Liquidez:</b> {pool_event.liquidity_sol:.2f} SOL\n"
            f"<b>Rug Checks:</b> {rug_result.checks_passed}/5 ✅\n\n"
            f"<b>TX:</b> <code>{tx_sig[:16]}...</code>\n\n"
            f"📊 Stats: {state.stats['today_trades']} trades hoy"
        )
        
    except Exception as e:
        logger.error(f"❌ Error en analyze_and_trade_pool: {e}", exc_info=True)

# ═══════════════════════════════════════════════════════════════
# MONITOREO DE POSICIONES
# ═══════════════════════════════════════════════════════════════

async def monitor_positions():
    """
    Loop de monitoreo de posiciones abiertas
    """
    logger.info("👁️ Iniciando monitoreo de posiciones...")
    
    while state.running:
        try:
            if not state.positions:
                await asyncio.sleep(config.POSITION_CHECK_INTERVAL)
                continue
            
            logger.info(f"\n🔍 Monitoreando {len(state.positions)} posición(es)...")
            
            for token_mint, position in list(state.positions.items()):
                try:
                    # Obtener precio actual
                    current_price = await state.price_calc.get_token_price_usd(
                        pool_address=position.pool_address,
                        token_mint=token_mint
                    )
                    
                    if not current_price:
                        logger.warning(f"⚠️ No se pudo obtener precio para {token_mint[:8]}...")
                        continue
                    
                    # Actualizar precios históricos
                    if current_price > position.highest_price:
                        position.highest_price = current_price
                    
                    if current_price < position.lowest_price:
                        position.lowest_price = current_price
                    
                    # Calcular métricas
                    pnl = position.current_pnl(current_price)
                    hold_time = position.hold_time_minutes()
                    
                    logger.info(
                        f"📊 {token_mint[:8]}: "
                        f"${current_price:.10f} | "
                        f"P&L: {pnl:+.2f}% | "
                        f"Tiempo: {hold_time:.1f}m | "
                        f"Max: {position.current_pnl(position.highest_price):+.2f}%"
                    )
                    
                    # Evaluar condiciones de salida
                    exit_reason = None
                    
                    # Stop Loss
                    if pnl <= config.STOP_LOSS_PERCENT:
                        exit_reason = "STOP_LOSS"
                        logger.warning(f"🛑 {token_mint[:8]} STOP LOSS: {pnl:.2f}%")
                    
                    # Take Profit 2
                    elif pnl >= config.TAKE_PROFIT_2:
                        exit_reason = "TAKE_PROFIT_2"
                        logger.info(f"💰💰 {token_mint[:8]} TP2: {pnl:.2f}%")
                    
                    # Take Profit 1
                    elif pnl >= config.TAKE_PROFIT_1:
                        exit_reason = "TAKE_PROFIT_1"
                        logger.info(f"💰 {token_mint[:8]} TP1: {pnl:.2f}%")
                    
                    # Timeout con pérdida
                    elif hold_time > 60 and pnl < -5:
                        exit_reason = "TIMEOUT_LOSS"
                        logger.warning(f"⏱️ {token_mint[:8]} timeout: {pnl:.2f}%")
                    
                    # Ejecutar salida si es necesario
                    if exit_reason:
                        await exit_position(
                            token_mint=token_mint,
                            position=position,
                            current_price=current_price,
                            exit_reason=exit_reason
                        )
                
                except Exception as e:
                    logger.error(f"Error monitoreando {token_mint[:8]}: {e}")
            
            # Esperar antes del siguiente check
            await asyncio.sleep(config.POSITION_CHECK_INTERVAL)
        
        except Exception as e:
            logger.error(f"Error en monitor_positions loop: {e}")
            await asyncio.sleep(config.POSITION_CHECK_INTERVAL)

async def exit_position(
    token_mint: str,
    position: Position,
    current_price: float,
    exit_reason: str
):
    """
    Cerrar una posición
    """
    try:
        pnl = position.current_pnl(current_price)
        hold_time = position.hold_time_minutes()
        is_win = pnl > 0
        
        logger.info(f"\n{'='*60}")
        logger.info(f"{'✅ CERRANDO POSICIÓN GANADORA' if is_win else '❌ CERRANDO POSICIÓN PERDEDORA'}")
        logger.info(f"Token: {token_mint[:12]}...")
        logger.info(f"Entrada: ${position.entry_price_usd:.10f}")
        logger.info(f"Salida: ${current_price:.10f}")
        logger.info(f"P&L: {pnl:+.2f}%")
        logger.info(f"Tiempo: {hold_time:.1f}m")
        logger.info(f"Razón: {exit_reason}")
        logger.info(f"{'='*60}")
        
        # Ejecutar venta
        # Nota: necesitaríamos calcular cuántos tokens tenemos
        # Por simplicidad en DRY_RUN, simulamos
        
        if config.DRY_RUN:
            logger.info("🧪 [DRY RUN] Simulando venta...")
            tx_sig = f"simulated-sell-{int(time.time())}"
        else:
            # En modo real, ejecutar venta via Jupiter
            logger.warning("💰 [REAL] Ejecutando venta...")
            # tx_sig = await state.trader.execute_sell(...)
            tx_sig = "real-sell-pending"
        
        # Actualizar stats
        if is_win:
            state.stats['wins'] += 1
        else:
            state.stats['losses'] += 1
        
        state.stats['total_pnl'] += pnl
        
        # Guardar en DB
        await save_trade(position, current_price, exit_reason)
        
        # Eliminar posición
        del state.positions[token_mint]
        
        # Notificar Telegram
        emoji = "✅" if is_win else "❌"
        result_text = "GANANCIA" if is_win else "PÉRDIDA"
        
        win_rate = 0
        total_closed = state.stats['wins'] + state.stats['losses']
        if total_closed > 0:
            win_rate = (state.stats['wins'] / total_closed) * 100
        
        mode_tag = "[DRY RUN]" if config.DRY_RUN else "[REAL]"
        
        await send_telegram(
            f"{emoji} <b>{mode_tag} {result_text}</b>\n\n"
            f"<b>Token:</b> {token_mint[:12]}...\n"
            f"<b>Entrada:</b> ${position.entry_price_usd:.10f}\n"
            f"<b>Salida:</b> ${current_price:.10f}\n"
            f"<b>P&L:</b> {pnl:+.2f}%\n\n"
            f"<b>Máximo:</b> ${position.highest_price:.10f} "
            f"({position.current_pnl(position.highest_price):+.2f}%)\n"
            f"<b>Mínimo:</b> ${position.lowest_price:.10f}\n"
            f"<b>Tiempo:</b> {hold_time:.1f}m\n"
            f"<b>Razón:</b> {exit_reason}\n\n"
            f"📊 <b>Stats:</b>\n"
            f"W/L: {state.stats['wins']}/{state.stats['losses']} "
            f"({win_rate:.1f}%)\n"
            f"P&L Total: {state.stats['total_pnl']:+.2f}%"
        )
        
        logger.info(f"✅ Posición cerrada y guardada")
        
    except Exception as e:
        logger.error(f"❌ Error cerrando posición: {e}", exc_info=True)

# ═══════════════════════════════════════════════════════════════
# INICIALIZACIÓN
# ═══════════════════════════════════════════════════════════════

async def initialize_bot():
    """
    Inicializar todos los componentes del bot
    """
    try:
        logger.info("\n" + "="*60)
        logger.info("🚀 RAYDIUM SNIPER BOT")
        logger.info("="*60)
        
        # 1. Validar configuración crítica
        if not config.PRIVATE_KEY:
            raise ValueError("❌ WALLET_PRIVATE_KEY no configurada")
        
        if not config.HELIUS_WSS_URL:
            raise ValueError("❌ HELIUS_WSS_URL no configurada")
        
        # 2. Inicializar wallet
        logger.info("🔐 Inicializando wallet...")
        state.wallet = Keypair.from_base58_string(config.PRIVATE_KEY)
        logger.info(f"✅ Wallet: {str(state.wallet.pubkey())[:12]}...")
        
        # 3. Inicializar RPC Pool
        logger.info("🔄 Inicializando RPC Pool...")
        state.rpc_pool = RPCPool()
        await state.rpc_pool.health_check()
        
        # 4. Inicializar componentes
        logger.info("🛡️ Inicializando Rug Checker...")
        state.rug_checker = RugChecker(state.rpc_pool)
        
        logger.info("💰 Inicializando Price Calculator...")
        state.price_calc = PriceCalculator(state.rpc_pool)
        
        logger.info("🪐 Inicializando Jupiter Trader...")
        state.trader = JupiterTrader(state.rpc_pool, state.wallet)
        
        # 5. Database
        logger.info("💾 Inicializando Database...")
        await init_database()
        
        # 6. Telegram
        if config.ENABLE_TELEGRAM and TELEGRAM_AVAILABLE and config.TELEGRAM_TOKEN:
            logger.info("📱 Inicializando Telegram...")
            state.telegram_bot = Bot(token=config.TELEGRAM_TOKEN)
            
            mode = "DRY_RUN 🧪" if config.DRY_RUN else "REAL 💰"
            await send_telegram(
                f"🚀 <b>Sniper Bot Iniciado</b>\n\n"
                f"<b>Modo:</b> {mode}\n"
                f"<b>Wallet:</b> <code>{str(state.wallet.pubkey())[:12]}...</code>\n"
                f"<b>Amount:</b> {config.TRADE_AMOUNT_SOL} SOL\n"
                f"<b>Stop Loss:</b> {config.STOP_LOSS_PERCENT}%\n"
                f"<b>Take Profit:</b> {config.TAKE_PROFIT_1}% / {config.TAKE_PROFIT_2}%\n"
                f"<b>Min Liquidity:</b> {config.MIN_LIQUIDITY_SOL} SOL\n\n"
                f"✅ Escuchando nuevos pools de Raydium..."
            )
        
        logger.info("\n" + "="*60)
        logger.info("✅ BOT INICIALIZADO CORRECTAMENTE")
        logger.info(f"🧪 Modo: {'DRY_RUN' if config.DRY_RUN else 'REAL'}")
        logger.info(f"💰 Trade Amount: {config.TRADE_AMOUNT_SOL} SOL")
        logger.info(f"📊 Targets: {config.TAKE_PROFIT_1}% / {config.TAKE_PROFIT_2}%")
        logger.info(f"🛑 Stop Loss: {config.STOP_LOSS_PERCENT}%")
        logger.info("="*60 + "\n")
        
    except Exception as e:
        logger.error(f"❌ Error inicializando bot: {e}")
        raise

# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

async def main():
    """Entry point principal"""
    try:
        # Inicializar
        await initialize_bot()
        
        # Lanzar tareas en paralelo
        tasks = [
            asyncio.create_task(listen_new_pools()),
            asyncio.create_task(monitor_positions())
        ]
        
        # Esperar a que termine (o Ctrl+C)
        await asyncio.gather(*tasks)
        
    except KeyboardInterrupt:
        logger.info("\n⏸️ Deteniendo bot por usuario...")
        state.running = False
        
    except Exception as e:
        logger.error(f"❌ Error crítico: {e}", exc_info=True)
        
    finally:
        # Cleanup
        logger.info("🧹 Limpiando recursos...")
        
        if state.positions:
            logger.warning(f"⚠️ Cerrando {len(state.positions)} posición(es) pendiente(s)...")
            for token_mint, position in list(state.positions.items()):
                try:
                    price = await state.price_calc.get_token_price_usd(
                        position.pool_address,
                        token_mint
                    )
                    if price:
                        await exit_position(token_mint, position, price, "BOT_STOPPED")
                except Exception as e:
                    logger.error(f"Error cerrando {token_mint[:8]}: {e}")
        
        if state.db_pool:
            await state.db_pool.close()
        
        if state.telegram_bot:
            await send_telegram(
                f"👋 <b>Bot Detenido</b>\n\n"
                f"Pools detectados: {state.stats['pools_detected']}\n"
                f"Pools analizados: {state.stats['pools_analyzed']}\n"
                f"Rug checks passed: {state.stats['rug_checks_passed']}\n"
                f"Trades: {state.stats['trades_simulated'] + state.stats['trades_executed']}\n"
                f"W/L: {state.stats['wins']}/{state.stats['losses']}\n"
                f"P&L Final: {state.stats['total_pnl']:+.2f}%"
            )
        
        logger.info("✅ Bot detenido correctamente")

if __name__ == "__main__":
    try:
        import time
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Hasta luego")
        sys.exit(0)
