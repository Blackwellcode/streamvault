/**
 * NachtSilo Tube — Dashboard Realtime VOD Sync
 * Automatically detects and renders new VODs on the start page without full reload.
 */
(function () {
  "use strict";

  var pollTimer = null;
  var isFetching = false;
  var POLL_INTERVAL_MS = 20000; // 20s
  var currentTopVod = null;
  var currentPage = 1;

  function escapeHtml(str) {
    if (!str) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function renderDesktopCard(vod, isFresh) {
    var thumbHtml = vod.thumb_url
      ? '<img src="' + escapeHtml(vod.thumb_url) + '" alt="' + escapeHtml(vod.title || vod.name) + '" class="nebula-vod-thumb-img" loading="lazy" decoding="async">'
      : '<div style="width:100%; height:100%; background: linear-gradient(135deg, #111A2B 0%, #151F33 100%); display:flex; align-items:center; justify-content:center; color:#A2AEC8; font-weight:700; font-size:16px;">' + escapeHtml(vod.streamer_name) + '</div>';

    var durHtml = vod.duration_str
      ? '<span class="nebula-vod-duration">' + escapeHtml(vod.duration_str) + '</span>'
      : '';

    var playOverlay = (document.body.classList.contains("is-tv") || document.body.classList.contains("is-mobile"))
      ? ''
      : '<div class="nebula-vod-play-overlay"><div class="nebula-vod-play-circle"><svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg></div></div>';

    var freshClass = isFresh ? ' ns-vod-fresh-enter' : '';

    return '<a href="' + escapeHtml(vod.url) + '" class="nebula-vod-card' + freshClass + '" data-vod-name="' + escapeHtml(vod.name) + '">' +
      '<div class="nebula-vod-thumb-box">' +
        thumbHtml +
        '<span class="nebula-vod-streamer-badge">' + escapeHtml(vod.streamer_name) + '</span>' +
        durHtml +
        playOverlay +
      '</div>' +
      '<div class="nebula-vod-body">' +
        '<h4 class="nebula-vod-title">' + escapeHtml(vod.title || vod.name) + '</h4>' +
        '<div class="nebula-vod-footer">' +
          '<span>' + escapeHtml(vod.category || "VOD") + '</span>' +
          '<span>' + escapeHtml(vod.date_str || "") + '</span>' +
        '</div>' +
      '</div>' +
    '</a>';
  }

  function renderMobileCard(vod, isFresh) {
    var thumbHtml = vod.thumb_url
      ? '<img src="' + escapeHtml(vod.thumb_url) + '" loading="lazy" decoding="async" alt="' + escapeHtml(vod.title || vod.name) + '" style="width: 100%; height: 100%; object-fit: cover;">'
      : '<div style="width: 100%; height: 100%; display: flex; align-items: center; justify-content: center; font-size: 12px; color: #A2AEC8; font-weight: 700;">VOD</div>';

    var durHtml = vod.duration_str
      ? '<span style="position: absolute; bottom: 4px; right: 4px; background: rgba(0,0,0,0.85); color: #FFF; font-size: 10px; padding: 1px 4px; border-radius: 3px; font-family: var(--ns-font-mono);">' + escapeHtml(vod.duration_str) + '</span>'
      : '';

    var freshBorder = isFresh ? 'border-color: #4D63FF; animation: vodCardPulse 1.2s ease-out;' : '';

    return '<a href="' + escapeHtml(vod.url) + '" data-vod-name="' + escapeHtml(vod.name) + '" style="display: flex; gap: 12px; background: var(--nebula-bg-surface, #111A2B); border: 1px solid var(--nebula-border, #26344F); border-radius: 12px; overflow: hidden; text-decoration: none; color: inherit; padding: 10px; box-sizing: border-box; ' + freshBorder + '">' +
      '<div style="position: relative; width: 120px; aspect-ratio: 16 / 9; border-radius: 8px; overflow: hidden; background: #0B1220; flex-shrink: 0;">' +
        thumbHtml +
        durHtml +
      '</div>' +
      '<div style="flex: 1; min-width: 0; display: flex; flex-direction: column; justify-content: center;">' +
        '<h4 style="font-size: 13px; font-weight: 600; color: #FFF; margin: 0 0 4px 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">' + escapeHtml(vod.title || vod.name) + '</h4>' +
        '<div style="font-size: 11px; color: #BB78FF; margin-bottom: 2px;">' + escapeHtml(vod.streamer_name) + '</div>' +
        '<div style="font-size: 10px; color: #75839D;">' + escapeHtml(vod.date_str || '') + '</div>' +
      '</div>' +
    '</a>';
  }

  function updatePaginationText(data) {
    if (!data) return;
    // Desktop inline header
    var headerCount = document.querySelector(".ns-pagination-inline span");
    if (headerCount) {
      headerCount.innerHTML = 'Seite <strong>' + data.page + '</strong> / <strong>' + data.total_pages + '</strong> (' + data.total_count + ' VODs)';
    }
    // Desktop bottom nav
    var bottomNavInfo = document.querySelector(".ns-pagination-bottom .ns-pagination-info");
    if (bottomNavInfo) {
      bottomNavInfo.innerHTML = 'Seite <strong>' + data.page + '</strong> von <strong>' + data.total_pages + '</strong> &bull; ' + data.total_count + ' VODs im Schnellzugriff';
    }
    // Mobile header counter
    var mobileCounter = document.querySelector("section > div > span:nth-child(2)");
    if (mobileCounter && mobileCounter.textContent.indexOf("Seite") !== -1) {
      mobileCounter.textContent = 'Seite ' + data.page + ' / ' + data.total_pages;
    }
  }

  function showNewVodNotification(newCount) {
    var existingPill = document.getElementById("ns-vod-update-pill");
    if (existingPill) return;

    var container = document.querySelector(".nebula-main-col") || document.querySelector("main");
    if (!container) return;

    var pill = document.createElement("div");
    pill.id = "ns-vod-update-pill";
    pill.style.cssText = "position: sticky; top: 12px; z-index: 100; margin: 0 auto 14px auto; max-width: 320px; background: linear-gradient(135deg, #4D63FF 0%, #754DFF 100%); color: #FFF; padding: 10px 18px; border-radius: 20px; font-size: 13px; font-weight: 600; text-align: center; box-shadow: 0 4px 16px rgba(77,99,255,0.4); cursor: pointer; transition: transform 0.2s ease, opacity 0.2s ease;";
    pill.innerHTML = '✨ ' + (newCount > 1 ? newCount + ' neue VODs' : '1 neues VOD') + ' verf&uuml;gbar &bull; Jetzt ansehen';

    pill.addEventListener("click", function () {
      window.location.href = "/?page=1";
    });

    var grid = document.getElementById("nebulaVodGrid") || document.getElementById("mobileVodList");
    if (grid && grid.parentNode) {
      grid.parentNode.insertBefore(pill, grid);
    }
  }

  async function checkDashboardUpdates() {
    if (isFetching) return;
    if (document.hidden) return;

    var desktopGrid = document.getElementById("nebulaVodGrid");
    var mobileList = document.getElementById("mobileVodList");
    var activeEl = desktopGrid || mobileList;
    if (!activeEl) return;

    isFetching = true;
    try {
      var controller = new AbortController();
      var timeoutId = setTimeout(function () { controller.abort(); }, 8000);

      var resp = await fetch("/api/dashboard/recent_vods?page=" + currentPage, {
        headers: { "Accept": "application/json" },
        signal: controller.signal
      });
      clearTimeout(timeoutId);

      if (!resp.ok) return;
      var data = await resp.json();
      if (!data || !data.ok || !Array.isArray(data.vods)) return;

      var newTop = data.top_vod_name || (data.vods[0] && data.vods[0].name) || "";
      if (!newTop) return;

      if (!currentTopVod) {
        currentTopVod = newTop;
        return;
      }

      if (newTop !== currentTopVod) {
        if (currentPage === 1) {
          // In-place refresh on page 1
          if (desktopGrid) {
            var html = "";
            data.vods.forEach(function (v, idx) {
              html += renderDesktopCard(v, idx === 0);
            });
            desktopGrid.innerHTML = html;
          } else if (mobileList) {
            var mobileHtml = "";
            data.vods.forEach(function (v, idx) {
              mobileHtml += renderMobileCard(v, idx === 0);
            });
            mobileList.innerHTML = mobileHtml;
          }
          currentTopVod = newTop;
          updatePaginationText(data);
        } else {
          // User is on page 2+; show non-intrusive jump banner
          showNewVodNotification(1);
        }
      }
    } catch (_) {
      // Graceful silence on aborted / offline requests
    } finally {
      isFetching = false;
    }
  }

  function init() {
    var desktopGrid = document.getElementById("nebulaVodGrid");
    var mobileList = document.getElementById("mobileVodList");
    var activeEl = desktopGrid || mobileList;
    if (!activeEl) return;

    currentPage = parseInt(activeEl.getAttribute("data-vod-page") || "1", 10) || 1;
    currentTopVod = activeEl.getAttribute("data-top-vod") || null;

    if (!currentTopVod) {
      var firstCard = activeEl.querySelector("[data-vod-name]");
      if (firstCard) {
        currentTopVod = firstCard.getAttribute("data-vod-name");
      }
    }

    // Polling setup with tab visibility hygiene
    pollTimer = setInterval(checkDashboardUpdates, POLL_INTERVAL_MS);

    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) {
        checkDashboardUpdates();
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
