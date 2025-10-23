#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🚀 MAIN ENTRY POINT - HEALTH SERVER + SNIPER BOT
=================================================
✅ Health server inicia INMEDIATAMENTE
✅ Sniper bot inicia después (asíncrono)
✅ Railway recibe 200 OK en segundos
"""

import asyncio
import logging
import sys
import os

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('main.log')
    ]
)

logger = logging.getLogger(__name__)

async def main():
    """✅ Entry point con health server PRIMERO"""
    
    try:
        logger.info("=" * 60)
        logger.info("🚀 RAYDIUM SNIPER BOT + HEALTH SERVER")
        logger.info("=" * 60)
        
        # ✅ Importar health server PRIMERO
        logger.info("🏥 Importando health server...")
        from health_server import start_health_server, update_bot_status
        
        # ✅ Obtener puerto de Railway
        port = int(os.getenv('PORT', '8080'))
        logger.info(f"📡 Puerto asignado: {port}")
        
        # ✅ Inicializar estado inicial
        update_bot_status(
            running=False,
            scans=0,
            positions=0,
            signals=0,
            trades=0,
            wins=0,
            losses=0,
            total_pnl=0.0,
            ml_enabled=False,
            mode="initializing"
        )
        
        logger.info("✅ Health server configurado - iniciando servidor HTTP...")
        
        # ✅ CRÍTICO: Iniciar health server EN BACKGROUND
        health_task = asyncio.create_task(start_health_server(port=port))
        
        # ✅ Esperar 2 segundos para que el servidor esté listo
        await asyncio.sleep(2)
        logger.info("✅ Health server ONLINE - Railway debería recibir 200 OK")
        
        # ✅ Ahora SÍ importar y ejecutar el bot sniper
        logger.info("🎯 Importando Raydium Sniper Bot...")
        try:
            # Importar desde ambos archivos (part 1 y part 2 combinados)
            import raydium_sniper_bot as sniper_bot
            
            logger.info("✅ Sniper bot importado")
            
            # Actualizar estado
            update_bot_status(
                running=True,
                scans=0,
                positions=0,
                mode="DRY_RUN" if os.getenv('DRY_RUN', 'true').lower() == 'true' else "REAL"
            )
            
            logger.info("🚀 Iniciando Sniper Bot...")
            
            # ✅ Ejecutar bot en paralelo con health server
            await asyncio.gather(
                health_task,  # Ya está corriendo
                sniper_bot.main(),  # Iniciar bot ahora
                return_exceptions=True
            )
            
        except ImportError as e:
            logger.error(f"❌ Error importando sniper bot: {e}")
            logger.warning("⚠️ Health server sigue corriendo sin bot")
            
            # Actualizar estado
            update_bot_status(
                running=False,
                scans=0,
                positions=0,
                mode="error_import"
            )
            
            # Mantener health server vivo
            await health_task
        
    except KeyboardInterrupt:
        logger.info("⏸️ Detenido por usuario")
        sys.exit(0)
        
    except Exception as e:
        logger.error(f"❌ Error crítico: {e}", exc_info=True)
        
        # Intentar mantener health server vivo aunque falle el bot
        try:
            from health_server import update_bot_status
            update_bot_status(
                running=False,
                scans=0,
                positions=0,
                mode="error_critical"
            )
            logger.info("⚠️ Health server sigue activo a pesar del error")
            await asyncio.Event().wait()  # Mantener vivo indefinidamente
        except:
            sys.exit(1)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Adiós")
        sys.exit(0)
