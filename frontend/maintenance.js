/**
 * YTCuttr — Site Maintenance Mode Overlay
 * Controlled via the YTCuttr Desktop Maintenance Control application.
 * When enabled:
 *   - Displays an unclosable dark window dead-center on the screen with:
 *       1. YTCuttr Logo with ambient red glow
 *       2. Live Countdown Timer (Hours : Minutes : Seconds)
 *       3. Custom Announcement / Reason text set by the admin
 *       4. Expected Completion Time
 *   - Completely blocks background typing, pasting, scrolling, and clicking.
 * When disabled:
 *   - The window is removed and the website is 100% normal.
 */

(function () {
  'use strict';

  const STATUS_ENDPOINT = '/maintenance.json';
  let countdownInterval = null;
  let isMaintenanceActive = false;

  // Block any keyboard typing, pasting, or input events while maintenance is active
  function blockBackgroundInput(e) {
    if (e.type === 'keydown' || e.type === 'keypress' || e.type === 'keyup' ||
        e.type === 'paste' || e.type === 'input' || e.type === 'beforeinput') {
      e.stopPropagation();
      e.preventDefault();
      if (document.activeElement && typeof document.activeElement.blur === 'function') {
        document.activeElement.blur();
      }
      return false;
    }
  }

  function enableBackgroundLock() {
    window.addEventListener('keydown', blockBackgroundInput, true);
    window.addEventListener('keypress', blockBackgroundInput, true);
    window.addEventListener('keyup', blockBackgroundInput, true);
    window.addEventListener('paste', blockBackgroundInput, true);
    window.addEventListener('input', blockBackgroundInput, true);
    window.addEventListener('beforeinput', blockBackgroundInput, true);
    document.body.style.overflow = 'hidden';
    if (document.activeElement && typeof document.activeElement.blur === 'function') {
      document.activeElement.blur();
    }
  }

  function disableBackgroundLock() {
    window.removeEventListener('keydown', blockBackgroundInput, true);
    window.removeEventListener('keypress', blockBackgroundInput, true);
    window.removeEventListener('keyup', blockBackgroundInput, true);
    window.removeEventListener('paste', blockBackgroundInput, true);
    window.removeEventListener('input', blockBackgroundInput, true);
    window.removeEventListener('beforeinput', blockBackgroundInput, true);
    document.body.style.overflow = '';
  }

  function escapeHtml(str) {
    return String(str || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  // Inject Self-Contained Styles
  function ensureStyles() {
    if (document.getElementById('ytc-maintenance-styles')) return;
    const style = document.createElement('style');
    style.id = 'ytc-maintenance-styles';
    style.textContent = `
      .ytc-maintenance-overlay {
        position: fixed !important;
        inset: 0 !important;
        width: 100vw !important;
        height: 100vh !important;
        z-index: 2147483647 !important;
        background: radial-gradient(circle at 50% 25%, rgba(20, 24, 38, 0.98), #060810 88%) !important;
        backdrop-filter: blur(20px) !important;
        -webkit-backdrop-filter: blur(20px) !important;
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
        padding: 24px !important;
        overflow: hidden !important;
        color: #ffffff !important;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif !important;
        pointer-events: all !important;
        user-select: none !important;
        -webkit-user-select: none !important;
        animation: ytcFadeIn 0.3s ease;
      }
      @keyframes ytcFadeIn {
        from { opacity: 0; transform: scale(0.98); }
        to { opacity: 1; transform: scale(1); }
      }
      .ytc-maintenance-center-card {
        max-width: 580px;
        width: 100%;
        background: rgba(15, 17, 28, 0.96);
        border: 1.5px solid rgba(255, 255, 255, 0.14);
        border-radius: 24px;
        padding: 38px 32px;
        text-align: center;
        box-shadow: 0 30px 80px rgba(0, 0, 0, 0.85), 0 0 100px rgba(239, 68, 68, 0.2);
        position: relative;
        margin: auto;
        pointer-events: all;
      }
      .ytc-m-logo-wrap {
        position: relative;
        display: inline-block;
        margin-bottom: 20px;
      }
      .ytc-m-logo-icon {
        font-size: 54px;
        line-height: 1;
        position: relative;
        z-index: 2;
        filter: drop-shadow(0 8px 24px rgba(239, 68, 68, 0.5));
      }
      .ytc-m-logo-glow {
        position: absolute;
        inset: -14px;
        background: radial-gradient(circle, rgba(239, 68, 68, 0.4), transparent 70%);
        border-radius: 50%;
        z-index: 1;
        animation: ytcPulse 2.5s infinite ease-in-out;
      }
      @keyframes ytcPulse {
        0%, 100% { transform: scale(1); opacity: 0.6; }
        50% { transform: scale(1.15); opacity: 0.9; }
      }
      .ytc-m-badge {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        background: rgba(239, 68, 68, 0.15);
        border: 1px solid rgba(239, 68, 68, 0.35);
        color: #f87171;
        font-size: 0.82rem;
        font-weight: 700;
        padding: 5px 14px;
        border-radius: 20px;
        margin-bottom: 16px;
        text-transform: uppercase;
        letter-spacing: 0.5px;
      }
      .ytc-m-pulse-dot {
        width: 8px;
        height: 8px;
        background: #ef4444;
        border-radius: 50%;
        box-shadow: 0 0 10px #ef4444;
        animation: ytcDotPulse 1.5s infinite;
      }
      @keyframes ytcDotPulse {
        0%, 100% { opacity: 1; transform: scale(1); }
        50% { opacity: 0.4; transform: scale(0.85); }
      }
      .ytc-m-heading {
        font-size: 1.65rem;
        font-weight: 800;
        color: #ffffff;
        margin: 0 0 10px 0;
        line-height: 1.3;
        letter-spacing: -0.5px;
      }
      .ytc-m-desc {
        font-size: 0.95rem;
        color: #94a3b8;
        line-height: 1.55;
        margin: 0 0 24px 0;
      }
      .ytc-countdown-wrap {
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 12px;
        margin: 0 auto 20px auto;
      }
      .ytc-countdown-card {
        background: rgba(255, 255, 255, 0.05);
        border: 1px solid rgba(255, 255, 255, 0.1);
        border-radius: 14px;
        padding: 12px 18px;
        min-width: 85px;
        display: flex;
        flex-direction: column;
        align-items: center;
        box-shadow: inset 0 2px 4px rgba(255, 255, 255, 0.05);
      }
      .ytc-c-val {
        font-family: 'SF Mono', Consolas, Monaco, monospace;
        font-size: 2rem;
        font-weight: 800;
        color: #60a5fa;
        line-height: 1.1;
        text-shadow: 0 0 20px rgba(96, 165, 250, 0.4);
      }
      .ytc-c-lbl {
        font-size: 0.72rem;
        font-weight: 600;
        color: #64748b;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        margin-top: 4px;
      }
      .ytc-c-sep {
        font-family: 'SF Mono', Consolas, Monaco, monospace;
        font-size: 1.8rem;
        font-weight: 700;
        color: #475569;
        margin-top: -12px;
      }
      .ytc-m-estimate-badge {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        background: rgba(99, 102, 241, 0.12);
        border: 1px solid rgba(99, 102, 241, 0.25);
        color: #c7d2fe;
        font-size: 0.85rem;
        padding: 6px 16px;
        border-radius: 10px;
        margin-bottom: 20px;
      }
      .ytc-m-estimate-badge strong {
        color: #ffffff;
      }
      .ytc-m-safety-notice {
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 8px;
        background: rgba(16, 185, 129, 0.08);
        border: 1px solid rgba(16, 185, 129, 0.2);
        color: #6ee7b7;
        font-size: 0.82rem;
        padding: 8px 14px;
        border-radius: 10px;
        margin-bottom: 20px;
        line-height: 1.4;
      }
      .ytc-m-footer {
        font-size: 0.84rem;
        color: #64748b;
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 6px;
        flex-wrap: wrap;
      }
      .ytc-m-mail-link {
        color: #38bdf8;
        text-decoration: none;
      }
      .ytc-m-mail-link:hover {
        text-decoration: underline;
      }
    `;
    document.head.appendChild(style);
  }

  async function checkMaintenanceStatus() {
    try {
      const resp = await fetch(`${STATUS_ENDPOINT}?_t=${Date.now()}`, {
        method: 'GET',
        headers: { 'Accept': 'application/json' },
        cache: 'no-store'
      });
      if (!resp.ok) return;
      const data = await resp.json();

      const now = Date.now();
      let isEnabled = Boolean(data && data.enabled);

      // Auto-disable if estimated end time has passed
      if (isEnabled && data.endTime) {
        const endMs = new Date(data.endTime).getTime();
        if (!isNaN(endMs) && now >= endMs) {
          isEnabled = false;
        }
      }

      if (isEnabled) {
        if (!isMaintenanceActive) {
          isMaintenanceActive = true;
          enableBackgroundLock();
        }
        renderCenterMaintenanceWindow(data);
      } else {
        if (isMaintenanceActive) {
          isMaintenanceActive = false;
          disableBackgroundLock();
          removeCenterMaintenanceWindow();
          window.location.reload();
        } else {
          removeCenterMaintenanceWindow();
        }
      }
    } catch (e) {
      console.warn('[Maintenance Check]:', e);
    }
  }

  function renderCenterMaintenanceWindow(data) {
    ensureStyles();

    let overlay = document.getElementById('ytcMaintenanceOverlay');
    if (!overlay) {
      overlay = document.createElement('div');
      overlay.id = 'ytcMaintenanceOverlay';
      overlay.className = 'ytc-maintenance-overlay';
      document.body.appendChild(overlay);
    }

    const titleText = data.title || 'YTCuttr is Under Maintenance';
    const reasonText = data.reason || 'We are performing scheduled maintenance and server performance upgrades. We will be back online shortly!';
    const targetEnd = data.endTime ? new Date(data.endTime) : null;
    const formattedEnd = targetEnd && !isNaN(targetEnd.getTime())
      ? targetEnd.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: true }) + ' (' + targetEnd.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ')'
      : 'Shortly';

    overlay.innerHTML = `
      <div class="ytc-maintenance-center-card">
        <div class="ytc-m-logo-wrap">
          <div class="ytc-m-logo-glow"></div>
          <span class="ytc-m-logo-icon">✂️</span>
        </div>

        <div class="ytc-m-badge">
          <span class="ytc-m-pulse-dot"></span> System Maintenance Active
        </div>

        <h1 class="ytc-m-heading">${escapeHtml(titleText)}</h1>
        <p class="ytc-m-desc">${escapeHtml(reasonText)}</p>

        <!-- Live Countdown Cards -->
        <div class="ytc-countdown-wrap">
          <div class="ytc-countdown-card">
            <span class="ytc-c-val" id="ytcCountHours">00</span>
            <span class="ytc-c-lbl">Hours</span>
          </div>
          <span class="ytc-c-sep">:</span>
          <div class="ytc-countdown-card">
            <span class="ytc-c-val" id="ytcCountMins">00</span>
            <span class="ytc-c-lbl">Minutes</span>
          </div>
          <span class="ytc-c-sep">:</span>
          <div class="ytc-countdown-card">
            <span class="ytc-c-val" id="ytcCountSecs">00</span>
            <span class="ytc-c-lbl">Seconds</span>
          </div>
        </div>

        <div class="ytc-m-estimate-badge">
          <span>🕒 Expected Completion:</span>
          <strong>${formattedEnd}</strong>
        </div>

        <div class="ytc-m-safety-notice">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#10b981" stroke-width="2.5"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
          <span>All cutting engines, tools, and servers are 100% secure.</span>
        </div>

        <div class="ytc-m-footer">
          <span>Need assistance?</span>
          <a href="mailto:support@ytcuttr.com" class="ytc-m-mail-link">support@ytcuttr.com</a>
        </div>
      </div>
    `;

    overlay.style.display = 'flex';
    startLiveCountdown(data.endTime);
  }

  function removeCenterMaintenanceWindow() {
    const overlay = document.getElementById('ytcMaintenanceOverlay');
    if (overlay) {
      overlay.style.display = 'none';
      overlay.remove();
    }
    if (countdownInterval) {
      clearInterval(countdownInterval);
      countdownInterval = null;
    }
  }

  function startLiveCountdown(endTimeStr) {
    if (countdownInterval) clearInterval(countdownInterval);
    if (!endTimeStr) return;

    const targetMs = new Date(endTimeStr).getTime();
    if (isNaN(targetMs)) return;

    function tick() {
      const diff = Math.max(0, targetMs - Date.now());
      if (diff <= 0) {
        clearInterval(countdownInterval);
        countdownInterval = null;
        setTimeout(() => { checkMaintenanceStatus(); }, 1500);
        return;
      }

      const totalSec = Math.floor(diff / 1000);
      const h = Math.floor(totalSec / 3600);
      const m = Math.floor((totalSec % 3600) / 60);
      const s = totalSec % 60;

      const pad = (n) => String(n).padStart(2, '0');
      const hEl = document.getElementById('ytcCountHours');
      const mEl = document.getElementById('ytcCountMins');
      const sEl = document.getElementById('ytcCountSecs');
      if (hEl) hEl.textContent = pad(h);
      if (mEl) mEl.textContent = pad(m);
      if (sEl) sEl.textContent = pad(s);
    }

    tick();
    countdownInterval = setInterval(tick, 1000);
  }

  // Initial check on load
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', checkMaintenanceStatus);
  } else {
    checkMaintenanceStatus();
  }

  // Periodic poll every 8 seconds to detect desktop app toggle
  setInterval(checkMaintenanceStatus, 8000);

})();
