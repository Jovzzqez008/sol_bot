#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
💰 PRICE CALCULATOR - Parser On-Chain de Raydium
=================================================
Calcula precios directamente de la blockchain sin APIs externas
"""

import asyncio
import logging
import time
from typing import Optional, Dict
from solders.pubkey import Pubkey
from construct import Struct, Int64ul, Padding, Bytes
from rpc_pool import RPCPool

logger = logging.getLogger(__name__)

# Layout del Pool State V4 de Raydium (estructura de datos)
RAYDIUM_POOL_V4_LAYOUT = Struct(
    "status" / Int64ul,
    "nonce" / Int64ul,
    "max_order" / Int64ul,
    "depth" / Int64ul,
    "base_decimal" / Int64ul,
    "quote_decimal" / Int64ul,
    "state" / Int64ul,
    "reset_flag" / Int64ul,
    "min_size" / Int64ul,
    "vol_max_cut_ratio" / Int64ul,
    "amount_wave_ratio" / Int64ul,
    "base_lot_size" / Int64ul,
    "quote_lot_size" / Int64ul,
    "min_price_multiplier" / Int64ul,
    "max_price_multiplier" / Int64ul,
    "system_decimal_value" / Int64ul,
    "min_separate_numerator" / Int64ul,
    "min_separate_denominator" / Int64ul,
    "trade_fee_numerator" / Int64ul,
    "trade_fee_denominator" / Int64ul,
    "pnl_numerator" / Int64ul,
    "pnl_denominator" / Int64ul,
    "swap_fee_numerator" / Int64ul,
    "swap_fee_denominator" / Int64ul,
    "base_need_take_pnl" / Int64ul,
    "quote_need_take_pnl" / Int64ul,
    "quote_total_pnl" / Int64ul,
    "base_total_pnl" / Int64ul,
    "pool_open_time" / Int64ul,
    "punish_pc_amount" / Int64ul,
    "punish_coin_amount" / Int64ul,
    "orderbook_to_init_time" / Int64ul,
    "swap_base_in_amount" / Int64ul,
    "swap_quote_out_amount" / Int64ul,
    "swap_base2_quote_fee" / Int64ul,
    "swap_quote_in_amount" / Int64ul,
    "swap_base_out_amount" / Int64ul,
    "swap_quote2_base_fee" / Int64ul,
    "base_vault" / Bytes(32),
    "quote_vault" / Bytes(32),
    "base_mint" / Bytes(32),
    "quote_mint" / Bytes(32),
    "lp_mint" / Bytes(32),
    "open_orders" / Bytes(32),
    "market_id" / Bytes(32),
    "market_program_id" / Bytes(32),
    "target_orders" / Bytes(32),
    "withdraw_queue" / Bytes(32),
    "lp_vault" / Bytes(32),
    "owner" / Bytes(32),
    Padding(32),  # lpReserve
    Padding(8 * 3),  # padding
)

class PriceCalculator:
    """Calculador de precios desde la blockchain"""
    
    def __init__(self, rpc_pool: RPCPool):
        self.rpc_pool = rpc_pool
        self.sol_price_cache = None
        self.sol_price_timestamp = 0
        
        # SOL mint address
        self.SOL_MINT = "So11111111111111111111111111111111111111112"
    
    async def get_token_price_usd(
        self, 
        pool_address: str,
        token_mint: str
    ) -> Optional[float]:
        """
        Calcular precio de un token en USD desde el pool de Raydium
        
        Args:
            pool_address: Dirección del pool (AMM ID)
            token_mint: Dirección del token a cotizar
            
        Returns:
            Precio en USD o None si falla
        """
        try:
            # 1. Obtener precio en SOL
            price_in_sol = await self.get_token_price_in_sol(pool_address, token_mint)
            
            if not price_in_sol or price_in_sol <= 0:
                return None
            
            # 2. Obtener precio de SOL en USD
            sol_price_usd = await self.get_sol_price_usd()
            
            if not sol_price_usd:
                return None
            
            # 3. Calcular precio final
            price_usd = price_in_sol * sol_price_usd
            
            logger.debug(f"💰 {token_mint[:8]}: {price_in_sol:.10f} SOL = ${price_usd:.10f}")
            
            return price_usd
            
        except Exception as e:
            logger.error(f"Error calculando precio: {e}")
            return None
    
    async def get_token_price_in_sol(
        self,
        pool_address: str,
        token_mint: str
    ) -> Optional[float]:
        """
        Calcular precio de un token en términos de SOL
        
        Precio = Reserva_SOL / Reserva_Token
        """
        try:
            client = self.rpc_pool.get_client()
            pool_pubkey = Pubkey.from_string(pool_address)
            
            # Obtener datos de la cuenta del pool
            account_info = await client.get_account_info(pool_pubkey)
            await client.close()
            
            if not account_info.value or not account_info.value.data:
                logger.warning(f"⚠️ Pool account not found: {pool_address[:8]}")
                return None
            
            # Parsear los datos binarios usando el layout
            pool_data = RAYDIUM_POOL_V4_LAYOUT.parse(account_info.value.data)
            
            # Extraer vaults
            base_vault_pubkey = Pubkey(pool_data.base_vault)
            quote_vault_pubkey = Pubkey(pool_data.quote_vault)
            
            # Obtener balances
            base_info, quote_info = await asyncio.gather(
                self._get_token_account_balance(str(base_vault_pubkey)),
                self._get_token_account_balance(str(quote_vault_pubkey))
            )
            
            if not base_info or not quote_info:
                return None
            
            base_reserve = base_info['amount']
            quote_reserve = quote_info['amount']
            
            base_decimals = base_info['decimals']
            quote_decimals = quote_info['decimals']
            
            # Ajustar por decimales
            base_reserve_adjusted = base_reserve / (10 ** base_decimals)
            quote_reserve_adjusted = quote_reserve / (10 ** quote_decimals)
            
            if base_reserve_adjusted == 0 or quote_reserve_adjusted == 0:
                return None
            
            # Determinar cuál es SOL y cuál es el token
            base_mint = str(Pubkey(pool_data.base_mint))
            quote_mint = str(Pubkey(pool_data.quote_mint))
            
            if base_mint == self.SOL_MINT:
                # SOL es base, token es quote
                price_in_sol = base_reserve_adjusted / quote_reserve_adjusted
            elif quote_mint == self.SOL_MINT:
                # Token es base, SOL es quote
                price_in_sol = quote_reserve_adjusted / base_reserve_adjusted
            else:
                # Ninguno es SOL - no podemos calcular directamente
                logger.warning(f"⚠️ Pool no tiene SOL: {base_mint[:8]} / {quote_mint[:8]}")
                return None
            
            return price_in_sol
            
        except Exception as e:
            logger.error(f"Error obteniendo precio en SOL: {e}")
            return None
    
    async def _get_token_account_balance(self, account_address: str) -> Optional[Dict]:
        """Obtener balance de una token account"""
        try:
            client = self.rpc_pool.get_client()
            pubkey = Pubkey.from_string(account_address)
            
            balance_info = await client.get_token_account_balance(pubkey)
            await client.close()
            
            if not balance_info.value:
                return None
            
            return {
                'amount': int(balance_info.value.amount),
                'decimals': balance_info.value.decimals,
                'ui_amount': float(balance_info.value.ui_amount or 0)
            }
            
        except Exception as e:
            logger.debug(f"Error getting balance: {e}")
            return None
    
    async def get_sol_price_usd(self) -> Optional[float]:
        """
        Obtener precio de SOL en USD
        Usa cache de 60 segundos para no hacer demasiadas llamadas
        """
        import aiohttp
        
        # Usar cache si tiene menos de 60 segundos
        if self.sol_price_cache and (time.time() - self.sol_price_timestamp) < 60:
            return self.sol_price_cache
        
        try:
            # Usar CoinGecko API gratuita (sin API key necesaria)
            url = "https://api.coingecko.com/api/v3/simple/price"
            params = {
                "ids": "solana",
                "vs_currencies": "usd"
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=5) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        sol_price = data.get("solana", {}).get("usd")
                        
                        if sol_price:
                            self.sol_price_cache = float(sol_price)
                            self.sol_price_timestamp = time.time()
                            logger.debug(f"💵 SOL Price: ${sol_price:.2f}")
                            return sol_price
            
            # Fallback: usar precio fijo conservador
            logger.warning("⚠️ No se pudo obtener precio de SOL, usando fallback: $150")
            return 150.0
            
        except Exception as e:
            logger.warning(f"Error obteniendo precio de SOL: {e}, usando fallback")
            return 150.0
    
    async def get_pool_liquidity_sol(self, pool_address: str) -> Optional[float]:
        """
        Calcular liquidez total del pool en SOL
        """
        try:
            client = self.rpc_pool.get_client()
            pool_pubkey = Pubkey.from_string(pool_address)
            
            account_info = await client.get_account_info(pool_pubkey)
            await client.close()
            
            if not account_info.value or not account_info.value.data:
                return None
            
            # CORREGIDO: Se eliminó el punto extra después de account_info.value
            pool_data = RAYDIUM_POOL_V4_LAYOUT.parse(account_info.value.data)
            
            # Extraer vaults
            base_vault_pubkey = Pubkey(pool_data.base_vault)
            quote_vault_pubkey = Pubkey(pool_data.quote_vault)
            
            # Obtener balances
            base_info, quote_info = await asyncio.gather(
                self._get_token_account_balance(str(base_vault_pubkey)),
                self._get_token_account_balance(str(quote_vault_pubkey))
            )
            
            if not base_info or not quote_info:
                return None
            
            base_reserve = base_info['ui_amount']
            quote_reserve = quote_info['ui_amount']
            
            if base_reserve == 0 or quote_reserve == 0:
                return None
            
            # Determinar cuál es SOL
            base_mint = str(Pubkey(pool_data.base_mint))
            quote_mint = str(Pubkey(pool_data.quote_mint))
            
            if base_mint == self.SOL_MINT:
                # Liquidez en SOL = 2 * reserva de SOL (porque es 50/50)
                liquidity_sol = 2 * base_reserve
            elif quote_mint == self.SOL_MINT:
                # Liquidez en SOL = 2 * reserva de SOL
                liquidity_sol = 2 * quote_reserve
            else:
                # Pool no contiene SOL
                return None
            
            return liquidity_sol
            
        except Exception as e:
            logger.error(f"Error calculando liquidez: {e}")
            return None
