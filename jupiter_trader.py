#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🪐 JUPITER TRADER - Integración con Jupiter V6 API
===================================================
Maneja quotes, swaps y ejecución de trades
"""

import os
import base64
import logging
from typing import Optional, Dict
import aiohttp
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solana.rpc.async_api import AsyncClient
from solana.rpc.types import TxOpts
from solana.rpc.commitment import Confirmed
from rpc_pool import RPCPool

logger = logging.getLogger(__name__)

class JupiterTrader:
    """Cliente para Jupiter V6 Swap API"""
    
    def __init__(self, rpc_pool: RPCPool, wallet_keypair: Keypair):
        self.rpc_pool = rpc_pool
        self.wallet = wallet_keypair
        
        # Jupiter V6 endpoints
        self.QUOTE_API = "https://quote-api.jup.ag/v6/quote"
        self.SWAP_API = "https://quote-api.jup.ag/v6/swap"
        
        # Configuración
        self.trade_amount_sol = float(os.getenv('TRADE_AMOUNT_SOL', '0.01'))
        self.slippage_bps = int(os.getenv('SLIPPAGE_BPS', '1500'))  # 15%
        
        # SOL mint
        self.SOL_MINT = "So11111111111111111111111111111111111111112"
        
        logger.info(f"✅ Jupiter Trader inicializado")
        logger.info(f"   Wallet: {str(self.wallet.pubkey())[:8]}...")
        logger.info(f"   Amount: {self.trade_amount_sol} SOL")
        logger.info(f"   Slippage: {self.slippage_bps / 100}%")
    
    async def get_quote(
        self, 
        input_mint: str,
        output_mint: str,
        amount: Optional[int] = None
    ) -> Optional[Dict]:
        """
        Obtener quote de Jupiter
        
        Args:
            input_mint: Token de entrada (usualmente SOL)
            output_mint: Token de salida (el que queremos comprar)
            amount: Cantidad en lamports (None = usar trade_amount_sol)
        
        Returns:
            Quote response de Jupiter o None
        """
        try:
            if amount is None:
                # Convertir SOL a lamports (1 SOL = 1,000,000,000 lamports)
                amount = int(self.trade_amount_sol * 1_000_000_000)
            
            params = {
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": str(amount),
                "slippageBps": str(self.slippage_bps),
                "onlyDirectRoutes": "false",
                "asLegacyTransaction": "false"
            }
            
            logger.info(f"📊 Solicitando quote: {amount / 1e9:.4f} SOL -> {output_mint[:8]}...")
            
            async with aiohttp.ClientSession() as session:
                async with session.get(self.QUOTE_API, params=params, timeout=10) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        logger.error(f"❌ Quote failed: {resp.status} - {text[:200]}")
                        return None
                    
                    quote = await resp.json()
                    
                    # Extraer info útil
                    out_amount = int(quote.get("outAmount", 0))
                    price_impact = float(quote.get("priceImpactPct", 0))
                    
                    logger.info(f"✅ Quote recibido:")
                    logger.info(f"   Out Amount: {out_amount:,} tokens")
                    logger.info(f"   Price Impact: {price_impact:.2f}%")
                    
                    return quote
        
        except Exception as e:
            logger.error(f"❌ Error getting quote: {e}")
            return None
    
    async def get_swap_transaction(
        self,
        quote_response: Dict
    ) -> Optional[str]:
        """
        Obtener transacción de swap desde el quote
        
        Returns:
            Transacción serializada en base64 o None
        """
        try:
            payload = {
                "userPublicKey": str(self.wallet.pubkey()),
                "quoteResponse": quote_response,
                "wrapAndUnwrapSol": True,
                "asLegacyTransaction": False
            }
            
            logger.info(f"🔄 Solicitando swap transaction...")
            
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.SWAP_API,
                    json=payload,
                    timeout=10
                ) as resp:
                    if resp.status not in [200, 201]:
                        text = await resp.text()
                        logger.error(f"❌ Swap request failed: {resp.status} - {text[:200]}")
                        return None
                    
                    swap_response = await resp.json()
                    swap_transaction = swap_response.get("swapTransaction")
                    
                    if not swap_transaction:
                        logger.error("❌ No swapTransaction en respuesta")
                        return None
                    
                    logger.info(f"✅ Swap transaction recibida ({len(swap_transaction)} chars)")
                    return swap_transaction
        
        except Exception as e:
            logger.error(f"❌ Error getting swap transaction: {e}")
            return None
    
    async def execute_buy(
        self,
        token_mint: str,
        dry_run: bool = True
    ) -> Optional[str]:
        """
        Ejecutar compra de un token
        
        Args:
            token_mint: Dirección del token a comprar
            dry_run: Si True, solo simula. Si False, ejecuta real
        
        Returns:
            Transaction signature o None
        """
        try:
            logger.info(f"{'🧪 [DRY RUN]' if dry_run else '💰 [REAL]'} Comprando {token_mint[:8]}...")
            
            # 1. Obtener quote
            quote = await self.get_quote(
                input_mint=self.SOL_MINT,
                output_mint=token_mint
            )
            
            if not quote:
                logger.error("❌ No se pudo obtener quote")
                return None
            
            # 2. Obtener transacción
            swap_tx_b64 = await self.get_swap_transaction(quote)
            
            if not swap_tx_b64:
                logger.error("❌ No se pudo obtener swap transaction")
                return None
            
            # 3. Deserializar y firmar
            tx_bytes = base64.b64decode(swap_tx_b64)
            versioned_tx = VersionedTransaction.from_bytes(tx_bytes)
            
            # Firmar con nuestra wallet
            signed_tx = versioned_tx.sign([self.wallet])
            
            # 4. Ejecutar o simular
            client = self.rpc_pool.get_client()
            
            if dry_run:
                # Modo simulación
                logger.info("🧪 Simulando transacción...")
                sim_result = await client.simulate_transaction(signed_tx)
                await client.close()
                
                if sim_result.value.err:
                    logger.error(f"❌ Simulación falló: {sim_result.value.err}")
                    return None
                
                logger.info("✅ Simulación exitosa!")
                logger.info(f"   Logs: {sim_result.value.logs[:3] if sim_result.value.logs else 'None'}")
                
                # Retornar un ID simulado
                return f"simulated-{int(time.time())}"
            
            else:
                # Modo REAL
                logger.warning("💰 EJECUTANDO TRADE REAL...")
                
                tx_sig = await client.send_transaction(
                    signed_tx,
                    opts=TxOpts(
                        skip_preflight=False,  # Verificar antes de enviar
                        preflight_commitment=Confirmed
                    )
                )
                await client.close()
                
                logger.info(f"✅ Trade ejecutado: {tx_sig}")
                return str(tx_sig)
        
        except Exception as e:
            logger.error(f"❌ Error ejecutando trade: {e}")
            return None
    
    async def execute_sell(
        self,
        token_mint: str,
        amount_tokens: int,
        dry_run: bool = True
    ) -> Optional[str]:
        """
        Ejecutar venta de un token
        
        Args:
            token_mint: Dirección del token a vender
            amount_tokens: Cantidad de tokens (en unidades mínimas)
            dry_run: Si True, solo simula
        
        Returns:
            Transaction signature o None
        """
        try:
            logger.info(f"{'🧪 [DRY RUN]' if dry_run else '💰 [REAL]'} Vendiendo {token_mint[:8]}...")
            
            # 1. Obtener quote (ahora input es el token, output es SOL)
            quote = await self.get_quote(
                input_mint=token_mint,
                output_mint=self.SOL_MINT,
                amount=amount_tokens
            )
            
            if not quote:
                logger.error("❌ No se pudo obtener quote para venta")
                return None
            
            # 2. Obtener transacción
            swap_tx_b64 = await self.get_swap_transaction(quote)
            
            if not swap_tx_b64:
                logger.error("❌ No se pudo obtener swap transaction")
                return None
            
            # 3. Deserializar y firmar
            tx_bytes = base64.b64decode(swap_tx_b64)
            versioned_tx = VersionedTransaction.from_bytes(tx_bytes)
            signed_tx = versioned_tx.sign([self.wallet])
            
            # 4. Ejecutar o simular
            client = self.rpc_pool.get_client()
            
            if dry_run:
                logger.info("🧪 Simulando venta...")
                sim_result = await client.simulate_transaction(signed_tx)
                await client.close()
                
                if sim_result.value.err:
                    logger.error(f"❌ Simulación de venta falló: {sim_result.value.err}")
                    return None
                
                logger.info("✅ Simulación de venta exitosa!")
                return f"simulated-sell-{int(time.time())}"
            
            else:
                logger.warning("💰 EJECUTANDO VENTA REAL...")
                
                tx_sig = await client.send_transaction(
                    signed_tx,
                    opts=TxOpts(skip_preflight=False, preflight_commitment=Confirmed)
                )
                await client.close()
                
                logger.info(f"✅ Venta ejecutada: {tx_sig}")
                return str(tx_sig)
        
        except Exception as e:
            logger.error(f"❌ Error ejecutando venta: {e}")
            return None

import time
