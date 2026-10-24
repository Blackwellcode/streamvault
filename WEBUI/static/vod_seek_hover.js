/**
 * StreamVault – Custom VOD Control Bar
 * Replaces native browser controls with a unified, auto-hiding control bar.
 * Features: Play/Pause · Time display · Seekbar (with buffer + thumbnail tooltip) · Volume · Fullscreen
 * Auto-hides after 3s of inactivity, re-appears on mouse/touch/keyboard.
 */
(function () {
  "use strict";

  function isFiniteNumber(v) {
    return typeof v === "number" && Number.isFinite(v);
  }

  function formatTime(seconds) {
    var total = Math.max(0, Math.floor(Number(seconds) || 0));
    var h = Math.floor(total / 3600);
    var m = Math.floor((total % 3600) / 60);
    var s = total % 60;
    if (h > 0) {
      return h + ":" + String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
    }
    return m + ":" + String(s).padStart(2, "0");
  }

  function clamp01(v) { return Math.max(0, Math.min(1, v)); }

  function durationOf(video, knownDuration) {
    if (isFiniteNumber(video.duration) && video.duration > 0) return video.duration;
    if (isFiniteNumber(knownDuration) && knownDuration > 0) return knownDuration;
    return NaN;
  }

  function dbg(kind, detail) {
    try {
      if (window.StreamVaultVodDebug && typeof window.StreamVaultVodDebug.log === "function") {
        window.StreamVaultVodDebug.log("seekbar", kind, detail || {});
      }
    } catch (_) {}
  }

  /* ── SVG icons ────────────────────────────────────────────────────── */
  var ICON_PLAY = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>';
  var ICON_PAUSE = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>';
  var ICON_VOL_ON = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02zM14 3.23v2.06c2.89.86 5 3.54 5 6.71s-2.11 5.85-5 6.71v2.06c4.01-.91 7-4.49 7-8.77s-2.99-7.86-7-8.77z"/></svg>';
  var ICON_VOL_OFF = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M16.5 12c0-1.77-1.02-3.29-2.5-4.03v2.21l2.45 2.45c.03-.2.05-.41.05-.63zm2.5 0c0 .94-.2 1.82-.54 2.64l1.51 1.51C20.63 14.91 21 13.5 21 12c0-4.28-2.99-7.86-7-8.77v2.06c2.89.86 5 3.54 5 6.71zM4.27 3L3 4.27 7.73 9H3v6h4l5 5v-6.73l4.25 4.25c-.67.52-1.42.93-2.25 1.18v2.06c1.38-.31 2.63-.95 3.69-1.81L19.73 21 21 19.73l-9-9L4.27 3zM12 4L9.91 6.09 12 8.18V4z"/></svg>';
  var ICON_FULL = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z"/></svg>';
  var ICON_EXIT_FULL = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M5 16h3v3h2v-5H5v2zm3-8H5v2h5V5H8v3zm6 11h2v-3h3v-2h-5v5zm2-11V5h-2v5h5V8h-3z"/></svg>';

  /* ── Main attach function ─────────────────────────────────────────── */
  function attach(video, options) {
    if (!video || video._streamVaultSeekHoverInstalled) return;
    video._streamVaultSeekHoverInstalled = true;

    var opts = options || {};
    var knownDuration = Number(opts.knownDuration) || 0;

    var host = opts.host
      || video.closest(".player-wrapper")
      || video.closest(".video-container")
      || video.parentElement;
    if (!host) return;

    if (window.getComputedStyle(host).position === "static") {
      host.style.position = "relative";
    }
    host.classList.add("has-vod-seekbar");
    // Custom bar replaces native controls — avoid double UI / layout spill.
    try { video.controls = false; } catch (_) {}
    try { video.removeAttribute("controls"); } catch (_) {}

    /* ── Build DOM ──────────────────────────────────────────────────── */
    var bar = document.createElement("div");
    bar.className = "vod-controlbar";
    bar.setAttribute("role", "toolbar");
    bar.setAttribute("aria-label", "Videoplayer-Steuerung");

    bar.innerHTML = [
      /* Play/Pause */
      '<button class="vod-cb-btn vod-cb-play" aria-label="Wiedergabe / Pause" type="button">',
        ICON_PLAY,
      '</button>',

      /* Time */
      '<span class="vod-cb-time"><span class="vod-cb-current">0:00</span><span class="vod-cb-sep"> / </span><span class="vod-cb-total">--:--</span></span>',

      /* Seekbar (flex-grows to fill available space) */
      '<div class="vod-seekbar" tabindex="0" role="slider" aria-label="Fortschritt" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">',
        '<div class="vod-seekbar__track">',
          '<div class="vod-seekbar__buffered"></div>',
          '<div class="vod-seekbar__played"></div>',
          '<div class="vod-seekbar__thumb"></div>',
        '</div>',
        '<div class="vod-seekbar__tip" hidden></div>',
      '</div>',

      /* Volume */
      '<button class="vod-cb-btn vod-cb-mute" aria-label="Stumm schalten" type="button">',
        ICON_VOL_ON,
      '</button>',
      '<input class="vod-cb-vol" type="range" min="0" max="1" step="0.02" value="1" aria-label="Lautstärke">',

      /* Fullscreen */
      '<button class="vod-cb-btn vod-cb-fullscreen" aria-label="Vollbild" type="button">',
        ICON_FULL,
      '</button>',
    ].join("");

    host.appendChild(bar);

    /* ── Refs ───────────────────────────────────────────────────────── */
    var playBtn     = bar.querySelector(".vod-cb-play");
    var currentEl   = bar.querySelector(".vod-cb-current");
    var totalEl     = bar.querySelector(".vod-cb-total");
    var seekEl      = bar.querySelector(".vod-seekbar");
    var trackEl     = bar.querySelector(".vod-seekbar__track");
    var playedEl    = bar.querySelector(".vod-seekbar__played");
    var bufferedEl  = bar.querySelector(".vod-seekbar__buffered");
    var thumbEl     = bar.querySelector(".vod-seekbar__thumb");
    var tipEl       = bar.querySelector(".vod-seekbar__tip");
    var muteBtn     = bar.querySelector(".vod-cb-mute");
    var volSlider   = bar.querySelector(".vod-cb-vol");
    var fsBtn       = bar.querySelector(".vod-cb-fullscreen");

    /* ── State ──────────────────────────────────────────────────────── */
    var dragging = false;
    var activeTarget = NaN;
    var hideTimer = null;
    var HIDE_DELAY = 3000;

    /* ── Auto-hide ──────────────────────────────────────────────────── */
    function showControls() {
      bar.classList.remove("vod-controlbar--hidden");
      host.style.cursor = "";
      resetHideTimer();
    }

    function hideControls() {
      if (dragging || video.paused) return;
      bar.classList.add("vod-controlbar--hidden");
      host.style.cursor = "none";
    }

    function resetHideTimer() {
      clearTimeout(hideTimer);
      if (!video.paused) {
        hideTimer = setTimeout(hideControls, HIDE_DELAY);
      }
    }

    host.addEventListener("mousemove", showControls);
    video._streamVaultShowControls = showControls;
    host.addEventListener("mouseleave", function () {
      clearTimeout(hideTimer);
      if (!video.paused && !dragging) {
        bar.classList.add("vod-controlbar--hidden");
        host.style.cursor = "";
      }
    });
    bar.addEventListener("mouseenter", function () {
      clearTimeout(hideTimer);
      bar.classList.remove("vod-controlbar--hidden");
    });
    bar.addEventListener("mouseleave", resetHideTimer);
    host.addEventListener("touchstart", showControls, { passive: true });

    /* ── Keyboard shortcuts ─────────────────────────────────────────── */
    host.setAttribute("tabindex", "-1");
    host.addEventListener("keydown", function (e) {
      var dur = durationOf(video, knownDuration);
      switch (e.key) {
        case " ": case "k": e.preventDefault(); togglePlay(); break;
        case "ArrowRight": e.preventDefault(); if (isFiniteNumber(dur)) video.currentTime = Math.min(dur, video.currentTime + 5); break;
        case "ArrowLeft":  e.preventDefault(); video.currentTime = Math.max(0, video.currentTime - 5); break;
        case "ArrowUp":    e.preventDefault(); video.volume = Math.min(1, video.volume + 0.1); break;
        case "ArrowDown":  e.preventDefault(); video.volume = Math.max(0, video.volume - 0.1); break;
        case "m": e.preventDefault(); video.muted = !video.muted; break;
        case "f": e.preventDefault(); toggleFullscreen(); break;
      }
    });

    /* ── Play / Pause ───────────────────────────────────────────────── */
    function togglePlay() {
      if (typeof video._streamVaultSetUserPaused === "function") {
        video._streamVaultSetUserPaused(!video.paused);
      }
      if (video.paused) {
        if (typeof video._streamVaultRequestPlay === "function") {
          try { video._streamVaultRequestPlay(); } catch (_) {}
        } else {
          var p = video.play();
          if (p && p.catch) p.catch(function () {});
        }
      } else {
        video.pause();
      }
    }

    playBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      togglePlay();
    });

    // Click on video itself toggles play/pause
    video.addEventListener("click", function () { togglePlay(); });

    function updatePlayBtn() {
      playBtn.innerHTML = video.paused ? ICON_PLAY : ICON_PAUSE;
      playBtn.setAttribute("aria-label", video.paused ? "Wiedergabe" : "Pause");
      if (video.paused) {
        showControls();
        clearTimeout(hideTimer);
      } else {
        resetHideTimer();
      }
    }
    video.addEventListener("play", updatePlayBtn);
    video.addEventListener("pause", updatePlayBtn);
    video.addEventListener("ended", updatePlayBtn);

    /* ── Volume ─────────────────────────────────────────────────────── */
    function updateVolBtn() {
      muteBtn.innerHTML = (video.muted || video.volume === 0) ? ICON_VOL_OFF : ICON_VOL_ON;
      volSlider.value = video.muted ? 0 : video.volume;
    }
    muteBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      video.muted = !video.muted;
    });
    volSlider.addEventListener("input", function (e) {
      e.stopPropagation();
      video.volume = parseFloat(volSlider.value);
      video.muted = video.volume === 0;
    });
    video.addEventListener("volumechange", updateVolBtn);

    /* ── Fullscreen ─────────────────────────────────────────────────── */
    function toggleFullscreen() {
      if (document.body && document.body.classList.contains("page-mobile-player")
          && window.StreamVaultMobileLandscape) {
        window.StreamVaultMobileLandscape.toggle();
        var forced = window.StreamVaultMobileLandscape.isForced();
        fsBtn.innerHTML = forced ? ICON_EXIT_FULL : ICON_FULL;
        fsBtn.setAttribute("aria-label", forced ? "Vollbild beenden" : "Vollbild");
        return;
      }
      if (!document.fullscreenElement) {
        (host.requestFullscreen || host.webkitRequestFullscreen || function(){}).call(host);
      } else {
        (document.exitFullscreen || document.webkitExitFullscreen || function(){}).call(document);
      }
    }
    fsBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      toggleFullscreen();
    });
    document.addEventListener("fullscreenchange", function () {
      if (document.body && document.body.classList.contains("page-mobile-player")
          && window.StreamVaultMobileLandscape) {
        var forced = window.StreamVaultMobileLandscape.isForced();
        fsBtn.innerHTML = forced ? ICON_EXIT_FULL : ICON_FULL;
        fsBtn.setAttribute("aria-label", forced ? "Vollbild beenden" : "Vollbild");
        return;
      }
      fsBtn.innerHTML = document.fullscreenElement ? ICON_EXIT_FULL : ICON_FULL;
      fsBtn.setAttribute("aria-label", document.fullscreenElement ? "Vollbild beenden" : "Vollbild");
    });
    function syncMobileFsIcon() {
      if (!window.StreamVaultMobileLandscape) return;
      var forced = window.StreamVaultMobileLandscape.isForced();
      fsBtn.innerHTML = forced ? ICON_EXIT_FULL : ICON_FULL;
      fsBtn.setAttribute("aria-label", forced ? "Vollbild beenden" : "Vollbild");
    }
    if (document.body) {
      document.body.addEventListener("streamvault:landscape-chat", syncMobileFsIcon);
      document.body.addEventListener("streamvault:landscape-force", syncMobileFsIcon);
    }

    /* ── Seekbar UI update ──────────────────────────────────────────── */
    function updateUi() {
      var dur = durationOf(video, knownDuration);
      var cur = isFiniteNumber(video.currentTime) ? video.currentTime : 0;

      currentEl.textContent = formatTime(cur);
      if (isFiniteNumber(dur)) {
        totalEl.textContent = formatTime(dur);
        var pct = clamp01(cur / dur) * 100;
        playedEl.style.width = pct + "%";
        thumbEl.style.left = pct + "%";
        seekEl.setAttribute("aria-valuenow", Math.round(pct));

        // buffered
        var bufPct = 0;
        try {
          var ranges = video.buffered;
          for (var i = 0; i < ranges.length; i++) {
            if (ranges.start(i) <= cur + 0.5) {
              bufPct = clamp01(ranges.end(i) / dur) * 100;
            }
          }
        } catch (_) {}
        bufferedEl.style.width = bufPct + "%";
      } else {
        totalEl.textContent = "--:--";
        playedEl.style.width = "0%";
        thumbEl.style.left = "0%";
        bufferedEl.style.width = "0%";
      }
    }

    /* ── Seekbar interaction ────────────────────────────────────────── */
    function fractionFromEvent(e) {
      var rect = trackEl.getBoundingClientRect();
      return clamp01((e.clientX - rect.left) / rect.width);
    }

    function showTip(frac) {
      var dur = durationOf(video, knownDuration);
      if (!isFiniteNumber(dur)) return;
      tipEl.textContent = formatTime(frac * dur);
      tipEl.style.left = (frac * 100) + "%";
      tipEl.hidden = false;
    }

    function hideTip() {
      tipEl.hidden = true;
    }

    function noteUserSeek(seconds) {
      if (typeof video._streamVaultNoteUserSeek === "function") {
        try { video._streamVaultNoteUserSeek(seconds); } catch (_) {}
      }
    }

    var lastCommitAt = 0;

    function commitSeek(e) {
      var now = Date.now();
      if (now - lastCommitAt < 250) {
        return;
      }
      lastCommitAt = now;

      var dur = durationOf(video, knownDuration);
      if (!isFiniteNumber(dur)) return;
      dragging = false;
      seekEl.classList.remove("is-dragging");
      seekEl.releasePointerCapture && seekEl.releasePointerCapture(e.pointerId);
      var frac = fractionFromEvent(e);
      var target = frac * dur;

      noteUserSeek(target);
      if (typeof video._streamVaultArmSeekPlayIntent === "function") {
        try { video._streamVaultArmSeekPlayIntent(); } catch (_) {}
      }
      if (typeof video._streamVaultSetUserPaused === "function") {
        try { video._streamVaultSetUserPaused(false); } catch (_) {}
      }
      video.currentTime = target;

      // Actively kick decode / playback at the seek target
      if (typeof video._streamVaultRequestPlay === "function") {
        try { video._streamVaultRequestPlay(); } catch (_) {}
      } else {
        var p = video.play();
        if (p && p.catch) p.catch(function () {});
      }

      hideTip();
      updateUi();
    }

    seekEl.addEventListener("pointermove", function (e) {
      showControls();
      var frac = fractionFromEvent(e);
      showTip(frac);
      if (dragging) {
        var dur = durationOf(video, knownDuration);
        if (isFiniteNumber(dur)) {
          activeTarget = frac * dur;
          playedEl.style.width = (frac * 100) + "%";
          thumbEl.style.left = (frac * 100) + "%";
        }
      }
    });

    seekEl.addEventListener("pointerleave", function () {
      if (!dragging) hideTip();
    });

    seekEl.addEventListener("pointerdown", function (e) {
      if (e.button !== 0) return;
      e.preventDefault();
      e.stopPropagation();
      dragging = true;
      seekEl.classList.add("is-dragging");
      seekEl.setPointerCapture && seekEl.setPointerCapture(e.pointerId);
      var frac = fractionFromEvent(e);
      showTip(frac);
    });

    seekEl.addEventListener("pointerup", function (e) {
      if (!dragging) return;
      e.preventDefault();
      e.stopPropagation();
      commitSeek(e);
    });

    seekEl.addEventListener("pointercancel", function () {
      dragging = false;
      seekEl.classList.remove("is-dragging");
      hideTip();
    });

    seekEl.addEventListener("click", function (e) {
      e.preventDefault();
      e.stopPropagation();
      commitSeek(e);
    });

    /* ── Video events ───────────────────────────────────────────────── */
    video.addEventListener("timeupdate", function () { if (!dragging) updateUi(); });
    video.addEventListener("progress", updateUi);
    video.addEventListener("seeked", function () { if (!dragging) updateUi(); });
    video.addEventListener("loadedmetadata", updateUi);
    video.addEventListener("durationchange", updateUi);

    /* Init */
    updateUi();
    updatePlayBtn();
    updateVolBtn();
  }

  window.StreamVaultSeekHover = {
    attach: attach,
    formatTime: formatTime,
  };
})();
