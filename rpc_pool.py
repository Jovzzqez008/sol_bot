#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🔄 RPC POOL MANAGER - Multi-RPC con Load Balancing
===================================================
Gestiona múltiples RPCs gratuitos para evitar límites
"""

import os
import random
import asyncio
import logging
from typing import List, Optional
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed, Processed

logger = logging.getLogger(__name__)

class RPCPool:
    """Pool de múltiples RPCs con balanceo de carga"""
    
    def __init__(self):
        self.rpc_urls = self._load_rpc_urls()
        self.current_index = 0
        self.health_status = {url: True for url in self.rpc_urls}
        
        if not self.rpc_urls:
            raise ValueError("❌ No se encontraron URLs de RPC en variables de entorno")
        
        logger.info(f"✅ RPC Pool inicializado con {len(self.rpc_urls)} proveedores")
    
    def _load_rpc_urls(self) -> List[str]:
        """Cargar URLs de RPCs desde variables de entorno"""
        urls = []
        
        # Intentar cargar cada RPC
        rpc_keys = [
            'HELIUS_RPC_URL',
            'QUICKNODE_RPC_URL',
            'TRITON_RPC_URL',
            'ALCHEMY_RPC_URL'
        ]
        
        for key in rpc_keys:
            url = os.getenv(key)
            if url:
                urls.append(url)
                provider = key.split('_')[0]
                logger.info(f"  ✓ {provider} RPC cargado")
        
        # Fallback al RPC público si no hay ninguno
        if not urls:
            logger.warning("⚠️ No se encontraron RPCs personalizados, usando público")
            urls.append('https://api.mainnet-beta.solana.com')
        
        return urls
    
    def get_client(self, random_selection: bool = True) -> AsyncClient:
        """
        Obtener un cliente RPC del pool
        
        Args:
            random_selection: Si True, selecciona aleatoriamente. Si False, usa round-robin
        """
        if random_selection:
            # Selección aleatoria para distribuir mejor la carga
            url = random.choice(self.rpc_urls)
        else:
            # Round-robin (uno tras otro)
            url = self.rpc_urls[self.current_index]
            self.current_index = (self.current_index + 1) % len(self.rpc_urls)
        
        return AsyncClient(url, commitment=Confirmed)
    
    def get_all_clients(self) -> List[AsyncClient]:
        """Obtener un cliente para cada RPC (para operaciones paralelas)"""
        return [AsyncClient(url, commitment=Confirmed) for url in self.rpc_urls]
    
    async def parallel_call(self, method_name: str, *args, **kwargs):
        """
        Ejecutar una llamada en todos los RPCs en paralelo
        Retorna el primer resultado exitoso
        """
        clients = self.get_all_clients()
        
        async def call_rpc(client: AsyncClient, idx: int):
            try:
                method = getattr(client, method_name)
                result = await method(*args, **kwargs)
                return (idx, result, None)
            except Exception as e:
                return (idx, None, e)
            finally:
                await client.close()
        
        # Lanzar todas las llamadas en paralelo
        tasks = [call_rpc(client, idx) for idx, client in enumerate(clients)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Retornar el primer resultado exitoso
        for idx, result, error in results:
            if result is not None and error is None:
                return result
        
        # Si todos fallaron, lanzar el último error
        raise Exception(f"Todas las llamadas a {method_name} fallaron")
    
    def mark_unhealthy(self, url: str):
        """Marcar un RPC como no saludable temporalmente"""
        if url in self.health_status:
            self.health_status[url] = False
            logger.warning(f"⚠️ RPC marcado como unhealthy: {url[:50]}...")
    
    def get_healthy_urls(self) -> List[str]:
        """Obtener solo las URLs saludables"""
        return [url for url, healthy in self.health_status.items() if healthy]
    
    async def health_check(self):
        """Verificar salud de todos los RPCs"""
        logger.info("🏥 Verificando salud de RPCs...")
        
        for url in self.rpc_urls:
            try:
                client = AsyncClient(url, commitment=Confirmed)
                # Intentar obtener la versión (llamada ligera)
                version = await asyncio.wait_for(client.get_version(), timeout=5.0)
                
                if version:
                    self.health_status[url] = True
                    provider = url.split('//')[1].split('.')[0]
                    logger.info(f"  ✓ {provider}: OK")
                else:
                    self.health_status[url] = False
                
                await client.close()
                
            except Exception as e:
                self.health_status[url] = False
                provider = url.split('//')[1].split('.')[0] if '//' in url else 'unknown'
                logger.warning(f"  ✗ {provider}: FAIL ({str(e)[:50]})")
        
        healthy_count = sum(1 for h in self.health_status.values() if h)
        logger.info(f"✅ RPCs saludables: {healthy_count}/{len(self.rpc_urls)}")
