(() => {
  if (window.__CPECC_CHAT_WIDGET_LOADER__) {
    return;
  }
  window.__CPECC_CHAT_WIDGET_LOADER__ = true;

  const config = window.CPECC_CHAT_WIDGET || {};
  const currentScript = document.currentScript;
  const fallbackScriptUrl = new URL(
    config.baseUrl || config.appBaseUrl || '/embed/iframe-loader.js',
    window.location.href
  ).toString();
  const scriptUrl = (() => {
    try {
      return new URL(currentScript && currentScript.src ? currentScript.src : fallbackScriptUrl, window.location.href);
    } catch {
      return new URL(fallbackScriptUrl);
    }
  })();

  const isWebUrl = (url) => url.protocol === 'https:' || url.protocol === 'http:';
  const getAppBaseUrl = () => {
    const embedSuffix = '/embed/iframe-loader.js';
    const pathname = scriptUrl.pathname.endsWith(embedSuffix)
      ? scriptUrl.pathname.slice(0, -embedSuffix.length)
      : '';
    return new URL(pathname ? `${pathname}/` : '/', scriptUrl).toString();
  };
  const appBaseUrl = getAppBaseUrl();

  const toFiniteNumber = (value, fallback) => {
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  };

  const toWebUrl = (value, fallback) => {
    let fallbackUrl;
    try {
      fallbackUrl = new URL(fallback, scriptUrl);
    } catch {
      fallbackUrl = new URL(fallbackScriptUrl);
    }
    const safeFallback = isWebUrl(fallbackUrl) ? fallbackUrl.toString() : window.location.origin;
    if (!value) return safeFallback;

    try {
      const url = new URL(value, scriptUrl);
      return isWebUrl(url) ? url.toString() : safeFallback;
    } catch {
      return safeFallback;
    }
  };

  const iframeUrl = toWebUrl(config.iframeUrl || config.src, appBaseUrl);
  const hasCustomLogoUrl = typeof config.logoUrl === 'string' && config.logoUrl.trim() !== '';
  let homeUrl = toWebUrl(config.homeUrl, appBaseUrl);
  const logoUrl = toWebUrl(config.logoUrl, new URL('static/logo.png', appBaseUrl).toString());
  const zIndex = toFiniteNumber(config.zIndex, 2147483647);
  const desktopBreakpoint = toFiniteNumber(config.desktopBreakpoint, 860);
  const minPanelWidth = toFiniteNumber(config.minWidth, 360);
  const minPanelHeight = toFiniteNumber(config.minHeight, 360);
  const defaultWidthRatio = toFiniteNumber(config.widthRatio, 0.4);
  const mobileWidthRatio = toFiniteNumber(config.mobileWidthRatio, 0.92);
  const defaultHeightRatio = toFiniteNumber(config.heightRatio, 1);
  const fallbackLogoWidth = Math.max(24, toFiniteNumber(config.launcherWidth || config.logoWidth, 68));
  const fallbackLogoHeight = Math.max(24, toFiniteNumber(config.launcherHeight || config.logoHeight, 68));
  const logoPath = (() => {
    try {
      return new URL(logoUrl, scriptUrl).pathname.toLowerCase();
    } catch {
      return '';
    }
  })();
  const launcherImageMode = String(config.launcherImageMode || 'auto').toLowerCase();
  const useRawLauncherImage =
    config.launcherBare === true ||
    launcherImageMode === 'raw' ||
    launcherImageMode === 'bare' ||
    (launcherImageMode === 'auto' && hasCustomLogoUrl && logoPath.endsWith('.gif'));
  const viewportPadding = 16;

  const readConfiguredHomeUrl = (payload) => {
    const candidates = [
      payload?.sso?.home_url,
      payload?.oauth?.enterprise_home_url,
      payload?.oauth?.enterprise?.home_url
    ];
    return candidates.find((value) => typeof value === 'string' && value.trim()) || null;
  };

  const mount = () => {
    if (!document.body || document.getElementById('cpecc-chat-widget-loader-root')) {
      return;
    }

    const host = document.createElement('div');
    host.id = 'cpecc-chat-widget-loader-root';
    host.style.position = 'fixed';
    host.style.inset = '0';
    host.style.zIndex = String(zIndex);
    host.style.pointerEvents = 'none';
    document.body.appendChild(host);

    const shadow = host.attachShadow({ mode: 'open' });
    const style = document.createElement('style');
    style.textContent = `
      :host {
        all: initial;
        --cpecc-control-bg: #ffffff;
        --cpecc-brand: #1f5fe8;
        --cpecc-ink: #334155;
        --cpecc-panel-bg: #ffffff;
        --cpecc-shadow: 0 30px 90px rgba(15, 23, 42, 0.2);
      }

      *, *::before, *::after {
        box-sizing: border-box;
      }

      .cpecc-widget-root {
        position: fixed;
        inset: 0;
        z-index: ${zIndex};
        pointer-events: none;
        font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      }

      .cpecc-launcher {
        position: fixed;
        left: 0;
        top: 0;
        z-index: 20;
        display: flex;
        width: ${fallbackLogoWidth}px;
        height: ${fallbackLogoHeight}px;
        align-items: center;
        justify-content: center;
        border: 0;
        border-radius: 50%;
        background: rgba(255, 255, 255, 0.9);
        box-shadow: 0 16px 44px rgba(15, 23, 42, 0.18);
        color: var(--cpecc-ink);
        cursor: grab;
        padding: 0;
        pointer-events: auto;
        touch-action: none;
        transition: box-shadow 180ms ease, opacity 180ms ease;
        user-select: none;
        will-change: transform;
      }

      .cpecc-launcher.is-image-raw {
        overflow: visible;
        border-radius: 0;
        background: transparent;
        box-shadow: none;
      }

      .cpecc-launcher:hover,
      .cpecc-launcher:focus-visible {
        box-shadow: 0 20px 54px rgba(31, 95, 232, 0.22);
        outline: none;
        transform: translate3d(var(--launcher-x, 0), var(--launcher-y, 0), 0) scale(1.02);
      }

      .cpecc-launcher.is-image-raw:hover,
      .cpecc-launcher.is-image-raw:focus-visible {
        box-shadow: none;
      }

      .cpecc-launcher:active {
        cursor: grabbing;
      }

      .cpecc-launcher.is-hidden {
        opacity: 0;
        pointer-events: none;
      }

      .cpecc-logo-disc {
        display: grid;
        width: min(52px, 76%);
        height: min(52px, 76%);
        flex: 0 0 auto;
        place-items: center;
        border-radius: 50%;
        background: var(--cpecc-control-bg);
        box-shadow: none;
        pointer-events: none;
      }

      .cpecc-launcher.is-image-raw .cpecc-logo-disc {
        width: 100%;
        height: 100%;
        border-radius: 0;
        background: transparent;
      }

      .cpecc-logo-disc img {
        display: block;
        width: min(38px, 56%);
        height: min(38px, 56%);
        object-fit: contain;
        pointer-events: none;
        user-select: none;
        -webkit-user-drag: none;
      }

      .cpecc-launcher.is-image-raw .cpecc-logo-disc img {
        width: 100%;
        height: 100%;
      }

      .cpecc-panel {
        position: fixed;
        z-index: 30;
        display: grid;
        grid-template-rows: auto minmax(0, 1fr);
        overflow: hidden;
        border: 0;
        border-radius: 24px;
        background: var(--cpecc-panel-bg);
        box-shadow: var(--cpecc-shadow);
        opacity: 0;
        pointer-events: none;
        transform-origin: center center;
        backdrop-filter: blur(20px);
        will-change: left, top, width, height, transform;
      }

      .cpecc-panel.is-open {
        opacity: 1;
        pointer-events: auto;
      }

      .cpecc-toolbar {
        position: relative;
        z-index: 4;
        display: flex;
        min-height: 50px;
        align-items: center;
        justify-content: space-between;
        gap: 18px;
        border-bottom: 0;
        background: linear-gradient(180deg, rgba(255, 255, 255, 0.72), rgba(248, 250, 252, 0.56));
        cursor: grab;
        padding: 4px 12px;
        touch-action: none;
        user-select: none;
      }

      .cpecc-toolbar:active {
        cursor: grabbing;
      }

      .cpecc-actions {
        position: relative;
        z-index: 8;
        display: flex;
        align-items: center;
        flex: 0 0 auto;
        gap: 18px;
      }

      .cpecc-actions.left {
        margin-left: 4px;
      }

      .cpecc-actions.right {
        margin-right: 4px;
      }

      .cpecc-action {
        appearance: none;
        display: grid;
        width: 40px;
        height: 40px;
        place-items: center;
        border: 0;
        border-radius: 50%;
        background: var(--cpecc-control-bg);
        box-shadow: none;
        color: var(--cpecc-ink);
        cursor: pointer;
        font: inherit;
        padding: 0;
        pointer-events: auto;
        transition: background 160ms ease, color 160ms ease;
      }

      .cpecc-action:hover,
      .cpecc-action:focus-visible {
        background: rgba(31, 95, 232, 0.1);
        color: var(--cpecc-brand);
        outline: none;
      }

      .cpecc-action svg {
        width: 22px;
        height: 22px;
        stroke: currentColor;
        stroke-width: 1.9;
      }

      .cpecc-frame {
        width: 100%;
        height: 100%;
        border: 0;
        background: #ffffff;
      }

      .cpecc-resize-handle {
        position: absolute;
        z-index: 5;
        background: transparent;
        pointer-events: auto;
        touch-action: none;
      }

      .cpecc-resize-handle[data-resize-handle="top-left"] {
        left: 0;
        top: 0;
        width: 12px;
        height: 12px;
        cursor: nwse-resize;
      }

      .cpecc-resize-handle[data-resize-handle="top"] {
        left: 14px;
        right: 14px;
        top: 0;
        height: 6px;
        cursor: ns-resize;
      }

      .cpecc-resize-handle[data-resize-handle="top-right"] {
        right: 0;
        top: 0;
        width: 12px;
        height: 12px;
        cursor: nesw-resize;
      }

      .cpecc-resize-handle[data-resize-handle="right"] {
        right: 0;
        top: 14px;
        bottom: 14px;
        width: 8px;
        cursor: ew-resize;
      }

      .cpecc-resize-handle[data-resize-handle="bottom-right"] {
        right: 0;
        bottom: 0;
        width: 12px;
        height: 12px;
        cursor: nwse-resize;
      }

      .cpecc-resize-handle[data-resize-handle="bottom"] {
        left: 14px;
        right: 14px;
        bottom: 0;
        height: 8px;
        cursor: ns-resize;
      }

      .cpecc-resize-handle[data-resize-handle="bottom-left"] {
        left: 0;
        bottom: 0;
        width: 12px;
        height: 12px;
        cursor: nesw-resize;
      }

      .cpecc-resize-handle[data-resize-handle="left"] {
        left: 0;
        top: 14px;
        bottom: 14px;
        width: 8px;
        cursor: ew-resize;
      }

      .cpecc-widget-root.is-interacting,
      .cpecc-widget-root.is-interacting * {
        user-select: none;
      }

      .cpecc-widget-root.is-interacting .cpecc-frame {
        pointer-events: none;
      }

      .cpecc-widget-root.is-interacting .cpecc-launcher,
      .cpecc-widget-root.is-interacting .cpecc-panel {
        transition: none !important;
      }
    `;

    const root = document.createElement('div');
    root.className = 'cpecc-widget-root';
    root.innerHTML = `
      <button class="cpecc-launcher${useRawLauncherImage ? ' is-image-raw' : ''}" type="button" aria-label="Open 中电慧语 iframe widget">
        <span class="cpecc-logo-disc"><img src="${logoUrl}" alt="" draggable="false"></span>
      </button>
      <section class="cpecc-panel" role="dialog" aria-label="中电慧语 iframe window" aria-hidden="true" inert>
        <header class="cpecc-toolbar">
          <div class="cpecc-actions left">
            <button class="cpecc-action cpecc-home" type="button" title="Open 中电慧语" aria-label="Open 中电慧语">
              <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <path d="M3 10.8 12 3l9 7.8" stroke-linecap="round" stroke-linejoin="round"></path>
                <path d="M5.5 10.2V21h13V10.2" stroke-linecap="round" stroke-linejoin="round"></path>
                <path d="M9.5 21v-6h5v6" stroke-linecap="round" stroke-linejoin="round"></path>
              </svg>
            </button>
          </div>
          <div class="cpecc-actions right">
            <button class="cpecc-action cpecc-refresh" type="button" title="Refresh" aria-label="Refresh">
              <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <path d="M20 12a8 8 0 1 1-2.35-5.65" stroke-linecap="round" stroke-linejoin="round"></path>
                <path d="M20 4v5h-5" stroke-linecap="round" stroke-linejoin="round"></path>
              </svg>
            </button>
            <button class="cpecc-action cpecc-retract" type="button" title="Retract" aria-label="Retract">
              <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <path d="m4.5 4.5 5 5" stroke-linecap="round" stroke-linejoin="round"></path>
                <path d="M9.5 5.5v4h-4" stroke-linecap="round" stroke-linejoin="round"></path>
                <path d="m19.5 19.5-5-5" stroke-linecap="round" stroke-linejoin="round"></path>
                <path d="M14.5 18.5v-4h4" stroke-linecap="round" stroke-linejoin="round"></path>
              </svg>
            </button>
          </div>
        </header>
        <iframe class="cpecc-frame" title="中电慧语" src="about:blank" allow="clipboard-read; clipboard-write; microphone; camera; fullscreen" referrerpolicy="strict-origin-when-cross-origin"></iframe>
        <div class="cpecc-resize-handle" data-resize-handle="top-left" aria-hidden="true"></div>
        <div class="cpecc-resize-handle" data-resize-handle="top" aria-hidden="true"></div>
        <div class="cpecc-resize-handle" data-resize-handle="top-right" aria-hidden="true"></div>
        <div class="cpecc-resize-handle" data-resize-handle="right" aria-hidden="true"></div>
        <div class="cpecc-resize-handle" data-resize-handle="bottom-right" aria-hidden="true"></div>
        <div class="cpecc-resize-handle" data-resize-handle="bottom" aria-hidden="true"></div>
        <div class="cpecc-resize-handle" data-resize-handle="bottom-left" aria-hidden="true"></div>
        <div class="cpecc-resize-handle" data-resize-handle="left" aria-hidden="true"></div>
      </section>
    `;

    shadow.append(style, root);

    const launcher = root.querySelector('.cpecc-launcher');
    const panel = root.querySelector('.cpecc-panel');
    const toolbar = root.querySelector('.cpecc-toolbar');
    const frame = root.querySelector('.cpecc-frame');
    const homeButton = root.querySelector('.cpecc-home');
    const refreshButton = root.querySelector('.cpecc-refresh');
    const retractButton = root.querySelector('.cpecc-retract');
    const resizeHandles = Array.from(root.querySelectorAll('.cpecc-resize-handle'));

    let isOpen = false;
    let launcherPosition = getDefaultLauncherPosition();
    let panelPosition = { x: 0, y: 0 };
    let panelSize = getDefaultPanelSize();
    let panelGeometryInitialized = false;
    let draggingLauncher = null;
    let draggingPanel = null;
    let resizingPanel = null;
    let previousCursor = null;

    async function loadConfiguredHomeUrl() {
      if (config.homeUrl || !window.fetch) return;

      try {
        const response = await fetch(new URL('api/config', appBaseUrl).toString(), {
          cache: 'no-store',
          credentials: 'omit'
        });
        if (!response.ok) return;

        const payload = await response.json();
        const configuredHomeUrl = readConfiguredHomeUrl(payload);
        if (configuredHomeUrl) {
          homeUrl = toWebUrl(configuredHomeUrl, appBaseUrl);
        }
      } catch {
        // The script URL origin remains the safe fallback when public config is unavailable.
      }
    }

    function clamp(value, min, max) {
      if (max < min) return min;
      return Math.min(Math.max(value, min), max);
    }

    function getDefaultPanelSize() {
      const widthRatio = window.innerWidth <= desktopBreakpoint ? mobileWidthRatio : defaultWidthRatio;
      return constrainSize({
        width: Math.round(window.innerWidth * widthRatio),
        height: Math.round(window.innerHeight * defaultHeightRatio)
      });
    }

    function constrainSize(size) {
      return {
        width: clamp(size.width, Math.min(minPanelWidth, window.innerWidth), window.innerWidth),
        height: clamp(size.height, Math.min(minPanelHeight, window.innerHeight), window.innerHeight)
      };
    }

    function getLauncherSize() {
      return {
        width: launcher.offsetWidth || fallbackLogoWidth,
        height: launcher.offsetHeight || fallbackLogoHeight
      };
    }

    function getDefaultLauncherPosition() {
      const launcherSize = getLauncherSize();
      return constrainLauncherPosition({
        x: window.innerWidth - launcherSize.width - 28,
        y: window.innerHeight - launcherSize.height - 28
      });
    }

    function constrainLauncherPosition(position) {
      const launcherSize = getLauncherSize();
      return {
        x: clamp(position.x, viewportPadding, window.innerWidth - launcherSize.width - viewportPadding),
        y: clamp(position.y, viewportPadding, window.innerHeight - launcherSize.height - viewportPadding)
      };
    }

    function constrainPanelPosition(position, size = panelSize) {
      return {
        x: clamp(position.x, 0, window.innerWidth - size.width),
        y: clamp(position.y, 0, window.innerHeight - size.height)
      };
    }

    function applyLauncherPosition() {
      launcher.style.setProperty('--launcher-x', `${launcherPosition.x}px`);
      launcher.style.setProperty('--launcher-y', `${launcherPosition.y}px`);
      launcher.style.transform = `translate3d(${launcherPosition.x}px, ${launcherPosition.y}px, 0)`;
    }

    function applyPanelGeometry() {
      panel.style.left = `${panelPosition.x}px`;
      panel.style.top = `${panelPosition.y}px`;
      panel.style.width = `${panelSize.width}px`;
      panel.style.height = `${panelSize.height}px`;
    }

    function getLogoCenter() {
      const launcherSize = getLauncherSize();
      return {
        x: launcherPosition.x + launcherSize.width / 2,
        y: launcherPosition.y + launcherSize.height / 2
      };
    }

    function getCenteredPanelPosition(size) {
      const origin = getLogoCenter();
      return constrainPanelPosition({ x: origin.x - size.width / 2, y: origin.y - size.height / 2 }, size);
    }

    function ensureIframeLoaded() {
      if (frame.getAttribute('src') === 'about:blank') {
        frame.setAttribute('src', iframeUrl);
      }
    }

    function animatePanel(opening, done) {
      const origin = getLogoCenter();
      const panelCenter = {
        x: panelPosition.x + panelSize.width / 2,
        y: panelPosition.y + panelSize.height / 2
      };
      const from = `translate3d(${origin.x - panelCenter.x}px, ${origin.y - panelCenter.y}px, 0) scale(0.08)`;
      const to = 'translate3d(0, 0, 0) scale(1)';
      const keyframes = opening
        ? [{ opacity: 0, transform: from }, { opacity: 1, transform: to }]
        : [{ opacity: 1, transform: to }, { opacity: 0, transform: from }];
      const animation = panel.animate(keyframes, {
        duration: opening ? 360 : 240,
        easing: opening ? 'cubic-bezier(.2,.9,.2,1)' : 'cubic-bezier(.4,0,1,1)',
        fill: 'forwards'
      });
      animation.onfinish = done;
    }

    function openWidget() {
      if (isOpen) return;
      isOpen = true;
      ensureIframeLoaded();
      if (!panelGeometryInitialized) {
        panelSize = getDefaultPanelSize();
        panelPosition = getCenteredPanelPosition(panelSize);
        panelGeometryInitialized = true;
      } else {
        panelSize = constrainSize(panelSize);
        panelPosition = constrainPanelPosition(panelPosition, panelSize);
      }
      applyPanelGeometry();
      panel.classList.add('is-open');
      panel.setAttribute('aria-hidden', 'false');
      panel.inert = false;
      launcher.classList.add('is-hidden');
      launcher.setAttribute('aria-hidden', 'true');
      launcher.inert = true;
      animatePanel(true, () => {
        panel.style.opacity = '1';
        panel.style.transform = 'none';
        retractButton.focus({ preventScroll: true });
      });
    }

    function retractWidget() {
      if (!isOpen) return;
      isOpen = false;
      animatePanel(false, () => {
        panel.classList.remove('is-open');
        panel.setAttribute('aria-hidden', 'true');
        panel.inert = true;
        panel.style.opacity = '';
        panel.style.transform = '';
        launcher.classList.remove('is-hidden');
        launcher.removeAttribute('aria-hidden');
        launcher.inert = false;
        launcher.focus({ preventScroll: true });
      });
    }

    function goHome() {
      const newTab = window.open(homeUrl, '_blank', 'noopener,noreferrer');
      if (newTab) {
        newTab.opener = null;
      }
    }

    function refreshIframe() {
      try {
        frame.contentWindow.location.reload();
      } catch {
        const currentSrc = frame.getAttribute('src') || iframeUrl;
        frame.setAttribute('src', currentSrc);
      }
    }

    function setWidgetInteraction(active, cursor = '') {
      root.classList.toggle('is-interacting', active);
      if (active) {
        if (!previousCursor) {
          previousCursor = {
            body: document.body.style.cursor,
            documentElement: document.documentElement.style.cursor
          };
        }
        document.body.style.cursor = cursor;
        document.documentElement.style.cursor = cursor;
        return;
      }
      if (previousCursor) {
        document.body.style.cursor = previousCursor.body;
        document.documentElement.style.cursor = previousCursor.documentElement;
        previousCursor = null;
      }
    }

    function stopWidgetInteractionIfIdle() {
      if (!draggingLauncher && !draggingPanel && !resizingPanel) {
        setWidgetInteraction(false);
      }
    }

    function cancelWidgetInteraction() {
      draggingLauncher = null;
      draggingPanel = null;
      resizingPanel = null;
      setWidgetInteraction(false);
    }

    function updateLauncherDrag(event) {
      if (!draggingLauncher || draggingLauncher.pointerId !== event.pointerId) return;
      const dx = event.clientX - draggingLauncher.startX;
      const dy = event.clientY - draggingLauncher.startY;
      if (Math.abs(dx) + Math.abs(dy) > 4) draggingLauncher.moved = true;
      launcherPosition = constrainLauncherPosition({
        x: draggingLauncher.originX + dx,
        y: draggingLauncher.originY + dy
      });
      applyLauncherPosition();
    }

    function finishLauncherDrag(event) {
      if (!draggingLauncher || draggingLauncher.pointerId !== event.pointerId) return;
      try {
        launcher.releasePointerCapture(event.pointerId);
      } catch {}
      const shouldOpen = !draggingLauncher.moved;
      draggingLauncher = null;
      stopWidgetInteractionIfIdle();
      if (shouldOpen) openWidget();
    }

    function updatePanelDrag(event) {
      if (!draggingPanel || draggingPanel.pointerId !== event.pointerId) return;
      panelPosition = constrainPanelPosition({
        x: draggingPanel.originX + event.clientX - draggingPanel.startX,
        y: draggingPanel.originY + event.clientY - draggingPanel.startY
      });
      applyPanelGeometry();
    }

    function finishPanelDrag(event) {
      if (!draggingPanel || draggingPanel.pointerId !== event.pointerId) return;
      try {
        toolbar.releasePointerCapture(event.pointerId);
      } catch {}
      draggingPanel = null;
      stopWidgetInteractionIfIdle();
    }

    function updatePanelResize(event) {
      if (!resizingPanel || resizingPanel.pointerId !== event.pointerId) return;
      const dx = event.clientX - resizingPanel.startX;
      const dy = event.clientY - resizingPanel.startY;
      const handle = resizingPanel.handle || '';
      const fromLeft = handle.includes('left');
      const fromRight = handle.includes('right');
      const fromTop = handle.includes('top');
      const fromBottom = handle.includes('bottom');
      const rightEdge = resizingPanel.startPanelX + resizingPanel.startWidth;
      const bottomEdge = resizingPanel.startPanelY + resizingPanel.startHeight;
      let nextX = fromLeft ? resizingPanel.startPanelX + dx : resizingPanel.startPanelX;
      let nextY = fromTop ? resizingPanel.startPanelY + dy : resizingPanel.startPanelY;
      let nextWidth = resizingPanel.startWidth;
      let nextHeight = resizingPanel.startHeight;

      if (fromLeft) {
        nextWidth = rightEdge - nextX;
      } else if (fromRight) {
        nextWidth = resizingPanel.startWidth + dx;
      }

      if (fromTop) {
        nextHeight = bottomEdge - nextY;
      } else if (fromBottom) {
        nextHeight = resizingPanel.startHeight + dy;
      }

      nextWidth = clamp(nextWidth, Math.min(minPanelWidth, window.innerWidth), window.innerWidth);
      nextHeight = clamp(nextHeight, Math.min(minPanelHeight, window.innerHeight), window.innerHeight);

      if (fromLeft) nextX = rightEdge - nextWidth;
      if (fromTop) nextY = bottomEdge - nextHeight;

      panelSize = { width: nextWidth, height: nextHeight };
      panelPosition = constrainPanelPosition({ x: nextX, y: nextY }, panelSize);
      applyPanelGeometry();
    }

    function finishPanelResize(event) {
      if (!resizingPanel || resizingPanel.pointerId !== event.pointerId) return;
      const activeHandle = resizingPanel.element;
      try {
        activeHandle.releasePointerCapture(event.pointerId);
      } catch {}
      resizingPanel = null;
      stopWidgetInteractionIfIdle();
    }

    function handleGlobalPointerMove(event) {
      updateLauncherDrag(event);
      updatePanelDrag(event);
      updatePanelResize(event);
    }

    function handleGlobalPointerEnd(event) {
      finishLauncherDrag(event);
      finishPanelDrag(event);
      finishPanelResize(event);
      stopWidgetInteractionIfIdle();
    }

    launcher.addEventListener('pointerdown', (event) => {
      if (isOpen || event.button !== 0) return;
      event.preventDefault();
      launcher.setPointerCapture(event.pointerId);
      setWidgetInteraction(true, 'grabbing');
      draggingLauncher = {
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        originX: launcherPosition.x,
        originY: launcherPosition.y,
        moved: false
      };
    });

    launcher.addEventListener('pointerup', finishLauncherDrag);
    launcher.addEventListener('dragstart', (event) => event.preventDefault());
    if (!window.PointerEvent) {
      launcher.addEventListener('click', openWidget);
    }
    launcher.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        openWidget();
      }
    });

    toolbar.addEventListener('pointerdown', (event) => {
      if (event.button !== 0 || (event.target instanceof Element && event.target.closest('button'))) {
        return;
      }
      toolbar.setPointerCapture(event.pointerId);
      setWidgetInteraction(true, 'grabbing');
      draggingPanel = {
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        originX: panelPosition.x,
        originY: panelPosition.y
      };
    });

    toolbar.addEventListener('pointerup', finishPanelDrag);

    resizeHandles.forEach((resizeHandle) => {
      resizeHandle.addEventListener('pointerdown', (event) => {
        if (event.button !== 0) return;
        event.preventDefault();
        resizeHandle.setPointerCapture(event.pointerId);
        setWidgetInteraction(true, getComputedStyle(resizeHandle).cursor || 'default');
        resizingPanel = {
          pointerId: event.pointerId,
          element: resizeHandle,
          handle: resizeHandle.dataset.resizeHandle,
          startX: event.clientX,
          startY: event.clientY,
          startPanelX: panelPosition.x,
          startPanelY: panelPosition.y,
          startWidth: panelSize.width,
          startHeight: panelSize.height
        };
      });
      resizeHandle.addEventListener('pointerup', finishPanelResize);
    });

    homeButton.addEventListener('click', goHome);
    refreshButton.addEventListener('click', refreshIframe);
    retractButton.addEventListener('click', retractWidget);

    window.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && isOpen) retractWidget();
    });
    window.addEventListener('pointermove', handleGlobalPointerMove);
    window.addEventListener('pointerup', handleGlobalPointerEnd);
    window.addEventListener('pointercancel', handleGlobalPointerEnd);
    window.addEventListener('blur', cancelWidgetInteraction);
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) cancelWidgetInteraction();
    });
    window.addEventListener('resize', () => {
      launcherPosition = constrainLauncherPosition(launcherPosition);
      applyLauncherPosition();
      if (isOpen) {
        panelSize = constrainSize(panelSize);
        panelPosition = constrainPanelPosition(panelPosition, panelSize);
        applyPanelGeometry();
      }
    });

    applyLauncherPosition();
    loadConfiguredHomeUrl();

    window.CPECCChatWidget = {
      open: openWidget,
      retract: retractWidget,
      refresh: refreshIframe
    };
  };

  if (document.body) {
    mount();
  } else {
    document.addEventListener('DOMContentLoaded', mount, { once: true });
  }
})();
