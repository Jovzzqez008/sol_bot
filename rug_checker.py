#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🛡️ RUG CHECKER - Sistema Exhaustivo Anti-Estafas
=================================================
5 checks críticos para evitar rug pulls y honeypots
"""

import os
import asyncio
import logging
from typing import Dict, Tuple, Optional
from dataclasses import dataclass
from solders.pubkey import Pubkey
from rpc_pool import RPCPool

logger = logging.getLogger(__name__)

@dataclass
class RugCheckResult:
    """Resultado de los checks de seguridad"""
    is_safe: bool
    risk_level: str  # "LOW", "MEDIUM", "HIGH"
    checks_passed: int
    checks_failed: int
    failures: list
    details: Dict
    
    def __str__(self):
        emoji = "✅" if self.is_safe else "❌"
        return f"{emoji} Risk: {self.risk_level} | Passed: {self.checks_passed}/5"

class RugChecker:
    """Verificador de seguridad de tokens"""
    
    def __init__(self, rpc_pool: RPCPool):
        self.rpc_pool = rpc_pool
        self.min_liquidity_sol = float(os.getenv('MIN_LIQUIDITY_SOL', '5.0'))
        self.max_holder_percent = float(os.getenv('MAX_HOLDER_PERCENT', '40.0'))
    
    async def check_token_safety(
        self, 
        token_mint: str, 
        creator_address: Optional[str] = None,
        liquidity_sol: Optional[float] = None
    ) -> RugCheckResult:
        """
        Ejecutar todos los checks de seguridad en paralelo
        
        Returns:
            RugCheckResult con el veredicto final
        """
        logger.info(f"🔍 Iniciando rug checks para {token_mint[:8]}...")
        
        failures = []
        details = {}
        checks_passed = 0
        
        try:
            # Lanzar todos los checks en paralelo
            results = await asyncio.gather(
                self._check_mint_authority(token_mint),
                self._check_freeze_authority(token_mint),
                self._check_holder_distribution(token_mint),
                self._check_liquidity(liquidity_sol),
                self._check_creator_history(creator_address) if creator_address else self._skip_check("creator_history"),
                return_exceptions=True
            )
            
            # Procesar resultados
            check_names = [
                "mint_authority",
                "freeze_authority", 
                "holder_distribution",
                "liquidity",
                "creator_history"
            ]
            
            for i, (check_name, result) in enumerate(zip(check_names, results)):
                if isinstance(result, Exception):
                    logger.warning(f"  ⚠️ {check_name}: ERROR ({str(result)[:50]})")
                    failures.append(f"{check_name}: {str(result)[:50]}")
                    details[check_name] = {"status": "error", "detail": str(result)[:100]}
                elif result["passed"]:
                    checks_passed += 1
                    logger.info(f"  ✅ {check_name}: PASS")
                    details[check_name] = result
                else:
                    logger.warning(f"  ❌ {check_name}: FAIL - {result.get('reason', 'Unknown')}")
                    failures.append(f"{check_name}: {result.get('reason', 'Unknown')}")
                    details[check_name] = result
            
            # Calcular riesgo
            checks_failed = 5 - checks_passed
            
            # Decisión final
            if checks_passed >= 4:
                risk_level = "LOW"
                is_safe = True
            elif checks_passed >= 3:
                risk_level = "MEDIUM"
                is_safe = False  # Demasiado riesgo
            else:
                risk_level = "HIGH"
                is_safe = False
            
            result = RugCheckResult(
                is_safe=is_safe,
                risk_level=risk_level,
                checks_passed=checks_passed,
                checks_failed=checks_failed,
                failures=failures,
                details=details
            )
            
            logger.info(f"🎯 Resultado final: {result}")
            return result
            
        except Exception as e:
            logger.error(f"❌ Error crítico en rug checks: {e}")
            return RugCheckResult(
                is_safe=False,
                risk_level="HIGH",
                checks_passed=0,
                checks_failed=5,
                failures=[f"Critical error: {str(e)}"],
                details={"error": str(e)}
            )
    
    async def _check_mint_authority(self, mint_address: str) -> Dict:
        """Check 1: Verificar que no puedan crear más tokens"""
        try:
            client = self.rpc_pool.get_client()
            pubkey = Pubkey.from_string(mint_address)
            
            account_info = await client.get_account_info(pubkey)
            await client.close()
            
            if not account_info.value:
                return {"passed": False, "reason": "Token account not found"}
            
            # Parsear los datos de la cuenta mint
            data = account_info.value.data
            
            # En Solana, el byte 0 indica si hay mint_authority
            # Si es 0, no hay autoridad (bueno). Si es 1, hay autoridad (malo)
            has_mint_authority = data[0] == 1 if len(data) > 0 else False
            
            if has_mint_authority:
                return {
                    "passed": False,
                    "reason": "Mint authority not renounced (can create infinite tokens)"
                }
            
            return {
                "passed": True,
                "reason": "Mint authority renounced ✓"
            }
            
        except Exception as e:
            return {"passed": False, "reason": f"Error: {str(e)[:50]}"}
    
    async def _check_freeze_authority(self, mint_address: str) -> Dict:
        """Check 2: Verificar que no puedan congelar tokens"""
        try:
            client = self.rpc_pool.get_client()
            pubkey = Pubkey.from_string(mint_address)
            
            account_info = await client.get_account_info(pubkey)
            await client.close()
            
            if not account_info.value:
                return {"passed": False, "reason": "Token account not found"}
            
            data = account_info.value.data
            
            # Byte 45 indica freeze authority
            has_freeze_authority = data[45] == 1 if len(data) > 45 else False
            
            if has_freeze_authority:
                return {
                    "passed": False,
                    "reason": "Freeze authority not renounced (can freeze tokens)"
                }
            
            return {
                "passed": True,
                "reason": "Freeze authority renounced ✓"
            }
            
        except Exception as e:
            return {"passed": False, "reason": f"Error: {str(e)[:50]}"}
    
    async def _check_holder_distribution(self, mint_address: str) -> Dict:
        """Check 3: Verificar que el creador no tenga demasiados tokens"""
        try:
            client = self.rpc_pool.get_client()
            pubkey = Pubkey.from_string(mint_address)
            
            # Obtener las cuentas más grandes
            largest = await client.get_token_largest_accounts(pubkey)
            await client.close()
            
            if not largest.value or len(largest.value) == 0:
                return {"passed": False, "reason": "No holders found"}
            
            # Obtener supply total
            supply_info = await self.rpc_pool.get_client().get_token_supply(pubkey)
            total_supply = float(supply_info.value.ui_amount or 0)
            
            if total_supply == 0:
                return {"passed": False, "reason": "Total supply is 0"}
            
            # Calcular porcentaje del holder más grande
            top_holder_amount = float(largest.value[0].ui_amount or 0)
            top_holder_percent = (top_holder_amount / total_supply) * 100
            
            if top_holder_percent > self.max_holder_percent:
                return {
                    "passed": False,
                    "reason": f"Top holder has {top_holder_percent:.1f}% (max: {self.max_holder_percent}%)"
                }
            
            return {
                "passed": True,
                "reason": f"Top holder: {top_holder_percent:.1f}% ✓",
                "top_holder_percent": top_holder_percent
            }
            
        except Exception as e:
            return {"passed": False, "reason": f"Error: {str(e)[:50]}"}
    
    async def _check_liquidity(self, liquidity_sol: Optional[float]) -> Dict:
        """Check 4: Verificar liquidez mínima"""
        try:
            if liquidity_sol is None:
                return {"passed": True, "reason": "Liquidity check skipped (no data)"}
            
            if liquidity_sol < self.min_liquidity_sol:
                return {
                    "passed": False,
                    "reason": f"Low liquidity: {liquidity_sol:.2f} SOL (min: {self.min_liquidity_sol})"
                }
            
            return {
                "passed": True,
                "reason": f"Liquidity: {liquidity_sol:.2f} SOL ✓",
                "liquidity_sol": liquidity_sol
            }
            
        except Exception as e:
            return {"passed": False, "reason": f"Error: {str(e)[:50]}"}
    
    async def _check_creator_history(self, creator_address: str) -> Dict:
        """Check 5: Verificar historial del creador"""
        try:
            if not creator_address:
                return {"passed": True, "reason": "Creator check skipped"}
            
            client = self.rpc_pool.get_client()
            pubkey = Pubkey.from_string(creator_address)
            
            # Obtener últimas transacciones
            sigs = await client.get_signatures_for_address(pubkey, limit=20)
            await client.close()
            
            if not sigs.value or len(sigs.value) < 3:
                # Wallet muy nueva o sin actividad - sospechoso
                return {
                    "passed": False,
                    "reason": "Creator wallet has very few transactions (suspicious)"
                }
            
            # Contar cuántas transacciones son muy recientes (últimas 24h)
            import time
            recent_count = sum(1 for sig in sigs.value if sig.block_time and (time.time() - sig.block_time) < 86400)
            
            if recent_count > 10:
                # Demasiada actividad en 24h - posible spammer
                return {
                    "passed": False,
                    "reason": f"Creator has {recent_count} tx in 24h (possible spammer)"
                }
            
            return {
                "passed": True,
                "reason": f"Creator history: {len(sigs.value)} tx, {recent_count} recent ✓"
            }
            
        except Exception as e:
            # Si falla este check, no es crítico
            return {"passed": True, "reason": f"Creator check failed (non-critical): {str(e)[:50]}"}
    
    async def _skip_check(self, check_name: str) -> Dict:
        """Saltar un check (considerarlo pasado)"""
        return {"passed": True, "reason": f"{check_name} skipped"}
