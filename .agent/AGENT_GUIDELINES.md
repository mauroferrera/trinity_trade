# AGENT_GUIDELINES.md - Reglas Indiscutibles de Desarrollo

## Principios Arquitectónicos

> **Código de referencia (solo lectura):** `C:/Users/fmaur/Desktop/Trading`
> En el resto de los documentos `REF/` significa esa ruta. `REF/` está **fuera** de este repo y no se importa ni se modifica.


1. **Arquitectura Agnóstica al Mercado.** Todo motor ubicado en `core/` (`risk_engine`, `smc_engine`, `orderflow_engine`, `simulator`, `lot_calculator`) DEBE ser **100% agnóstico al activo/mercado**. No debe importar adaptadores, MT5, Databento, Binance u otros feeds de datos.

2. **Separación Estricta de Responsabilidades.** Núcleo = **lógica pura (determinista)**. I/O, conectores, websockets, requests y acceso a APIs viven **exclusivamente en `adapters/`, `macro_ingestor/`, `api/`**.

3. **Principio de Inversión de Dependencias.** Los adaptadores DEBEN implementar interfaces definidas en `adapters/base_adapter.py`. El `core/` recibe datos **normalizados** (OHLC/Ticks/Depth), nunca objetos específicos del proveedor. Usar **Dependency Injection** cuando sea necesario.

4. **Lógica de Negocio Fuera de la API.** Los endpoints en `api/routes/` DEBEN mantenerse **delgados**. Toda lógica de negocio, cálculo o validación va a `core/` o servicios. `api/app.py` solo debe encargarse de bootstrap, middlewares, mounts y registro de rutas.

5. **Monorepo Modular.** Un único repositorio para todos los mercados (B3, Forex/CME, Cripto). No crear repos separados. La activación dinámica de fuentes se realiza vía `config/asset_sources_map.yaml`.

## Reglas de Desarrollo y Progreso

6. **Tests Verdes ANTES de Marcar Completado.** **Regla innegociable.** Ningún módulo puede marcarse como `completed` en `.agent/PROJECT_STATE.json` mientras **no pasen todos los tests relevantes (`pytest`) en verde**. Tras migrar/crear código, ejecutar suite correspondiente y validar regresión.

7. **Port Selectivo + No Big-Bang.** Mantener `REF/` **intacto y en solo lectura** como referencia durante toda la reconstrucción. No parchear el monolito existente. Construir estructura limpia (Fase 0) y portar **lógica pura selectivamente** (priorizar **Fase 1 - core/**). Extraer `app.py` por **extracción incremental por dominios**, nunca big-bang destructivo.

8. **Preservar Código de Alto Valor.** Al portar desde `REF/`, **reutilizar lógica interna**, no reescribir por capricho. Documentar cambios mínimos necesarios para desacoplar (imports, normalización de interfaces).

9. **Rutas Únicas y Absolutas.** Respetar `core/paths.py` como **fuente única de verdad** de rutas. Usar rutas absolutas (nunca relativas al CWD). No crear segundas instancias de `trading.db` por cambio de directorio de ejecución.

10. **Base de Datos - Integridad.** No alterar `database/models.py` sin evaluar impacto/migración en `database/store.py`. Usar UPSERT cuando corresponda. Mantener coherencia con esquema existente si se reutiliza `trading.db`.

## Reglas Operativas del Agente

11. **Reanudación Obligatoria entre Sesiones.** Al iniciar cualquier nueva sesión (tras reiniciar PC), **SIEMPRE leer primero**: `.agent/PROJECT_STATE.json` → `.agent/AGENT_GUIDELINES.md` → `.agent/PLAN.md`. Con eso reanuda exactamente en el punto correcto, sin volver a analizar todo desde cero.

12. **Actualizar Estado en Tiempo Real.** Cada tarea completada debe actualizar: `[x]` en `.agent/CHECKLIST.md` y mover módulos entre `completed/in_progress/pending` en `.agent/PROJECT_STATE.json`. Actualizar `last_updated` al completar hitos relevantes.

13. **Decisiones Persistentes.** Toda respuesta a las clarificaciones (sección 6 de PLAN.md) debe registrarse en `.agent/DECISIONS.md`. **Nunca volver a preguntar lo mismo** entre sesiones.

14. **Minimalismo y Profesionalidad.** Mantener mismo rigor matemático/determinista del proyecto original. Código limpio, sin comentarios innecesarios (seguir estilo existente). No añadir complejidad sin justificación.

15. **Validación Continua.** Tras cada fase/migración crítica, ejecutar `pytest` sobre tests relevantes. Verificar integración end-to-end con **datos mock** (evitar dependencias MT5 en tests unitarios).

## Convenciones de Código

16. **Core Puro.** `core/*` no debe tener imports de `adapters`, `api`, `agent` (flujo unidireccional: adaptadores → normalizan → core consume).

17. **Adaptadores por Mercado.** Cada mercado en su carpeta: `adapters/b3/`, `adapters/forex/`, `adapters/crypto/`. Implementar `base_adapter.BaseAdapter` (o interfaz abstracta). Encapsular locks/concurrencia del proveedor dentro del adaptador.

18. **Configuración Dinámica.** `config/asset_sources_map.yaml` orquesta qué fuentes activan por activo/mercado. **No reemplaza** `strategy.yaml` (reglas/estrategia). Se complementan.

19. **Explicabilidad vs Decisión.** `agent/` es capa de **explicabilidad e interpretación** (traduce datos a lenguaje natural, auditoría post-mortem). **Nunca debe calcular entradas/SL/TP en tiempo real** para ejecución (esa responsabilidad queda en `core/risk_engine.py` + backend determinista).
