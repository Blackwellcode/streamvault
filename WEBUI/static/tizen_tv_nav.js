(function () {
  "use strict";

  if (typeof Promise !== "undefined" && typeof Promise.allSettled !== "function") {
    Promise.allSettled = function (promises) {
      var arr = Array.prototype.slice.call(promises || []);
      return Promise.all(arr.map(function (p) {
        return Promise.resolve(p).then(
          function (val) { return { status: "fulfilled", value: val }; },
          function (err) { return { status: "rejected", reason: err }; }
        );
      }));
    };
  }

  var ua = navigator.userAgent || "";
  var isTv = /Tizen|SMART-TV|SmartTV|NetCast|Web0S/i.test(ua) || window.location.search.indexOf("tv=1") !== -1;
  if (!isTv) return;

  document.documentElement.classList.add("is-tizen-tv");

  var KEY_LEFT = 37;
  var KEY_UP = 38;
  var KEY_RIGHT = 39;
  var KEY_DOWN = 40;
  var KEY_ENTER = 13;
  var KEY_RETURN = 10009;
  var KEY_PLAY = 415;
  var KEY_PAUSE = 19;
  var KEY_PLAY_PAUSE = 10252;
  var KEY_STOP = 413;
  var KEY_REWIND = 412;
  var KEY_FF = 417;
  var KEY_BLUE = 406;

  var FOCUS_SELECTOR = [
    "a[href]",
    "button:not([disabled])",
    "input:not([type=\"hidden\"]):not([disabled])",
    "select:not([disabled])",
    "textarea:not([disabled])",
    "video",
    ".vod-seekbar",
    "[role=\"slider\"]",
    "[role=\"button\"]",
    "[tabindex]:not([tabindex=\"-1\"])"
  ].join(", ");

  var lastBackAt = 0;
  var toastEl = null;
  var toastTimer = null;
  var controlAwakeTimer = null;
  var lastSeekKeyAt = 0;
  var lastSeekDir = 0;
  var seekStreak = 0;
  var tipHideTimer = null;

  function formatDuration(sec) {
    var s = Math.max(0, Math.floor(Number(sec) || 0));
    var h = Math.floor(s / 3600);
    var m = Math.floor((s % 3600) / 60);
    var r = s % 60;
    var mm = (m < 10 ? "0" : "") + m;
    var rr = (r < 10 ? "0" : "") + r;
    return h > 0 ? (h + ":" + mm + ":" + rr) : (m + ":" + rr);
  }

  function ensureTvStylesheet() {
    document.documentElement.classList.add("is-tizen-tv");
    if (document.body) {
      if (document.body.classList.contains("page-streamer_view")) {
        document.body.classList.add("page-streamer");
      }
      if (document.body.classList.contains("page-channels_view")) {
        document.body.classList.add("page-channels");
      }
    }
    var head = document.head || document.documentElement;
    var link = document.getElementById("ns-tizen-tv-css");
    if (!link) {
      link = document.createElement("link");
      link.id = "ns-tizen-tv-css";
      link.rel = "stylesheet";
      link.href = "/static/css/tizen_tv.css?v=20261017-live13";
      head.appendChild(link);
    } else if (head.lastElementChild !== link) {
      head.appendChild(link);
    }

    if (!document.getElementById("ns-tizen-tv-toast-style")) {
      var style = document.createElement("style");
      style.id = "ns-tizen-tv-toast-style";
      style.textContent = [
        ".ns-tv-toast {",
        "  position: fixed;",
        "  bottom: 42px;",
        "  left: 50%;",
        "  transform: translateX(-50%);",
        "  z-index: 999999;",
        "  background: rgba(8, 13, 26, 0.96);",
        "  color: #c6ff6a;",
        "  border: 2px solid #c6ff6a;",
        "  border-radius: 14px;",
        "  padding: 14px 28px;",
        "  font-size: 22px;",
        "  font-weight: 800;",
        "  box-shadow: 0 14px 36px rgba(0, 0, 0, 0.85);",
        "  pointer-events: none;",
        "}"
      ].join("\n");
      head.appendChild(style);
    }
  }

  function showToast(msg) {
    if (!toastEl) {
      toastEl = document.createElement("div");
      toastEl.className = "ns-tv-toast";
      (document.body || document.documentElement).appendChild(toastEl);
    }
    toastEl.textContent = msg;
    toastEl.style.display = "block";
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      if (toastEl) toastEl.style.display = "none";
    }, 2400);
  }

  function wakeControlBar() {
    var pw = document.querySelector(".player-wrapper");
    var bar = document.querySelector(".vod-controlbar");
    var video = getActiveVideo();
    if (pw) pw.classList.add("ns-tv-controls-awake");
    if (bar) bar.classList.remove("vod-controlbar--hidden");
    if (video && typeof video._streamVaultShowControls === "function") {
      try { video._streamVaultShowControls(); } catch (_) {}
    }
    var seekEl = document.querySelector(".vod-seekbar");
    if (seekEl && !seekEl.hasAttribute("tabindex")) {
      seekEl.setAttribute("tabindex", "0");
    }
    if (controlAwakeTimer) clearTimeout(controlAwakeTimer);
    controlAwakeTimer = setTimeout(function () {
      var active = document.activeElement;
      if (active && bar && bar.contains(active)) return;
      if (pw && video && !video.paused) {
        pw.classList.remove("ns-tv-controls-awake");
      }
    }, 5000);
  }

  function isVisible(el) {
    if (!el || !el.getBoundingClientRect) return false;
    var style = window.getComputedStyle(el);
    if (!style || style.display === "none" || style.visibility === "hidden") {
      return false;
    }
    if (parseFloat(style.opacity) === 0 && !el.closest(".vod-controlbar")) {
      return false;
    }
    var rect = el.getBoundingClientRect();
    return rect.width > 6 && rect.height > 6;
  }

  function getFocusableElements() {
    var raw = document.querySelectorAll(FOCUS_SELECTOR);
    var out = [];
    for (var i = 0; i < raw.length; i++) {
      if (isVisible(raw[i])) {
        if ((raw[i].tagName === "VIDEO" || raw[i].classList.contains("vod-seekbar")) && !raw[i].hasAttribute("tabindex")) {
          raw[i].setAttribute("tabindex", "0");
        }
        out.push(raw[i]);
      }
    }
    return out;
  }

  function focusElement(el) {
    if (!el) return;
    var prev = document.querySelectorAll(".ns-tv-focus");
    for (var i = 0; i < prev.length; i++) {
      prev[i].classList.remove("ns-tv-focus");
    }
    el.classList.add("ns-tv-focus");
    if (el.closest && (el.closest(".player-wrapper") || el.closest(".vod-controlbar"))) {
      wakeControlBar();
    }
    try {
      el.focus({ preventScroll: true });
    } catch (e) {
      try { el.focus(); } catch (_) {}
    }
    try {
      el.scrollIntoView({ behavior: "smooth", block: "nearest", inline: "nearest" });
    } catch (e) {
      try { el.scrollIntoView(false); } catch (_) {}
    }
  }

  function getActiveVideo() {
    return document.querySelector("video#vodPlayer, video#player, .player-wrapper video, video");
  }

  function isTvFullscreen() {
    var fsEl = document.fullscreenElement || document.webkitFullscreenElement;
    return !!(fsEl || (document.body && document.body.classList.contains("ns-tv-video-fullscreen")));
  }

  function setTvFullscreen(enable) {
    if (!document.body) return;
    var video = getActiveVideo();
    if (enable) {
      document.body.classList.add("ns-tv-video-fullscreen");
      window.scrollTo(0, 0);
      wakeControlBar();
      var seekEl = document.querySelector(".vod-seekbar");
      focusElement(seekEl || video);
      showToast("Vollbild aktiv — „Zurück“ beendet Vollbild");
    } else {
      document.body.classList.remove("ns-tv-video-fullscreen");
      var fsEl = document.fullscreenElement || document.webkitFullscreenElement;
      if (fsEl) {
        if (document.exitFullscreen) document.exitFullscreen();
        else if (document.webkitExitFullscreen) document.webkitExitFullscreen();
      }
      showToast("Kino-Ansicht aktiv");
    }
  }

  function toggleVideoFullscreen() {
    setTvFullscreen(!isTvFullscreen());
  }

  function toggleVideoPlay(video) {
    if (!video) return;
    wakeControlBar();
    if (video.paused) {
      try {
        if (typeof video._streamVaultSetUserPaused === "function") {
          video._streamVaultSetUserPaused(false);
        }
        var p = video.play();
        if (p && typeof p.catch === "function") {
          p.catch(function () {
            video.muted = true;
            video.play();
          });
        }
        showToast("▶ Wiedergabe");
      } catch (e) {}
    } else {
      try {
        if (typeof video._streamVaultSetUserPaused === "function") {
          video._streamVaultSetUserPaused(true);
        }
        video.pause();
        showToast("⏸ Pause");
      } catch (e) {}
    }
  }

  function scrubVideoByKey(video, dir) {
    if (!video) return;
    wakeControlBar();
    var now = Date.now();
    if (dir === lastSeekDir && (now - lastSeekKeyAt) < 900) {
      seekStreak += 1;
    } else {
      seekStreak = 1;
      lastSeekDir = dir;
    }
    lastSeekKeyAt = now;

    var step = 15;
    if (seekStreak >= 12) step = 300;
    else if (seekStreak >= 8) step = 180;
    else if (seekStreak >= 4) step = 60;
    else if (seekStreak >= 2) step = 30;

    var dur = (typeof video.duration === "number" && Number.isFinite(video.duration) && video.duration > 0) ? video.duration : Infinity;
    var cur = Number(video.currentTime) || 0;
    var target = Math.max(0, Math.min(dur, cur + dir * step));

    if (typeof video._streamVaultNoteUserSeek === "function") {
      try { video._streamVaultNoteUserSeek(target); } catch (_) {}
    }
    video.currentTime = target;

    if (Number.isFinite(dur) && dur > 0) {
      var pct = Math.max(0, Math.min(100, (target / dur) * 100));
      var playedEl = document.querySelector(".vod-seekbar__played");
      var thumbEl = document.querySelector(".vod-seekbar__thumb");
      var tipEl = document.querySelector(".vod-seekbar__tip");
      var curEl = document.querySelector(".vod-cb-current");
      if (playedEl) playedEl.style.width = pct + "%";
      if (thumbEl) thumbEl.style.left = pct + "%";
      if (curEl) curEl.textContent = formatDuration(target);
      if (tipEl) {
        tipEl.textContent = formatDuration(target);
        tipEl.style.left = pct + "%";
        tipEl.hidden = false;
        if (tipHideTimer) clearTimeout(tipHideTimer);
        tipHideTimer = setTimeout(function () { tipEl.hidden = true; }, 2200);
      }
    }

    var sign = dir > 0 ? "+" : "-";
    var stepLabel = step >= 60 ? (Math.round(step / 60) + " Min") : (step + "s");
    showToast((dir > 0 ? "⏩ " : "⏪ ") + formatDuration(target) + " (" + sign + stepLabel + ")");
  }

  function enhanceVideoPage() {
    var video = getActiveVideo();
    if (!video) return;
    try { video.controls = false; } catch (_) {}
    try { video.removeAttribute("controls"); } catch (_) {}
    if (!video.hasAttribute("tabindex")) {
      video.setAttribute("tabindex", "0");
    }

    // Remove any legacy injected buttons in vod-details
    var oldFs = document.getElementById("btnTvFullscreenToggle");
    if (oldFs && oldFs.parentNode) oldFs.parentNode.removeChild(oldFs);
    var oldPlay = document.getElementById("btnTvPlayToggle");
    if (oldPlay && oldPlay.parentNode) oldPlay.parentNode.removeChild(oldPlay);

    var seekEl = document.querySelector(".vod-seekbar");
    if (seekEl && !seekEl.hasAttribute("tabindex")) {
      seekEl.setAttribute("tabindex", "0");
    }

    // Bind custom controlbar fullscreen button to TV 1920x1080 fullscreen
    var cbFs = document.querySelector(".vod-cb-fullscreen");
    if (cbFs && !cbFs._nsTvBound) {
      cbFs._nsTvBound = true;
      cbFs.addEventListener("click", function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        toggleVideoFullscreen();
      }, true);
    }

    setTimeout(function () {
      wakeControlBar();
      var sEl = document.querySelector(".vod-seekbar");
      if (sEl && !sEl.hasAttribute("tabindex")) {
        sEl.setAttribute("tabindex", "0");
      }
      if (video.paused && !video.ended) {
        try {
          var p = video.play();
          if (p && typeof p.catch === "function") {
            p.catch(function () {
              video.muted = true;
              var p2 = video.play();
              if (p2 && typeof p2.then === "function") {
                p2.then(function () {
                  setTimeout(function () { video.muted = false; }, 300);
                }).catch(function () {});
              }
            });
          }
        } catch (_) {}
      }
    }, 450);
  }

  function ensureInitialFocus() {
    var active = document.activeElement;
    if (active && active !== document.body && active !== document.documentElement && isVisible(active)) {
      active.classList.add("ns-tv-focus");
      return active;
    }
    var seekEl = document.querySelector(".vod-seekbar");
    if (seekEl && isVisible(seekEl)) {
      wakeControlBar();
      focusElement(seekEl);
      return seekEl;
    }
    var video = document.querySelector("video#vodPlayer, video#player");
    if (video && isVisible(video)) {
      if (!video.hasAttribute("tabindex")) video.setAttribute("tabindex", "0");
      focusElement(video);
      return video;
    }
    var mainCandidates = document.querySelectorAll("main a[href], .ns-workspace-content a[href], main button, .maintenance-login-link, #login-submit");
    for (var i = 0; i < mainCandidates.length; i++) {
      if (isVisible(mainCandidates[i])) {
        focusElement(mainCandidates[i]);
        return mainCandidates[i];
      }
    }
    var all = getFocusableElements();
    if (all.length > 0) {
      focusElement(all[0]);
      return all[0];
    }
    return null;
  }

  function moveSpatialFocus(dirKey) {
    var video = getActiveVideo();
    if (video) wakeControlBar();

    var current = document.activeElement;
    var seekEl = document.querySelector(".vod-seekbar");
    var playBtn = document.querySelector(".vod-cb-play");

    // Direct ergonomic transitions inside video player
    if (current && current.tagName === "VIDEO" && dirKey === KEY_DOWN && seekEl) {
      focusElement(seekEl);
      return;
    }
    if (current && current.classList && current.classList.contains("vod-seekbar")) {
      if (dirKey === KEY_UP && video) {
        focusElement(video);
        return;
      }
      if (dirKey === KEY_DOWN && playBtn) {
        focusElement(playBtn);
        return;
      }
    }

    var elements = getFocusableElements();
    if (!elements.length) return;

    if (!current || current === document.body || current === document.documentElement || !isVisible(current)) {
      ensureInitialFocus();
      return;
    }

    var curRect = current.getBoundingClientRect();
    var cx = curRect.left + curRect.width / 2;
    var cy = curRect.top + curRect.height / 2;

    var bestEl = null;
    var bestScore = Infinity;

    for (var i = 0; i < elements.length; i++) {
      var el = elements[i];
      if (el === current) continue;
      var r = el.getBoundingClientRect();
      var ex = r.left + r.width / 2;
      var ey = r.top + r.height / 2;
      var dx = ex - cx;
      var dy = ey - cy;

      var primary = 0;
      var secondary = 0;
      if (dirKey === KEY_RIGHT) {
        if (dx <= 4) continue;
        primary = dx;
        secondary = Math.abs(dy);
      } else if (dirKey === KEY_LEFT) {
        if (dx >= -4) continue;
        primary = -dx;
        secondary = Math.abs(dy);
      } else if (dirKey === KEY_DOWN) {
        if (dy <= 4) continue;
        primary = dy;
        secondary = Math.abs(dx);
      } else if (dirKey === KEY_UP) {
        if (dy >= -4) continue;
        primary = -dy;
        secondary = Math.abs(dx);
      }

      var score = primary + secondary * 2.4;
      if (score < bestScore) {
        bestScore = score;
        bestEl = el;
      }
    }

    if (bestEl) {
      focusElement(bestEl);
    } else if (dirKey === KEY_DOWN) {
      window.scrollBy(0, 320);
    } else if (dirKey === KEY_UP) {
      window.scrollBy(0, -320);
    }
  }

  function handleBackKey() {
    if (isTvFullscreen()) {
      setTvFullscreen(false);
      return;
    }

    var closeBtn = document.querySelector("#vodAdminMetaOverlay:not([hidden]) button, #vodClipOverlay:not([hidden]) button, .modal.open .close");
    if (closeBtn && isVisible(closeBtn)) {
      closeBtn.click();
      return;
    }

    var path = window.location.pathname || "/";
    if (path !== "/" && path !== "/index") {
      if (window.history.length > 1) {
        window.history.back();
      } else {
        window.location.href = "/";
      }
      return;
    }

    var now = Date.now();
    if (now - lastBackAt < 3000) {
      if (window.history.length > 1) {
        window.history.back();
      }
      return;
    }
    lastBackAt = now;
    showToast("Erneut „Zurück“ drücken, um zum TV-Startbildschirm zu wechseln");
  }

  window.addEventListener("keydown", function (e) {
    var code = e.keyCode || e.which;
    var active = document.activeElement;
    var tag = active && active.tagName ? active.tagName.toUpperCase() : "";
    var isTextInput = (tag === "INPUT" || tag === "TEXTAREA") && active.type !== "button" && active.type !== "submit" && active.type !== "checkbox" && active.type !== "radio" && active.type !== "range";

    if (code === KEY_RETURN || e.key === "XF86Back") {
      e.preventDefault();
      e.stopPropagation();
      handleBackKey();
      return;
    }

    var video = getActiveVideo();
    if (code === KEY_PLAY_PAUSE) {
      e.preventDefault();
      toggleVideoPlay(video);
      return;
    }
    if (code === KEY_PLAY && video) {
      e.preventDefault();
      wakeControlBar();
      try { video.play(); showToast("▶ Wiedergabe"); } catch (_) {}
      return;
    }
    if ((code === KEY_PAUSE || code === KEY_STOP) && video) {
      e.preventDefault();
      wakeControlBar();
      try { video.pause(); showToast("⏸ Pause"); } catch (_) {}
      return;
    }
    if (code === KEY_REWIND && video) {
      e.preventDefault();
      scrubVideoByKey(video, -1);
      return;
    }
    if (code === KEY_FF && video) {
      e.preventDefault();
      scrubVideoByKey(video, 1);
      return;
    }
    if (code === KEY_BLUE && video) {
      e.preventDefault();
      toggleVideoFullscreen();
      return;
    }

    if (code === KEY_LEFT || code === KEY_RIGHT || code === KEY_UP || code === KEY_DOWN) {
      if (isTextInput && (code === KEY_LEFT || code === KEY_RIGHT)) {
        return;
      }
      var isSeekbarFocused = !!(active && active.classList && active.classList.contains("vod-seekbar"));
      if (video && (isTvFullscreen() || active === video || isSeekbarFocused) && (code === KEY_LEFT || code === KEY_RIGHT)) {
        e.preventDefault();
        e.stopPropagation();
        scrubVideoByKey(video, code === KEY_RIGHT ? 1 : -1);
        return;
      }
      e.preventDefault();
      moveSpatialFocus(code);
      return;
    }

    if (code === KEY_ENTER) {
      if (active && (active.tagName === "VIDEO" || (active.classList && active.classList.contains("vod-seekbar")))) {
        e.preventDefault();
        e.stopPropagation();
        toggleVideoPlay(video || active);
        return;
      }
      if (active && active.classList && active.classList.contains("vod-cb-fullscreen")) {
        e.preventDefault();
        e.stopPropagation();
        toggleVideoFullscreen();
        return;
      }
      if (active && active !== document.body && !isTextInput && tag !== "BUTTON" && tag !== "A") {
        e.preventDefault();
        active.click();
      }
    }
  }, true);

  function initTv() {
    ensureTvStylesheet();
    enhanceVideoPage();
    setTimeout(function () {
      enhanceVideoPage();
      ensureInitialFocus();
    }, 180);
  }

  window.addEventListener("pageshow", function () {
    ensureTvStylesheet();
    enhanceVideoPage();
    setTimeout(ensureInitialFocus, 120);
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initTv);
  } else {
    initTv();
  }
})();
