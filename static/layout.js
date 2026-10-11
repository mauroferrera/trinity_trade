/* ============================================================
   Layout del dashboard: modo FOCO EN GRÁFICO.
   - Al cargar, los paneles secundarios vienen colapsados y el
     gráfico queda protagonista (se pueden abrir uno a uno).
   - Cada panel tiene chevron para colapsar/expandir.
   - Estado persistente en localStorage["dashboard_layout_v1"].
   - Emite "layoutchange" tras cualquier cambio que afecte geometrías
     (main.js lo usa para llamar chart.resize()/ofChart.resize()/resChartObj.resize()).
   ============================================================ */
(function () {
  "use strict";

  const KEY = "dashboard_layout_v1";
  const CHAT_MIN = 300;
  const CHAT_MAX = 560;
  const CHAT_DEFAULT = 380;

  // Paneles por defecto colapsados (todo menos el gráfico).
  const DEFAULT_COLLAPSED = [
    "research",
    "orderflow",
    "news",
    "positions",
    "history",
    "db",
  ];

  const clamp = (v, min, max) => Math.min(max, Math.max(min, v));

  function defaults() {
    const collapsed = {};
    DEFAULT_COLLAPSED.forEach((id) => (collapsed[id] = true));
    return { collapsed, chatWidth: CHAT_DEFAULT };
  }

  function load() {
    try {
      const raw = JSON.parse(localStorage.getItem(KEY)) || {};
      const base = defaults();
      return {
        collapsed:
          raw.collapsed && typeof raw.collapsed === "object"
            ? { ...base.collapsed, ...raw.collapsed }
            : base.collapsed,
        chatWidth: clamp(raw.chatWidth || CHAT_DEFAULT, CHAT_MIN, CHAT_MAX),
      };
    } catch (_) {
      return defaults();
    }
  }

  function save() {
    localStorage.setItem(KEY, JSON.stringify(state));
  }

  const state = load();

  const els = {
    chat: () => document.getElementById("chatPanel"),
    resizer: () => document.getElementById("chatResizer"),
  };

  function panelEls() {
    return Array.from(
      document.querySelectorAll(".main [data-panel]")
    ).map((el) => ({ id: el.dataset.panel, el }));
  }

  /* ---------- colapso ---------- */

  function toggleCollapse(panel, force) {
    const next = force !== undefined ? force : !panel.classList.contains("collapsed");
    panel.classList.toggle("collapsed", next);
    state.collapsed[panel.dataset.panel] = next;
    save();
    emit();
  }

  function injectCollapseButtons() {
    panelEls().forEach(({ el }) => {
      if (el.querySelector(".panel-collapse")) return;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "panel-collapse";
      btn.title = "Colapsar / expandir panel";
      btn.setAttribute("aria-label", "Colapsar / expandir panel");
      btn.innerHTML =
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>';
      const head = el.querySelector(".panel-head");
      if (head) head.appendChild(btn);
    });
  }

  function applyCollapsed() {
    panelEls().forEach(({ id, el }) => {
      el.classList.toggle("collapsed", Boolean(state.collapsed[id]));
    });
  }

  /* ---------- chat redimensionable ---------- */

  function initResizer() {
    const handle = els.resizer();
    if (!handle) return;
    handle.addEventListener("pointerdown", (e) => {
      if (window.innerWidth <= 1024) return;
      e.preventDefault();
      const startX = e.clientX;
      const startW = state.chatWidth;
      handle.setPointerCapture(e.pointerId);
      const onMove = (ev) => {
        state.chatWidth = clamp(startW + (startX - ev.clientX), CHAT_MIN, CHAT_MAX);
        document.documentElement.style.setProperty("--chat-w", state.chatWidth + "px");
      };
      const onUp = (ev) => {
        handle.removeEventListener("pointermove", onMove);
        handle.removeEventListener("pointerup", onUp);
        handle.removeEventListener("pointercancel", onUp);
        save();
        emit();
      };
      handle.addEventListener("pointermove", onMove);
      handle.addEventListener("pointerup", onUp);
      handle.addEventListener("pointercancel", onUp);
    });
  }

  function applyChatWidth() {
    document.documentElement.style.setProperty("--chat-w", state.chatWidth + "px");
  }

  /* ---------- eventos / arranque ---------- */

  function emit() {
    document.dispatchEvent(
      new CustomEvent("layoutchange", {
        detail: {
          collapsed: panelEls()
            .filter(({ el }) => el.classList.contains("collapsed"))
            .map(({ id }) => id),
          chatWidth: state.chatWidth,
        },
      })
    );
  }

  function wire() {
    document.addEventListener("click", (e) => {
      const cb = e.target.closest(".panel-collapse");
      if (cb) {
        const panel = cb.closest("[data-panel]");
        if (panel) toggleCollapse(panel);
      }
    });
  }

  function init() {
    if (!document.body || !document.querySelector(".main")) return;
    injectCollapseButtons();
    applyChatWidth();
    applyCollapsed();
    wire();
    initResizer();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  window.Layout = {
    getState: () => ({ ...state }),
    toggleCollapse,
  };
})();