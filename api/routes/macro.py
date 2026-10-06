"""Contexto macro: el sesgo del COT y su refresco.

Por que un router propio y no una linea en `health.py`: son rutas que tocan datos
de un TERCERO (la CFTC) y no el broker. El `GET /api/cot/report` es una lectura de
la tabla, asi que podria vivir en `journal.py`, pero repetir la palabra "macro" en
el grupo de tags y en el nombre del modulo cuesta menos que la confusion de buscar
el COT entre "base de datos y bitacora".

Las dos reglas que sigo, que son las de REF y estan justificadas ahi:

1. **`GET /api/cot/report` es la tabla, no la red.** Se construye con
   `store.list_cot_reports()` y `cot_service.build_report`, que es PURO. Si el
   panel de sesgo hizo una peticion a la CFTC en cada refresco de pagina, cada
   refresco seria un rate-limit esperando a ocurrir.
2. **`POST /api/cot/refresh` es el unico que sale a la red**, y por eso lleva token.
   Un cliente que refresca el COT cada 10 s es un 403 esperando a ocurrir.
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query

from .. import deps

router = APIRouter(tags=["macro"])


def _reporte_desde_tabla(store: Any, limit: int) -> Dict[str, Any]:
    """El sesgo derivado de lo guardado, o 404 con el motivo si no hay nada.

    `limit` son FILAS de la tabla, no semanas de indice: el indice del COT mira 26
    semanas (`cot_service.INDEX_WINDOW`) y la tabla trae una fila por semana, asi
    que 60 filas cubren el indice con holgura. Fijar el minimo en 26 en la query
    hace que un `limit=5` no produzca un "indice" calculado sobre cinco semanas
    apresentado como si fuera el de un ano.
    """
    from macro_ingestor.forex import cot_service

    filas: List[Dict[str, Any]] = store.list_cot_reports(limit=limit)
    reporte = cot_service.build_report(filas)
    if reporte is None:
        raise HTTPException(
            status_code=404,
            detail="sin reportes del COT guardados (pulsa refrescar para descargarlos)",
        )
    return reporte


@router.get("/api/cot/report", response_model=None)
def cot_report(
    limit: int = Query(60, ge=26, le=520),
    store: Any = Depends(deps.store),
) -> Dict[str, Any]:
    """El sesgo del COT de EUR, derivado de lo que hay en la base.

    404 con el motivo cuando no hay reportes: "la CFTC no ha publicado" y "todavia
    no has refrescado" son el mismo 404 para el cliente y lo que los distingue es el
    texto. Un `200 {"bias": "NEUTRAL"}` con la base vacia seria un sesgo neutro
    INVENTADO, que es justo lo que `build_report` nunca hace.
    """
    return _reporte_desde_tabla(store, limit)


@router.post("/api/cot/refresh", response_model=None)
def cot_refresh(
    limit: int = Query(60, ge=26, le=520),
    store: Any = Depends(deps.store),
    _auth: None = Depends(deps.require_api_token),
) -> Dict[str, Any]:
    """Descarga el COT, lo persiste y devuelve el reporte recien calculado.

    El orden es descargar -> guardar -> releer de la base -> derivar, y no
    "descargar y derivar en memoria": lo derivado se guarda CON su indice y su
    sesgo, para que un lector posterior (el panel, el score) vea el mismo numero
    aunque la descarga de manana falle. Si se derivara solo en memoria, la tabla
    guardaria la fila cruda y dos visores darian dos sesgos para la misma fecha.

    El indice y el sesgo se escriben solo en la fila MAS RECIENT, que es la unica
    que `build_report` deriva. Las demas se guardan crudas a proposito: inventar un
    indice por fila exigiria un `build_report` por fila, con su propia ventana, y el
    numero de una ventana movil no es el numero de la serie completa. Escribirlo
    dejaria en la base cifras que ninguna recomputacion reproduce.
    """
    from macro_ingestor.forex import cot_service

    crudos = cot_service.fetch_reports(limit=limit)
    if crudos:
        reporte = cot_service.build_report(crudos)
        ultima = (reporte or {}).get("report_date")
        for fila in crudos:
            vigente = reporte if fila.get("report_date") == ultima else None
            store.upsert_cot_report(
                fila["report_date"],
                fila["am_net"],
                fila["lf_net"],
                fila["nc_net"],
                cot_index=(vigente or {}).get("cot_index_26w"),
                macro_bias=(vigente or {}).get("macro_bias"),
                delta_am=(vigente or {}).get("delta_asset_managers"),
                delta_lf=(vigente or {}).get("delta_leveraged_funds"),
            )
    return _reporte_desde_tabla(store, limit)


__all__ = ["router"]