# MEJORAS_DASHBOARD_LAYOUT.md - Sistema de layout del dashboard

Registro de **por qué** el dashboard arranca en modo foco en el gráfico y cómo
funciona su layout. Implementado en `static/layout.js` (nuevo), con cambios en
`static/index.html`, `static/style.css` y `static/main.js`. Decisiones: D-080
(control de layout) y D-081 (simplificación a solo modo foco).

Entregado: **2026-10-09** (revisado mismo día). Solo frontend (vanilla JS +
ECharts, sin build); el backend no se toca y la suite completa sigue en verde.

---

## 1. Qué hay ahora (modo foco en gráfico, el único)

- **Al cargar, todos los paneles secundarios vienen colapsados** (`research`,
  `orderflow`, `news`, `positions`, `history`, `db`); el gráfico queda
  protagonista. Se pueden abrir uno a uno con el chevron de cada panel.
- Cada `.panel` tiene un botón de colapso (chevron en `.panel-head`); el estado
  de colapso se persiste en `localStorage["dashboard_layout_v1"]`.
- El chat lateral sigue redimensionable: asa `.chat-resizer` (Pointer Events),
  ancho por defecto `--chat-w: 380px` con clamp 300–560px y persistencia.
- Todo cambio de geometría dispara `CustomEvent("layoutchange")`; `main.js` lo
  usa para llamar `chart.resize()`, `ofChart.resize()` y `resChartObj.resize()`.

### Lo que se quitó (D-081)

El desplegable `#layoutPreset` del topbar y los presets `single`, `two-column` y
`tabs`. Antes de esta revisión el sistema tenia 4 presets y agrupación por
pestañas; la petición fue "dejar solo el layout de foco en gráfico", así que se
eliminaron el selector, el CSS de `two-column` y todo el bloque de `.tabbed-panels`
(no queda rastro en `index.html`, `layout.js` ni `style.css`).

## 2. Estado persistido

`localStorage["dashboard_layout_v1"]` = `{ collapsed: {panelId: bool}, chatWidth: 380..560 }`.

- Los paneles secundarios colapsan por defecto solo la primera vez; el merge con
  lo guardado respeta expansiones manuales posteriores.
- No hay nada sincronizado con el backend: es estado de UI únicamente.

## 3. Reglas de oro

- Mover/colapsar paneles es seguro porque `main.js` referencia los paneles por
  `id`/selectores, no por posición en el DOM.
- Si se añade otro chart ECharts, hay que sumar su `.resize()` al listener de
  `layoutchange` en `main.js`.
- El selector de panel es `.main [data-panel]` (ver `PANELS` por defecto en
  `DEFAULT_COLLAPSED` dentro de `layout.js`).

## 4. Verificación

1. `uvicorn api.app:app --reload --port 8000` y abrir `/` → los paneles
   secundarios abren colapsados y el gráfico destaca.
2. Expandir/colapsar paneles y recargar → el estado se mantiene.
3. Arrastrar el asa del chat (solo ≥1024px) → el ancho persiste tras recarga.
4. Harness JS (dom-shim en `layout_test.js`): 14 asserts OK.
5. Suite: `pytest` (1764 passed, 2 xfailed) — solo se tocó estática.