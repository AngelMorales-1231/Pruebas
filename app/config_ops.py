"""Configuración propia de la consola interna (variables de entorno OPS_*).

Vive en un BaseSettings separado para que el drop-in no obligue a editar
`config.py`. Mismo mecanismo (.env soportado); prefijo OPS_ para no colisionar.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings


class OpsSettings(BaseSettings):
    # --- Finanzas (§7). CAC y margen NO son derivables de la BD: los fija el
    # equipo. Con cac_clp = 0 el endpoint devuelve ltv_cac = null.
    ops_valor_uf: float = 39000.0          # reemplazar por fuente diaria (SII/CMF)
    ops_cac_clp: float = 0.0
    ops_margen_bruto_pct: float = 80.0
    ops_objetivo_churn_pct: float = 2.0

    # --- Umbrales del detector de incidentes (§8.1)
    ops_umbral_p95_ms: int = 800
    ops_umbral_cola: int = 500
    ops_fallos_para_caida: int = 3         # heartbeats consecutivos fallidos
    ops_sanos_para_monitoreo: int = 5      # heartbeats sanos -> monitoreando
    ops_min_para_resolver: int = 30        # minutos sanos -> resuelto
    ops_min_antirebote: int = 10           # espera tras cerrar antes de reabrir

    # --- Heartbeats
    ops_heartbeat_ttl_s: int = 300         # sin latido en 5 min => caído (ciego)
    ops_ventana_dias_default: int = 30

    class Config:
        env_file = ".env"


ops_settings = OpsSettings()

# Componentes en el orden del wireframe; el seed de 07 debe coincidir.
COMPONENTES = ("api", "db", "workers", "storage", "ia", "integraciones")
