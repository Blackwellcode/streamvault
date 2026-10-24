(function () {
  "use strict";

  var MIN_ZOOM = 1;
  var MAX_ZOOM = 5;
  var ZOOM_STEP = 0.25;

  var overlay = null;
  var imgEl = null;
  var stageEl = null;
  var zoomLabel = null;
  var zoom = 1;
  var panX = 0;
  var panY = 0;
  var dragging = false;
  var dragStartX = 0;
  var dragStartY = 0;
  var panStartX = 0;
  var panStartY = 0;
  var lastFocus = null;

  function svgIcon(pathD) {
    return (
      '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" focusable="false">' +
      '<path fill="currentColor" d="' + pathD + '"/>' +
      "</svg>"
    );
  }

  var ICON_ZOOM_IN =
    "M15.5 14h-.79l-.28-.27A6.47 6.47 0 0 0 16 9.5 6.5 6.5 0 1 0 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14zM10 7H9v2H7v1h2v2h1v-2h2V9h-2V7z";
  var ICON_ZOOM_OUT =
    "M15.5 14h-.79l-.28-.27A6.47 6.47 0 0 0 16 9.5 6.5 6.5 0 1 0 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14zM7 9h5v1H7V9z";
  var ICON_CLOSE =
    "M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z";

  function ensureOverlay() {
    if (overlay) return overlay;

    overlay = document.createElement("div");
    overlay.id = "nsPhotoViewer";
    overlay.className = "ns-photo-viewer";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-label", "Fotoviewer");
    overlay.hidden = true;
    overlay.innerHTML =
      '<div class="ns-photo-viewer__backdrop" data-ns-photo-close="1"></div>' +
      '<div class="ns-photo-viewer__chrome">' +
      '<div class="ns-photo-viewer__toolbar" role="toolbar" aria-label="Bildsteuerung">' +
      '<button type="button" class="ns-photo-viewer__btn" data-ns-photo-zoom="-1" title="Herauszoomen" aria-label="Herauszoomen">' +
      svgIcon(ICON_ZOOM_OUT) +
      "</button>" +
      '<span class="ns-photo-viewer__zoom-label" data-ns-photo-zoom-label>100%</span>' +
      '<button type="button" class="ns-photo-viewer__btn" data-ns-photo-zoom="1" title="Hereanzoomen" aria-label="Hereanzoomen">' +
      svgIcon(ICON_ZOOM_IN) +
      "</button>" +
      '<button type="button" class="ns-photo-viewer__btn ns-photo-viewer__btn--close" data-ns-photo-close="1" title="Schließen" aria-label="Schließen">' +
      svgIcon(ICON_CLOSE) +
      "</button>" +
      "</div>" +
      '<div class="ns-photo-viewer__stage" data-ns-photo-stage>' +
      '<img class="ns-photo-viewer__img" alt="" draggable="false" data-ns-photo-img>' +
      "</div>" +
      "</div>";

    document.body.appendChild(overlay);

    imgEl = overlay.querySelector("[data-ns-photo-img]");
    stageEl = overlay.querySelector("[data-ns-photo-stage]");
    zoomLabel = overlay.querySelector("[data-ns-photo-zoom-label]");

    overlay.addEventListener("click", function (e) {
      var t = e.target;
      if (t.closest && t.closest("[data-ns-photo-close]")) {
        e.preventDefault();
        close();
        return;
      }
      var zoomBtn = t.closest && t.closest("[data-ns-photo-zoom]");
      if (zoomBtn) {
        e.preventDefault();
        var dir = parseFloat(zoomBtn.getAttribute("data-ns-photo-zoom") || "0");
        setZoom(zoom + dir * ZOOM_STEP);
      }
    });

    stageEl.addEventListener(
      "wheel",
      function (e) {
        e.preventDefault();
        var delta = e.deltaY < 0 ? ZOOM_STEP : -ZOOM_STEP;
        setZoom(zoom + delta);
      },
      { passive: false }
    );

    stageEl.addEventListener("pointerdown", function (e) {
      if (e.button !== 0 || zoom <= MIN_ZOOM) return;
      dragging = true;
      dragStartX = e.clientX;
      dragStartY = e.clientY;
      panStartX = panX;
      panStartY = panY;
      stageEl.classList.add("is-dragging");
      try {
        stageEl.setPointerCapture(e.pointerId);
      } catch (_) {}
    });

    stageEl.addEventListener("pointermove", function (e) {
      if (!dragging) return;
      panX = panStartX + (e.clientX - dragStartX);
      panY = panStartY + (e.clientY - dragStartY);
      applyTransform();
    });

    function endDrag(e) {
      if (!dragging) return;
      dragging = false;
      stageEl.classList.remove("is-dragging");
      try {
        if (e && e.pointerId != null) stageEl.releasePointerCapture(e.pointerId);
      } catch (_) {}
    }
    stageEl.addEventListener("pointerup", endDrag);
    stageEl.addEventListener("pointercancel", endDrag);

    document.addEventListener("keydown", function (e) {
      if (overlay.hidden) return;
      if (e.key === "Escape") {
        e.preventDefault();
        close();
      } else if (e.key === "+" || e.key === "=") {
        e.preventDefault();
        setZoom(zoom + ZOOM_STEP);
      } else if (e.key === "-" || e.key === "_") {
        e.preventDefault();
        setZoom(zoom - ZOOM_STEP);
      }
    });

    return overlay;
  }

  function applyTransform() {
    if (!imgEl) return;
    imgEl.style.transform =
      "translate(" + panX + "px," + panY + "px) scale(" + zoom + ")";
    if (zoomLabel) zoomLabel.textContent = Math.round(zoom * 100) + "%";
    if (stageEl) {
      stageEl.classList.toggle("is-zoomed", zoom > MIN_ZOOM);
    }
  }

  function setZoom(next) {
    var clamped = Math.max(
      MIN_ZOOM,
      Math.min(MAX_ZOOM, Math.round(next / ZOOM_STEP) * ZOOM_STEP)
    );
    if (clamped === MIN_ZOOM) {
      panX = 0;
      panY = 0;
    }
    zoom = clamped;
    applyTransform();
  }

  function open(src, alt) {
    if (!src) return;
    ensureOverlay();
    lastFocus = document.activeElement;
    zoom = 1;
    panX = 0;
    panY = 0;
    imgEl.removeAttribute("src");
    imgEl.alt = alt || "";
    imgEl.src = src;
    applyTransform();
    overlay.hidden = false;
    document.documentElement.classList.add("ns-photo-viewer-open");
    var closeBtn = overlay.querySelector(".ns-photo-viewer__btn--close");
    if (closeBtn) closeBtn.focus();
  }

  function close() {
    if (!overlay || overlay.hidden) return;
    overlay.hidden = true;
    document.documentElement.classList.remove("ns-photo-viewer-open");
    if (imgEl) {
      imgEl.removeAttribute("src");
      imgEl.alt = "";
    }
    zoom = 1;
    panX = 0;
    panY = 0;
    dragging = false;
    if (lastFocus && typeof lastFocus.focus === "function") {
      try {
        lastFocus.focus();
      } catch (_) {}
    }
    lastFocus = null;
  }

  function bindTriggers(root) {
    var scope = root || document;
    scope.querySelectorAll("[data-ns-photo]").forEach(function (el) {
      if (el.dataset.nsPhotoBound === "1") return;
      el.dataset.nsPhotoBound = "1";
      el.addEventListener("click", function (e) {
        var href = el.getAttribute("href") || el.getAttribute("data-ns-photo-src") || "";
        if (!href) return;
        e.preventDefault();
        var img = el.querySelector("img");
        var full =
          el.getAttribute("data-ns-photo-src") ||
          (img && (img.getAttribute("data-full") || img.currentSrc || img.src)) ||
          href;
        var alt = (img && img.getAttribute("alt")) || el.getAttribute("title") || "";
        open(full, alt);
      });
    });
  }

  window.StreamVaultPhotoViewer = {
    open: open,
    close: close,
    bind: bindTriggers
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      bindTriggers(document);
    });
  } else {
    bindTriggers(document);
  }
})();
