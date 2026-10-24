(function () {
  "use strict";

  var CLIP_DEFAULT_DURATION = 30;
  var CLIP_MAX_DURATION = 300;
  var CLIP_MIN_DURATION = 1;
  var CLIENT_DEBOUNCE_MS = 400;
  var POLL_MS = 400;
  var POLL_TIMEOUT_MS = 180000;

  function isNumber(value) {
    return Number.isFinite(value) && value >= 0;
  }

  function formatTime(seconds) {
    var safe = Math.max(0, Math.floor(Number(seconds) || 0));
    var hours = Math.floor(safe / 3600);
    var minutes = Math.floor((safe % 3600) / 60);
    var rest = safe % 60;
    if (hours > 0) {
      return hours + ":" + String(minutes).padStart(2, "0") + ":" + String(rest).padStart(2, "0");
    }
    return String(minutes).padStart(2, "0") + ":" + String(rest).padStart(2, "0");
  }

  function seekableBounds(player) {
    try {
      if (player && player.seekable && player.seekable.length) {
        var start = player.seekable.start(0);
        var end = player.seekable.end(player.seekable.length - 1);
        if (isNumber(start) && isNumber(end) && end > start) {
          return { start: start, end: end };
        }
      }
    } catch (_) {}
    var duration = Number(player && player.duration);
    if (isNumber(duration) && duration > 1 && Number.isFinite(duration)) {
      return { start: 0, end: duration };
    }
    return null;
  }

  function el() {
    for (var i = 0; i < arguments.length; i++) {
      var node = document.getElementById(arguments[i]);
      if (node) return node;
    }
    return null;
  }

  function dataAttr(node, names) {
    if (!node || !node.dataset) return "";
    for (var i = 0; i < names.length; i++) {
      var value = node.dataset[names[i]];
      if (value) return value;
    }
    return "";
  }

  function csrfToken() {
    var field = document.querySelector("#vodClipForm input[name='csrf_token'], #vodClipForm input[name='csrf_token'], input[name='csrf_token']");
    return field ? field.value : "";
  }

  function loadSettings(overrides) {
    var settings = {
      hotkey: "F8",
      preroll_sec: 30,
      postroll_sec: 10,
      quality: "fast",
      include_audio: true
    };
    if (window.CLIP_SETTINGS && typeof window.CLIP_SETTINGS === "object") {
      Object.assign(settings, window.CLIP_SETTINGS);
    }
    if (overrides && typeof overrides === "object") {
      Object.assign(settings, overrides);
    }
    return settings;
  }

  function ensureToast() {
    var toast = document.getElementById("clipToast");
    if (toast) return toast;
    toast = document.createElement("div");
    toast.id = "clipToast";
    toast.className = "clip-toast";
    toast.hidden = true;
    toast.setAttribute("aria-live", "polite");
    toast.innerHTML =
      '<div class="clip-toast-kicker"></div>' +
      '<div class="clip-toast-msg"></div>' +
      '<a class="clip-toast-link" hidden></a>';
    document.body.appendChild(toast);
    return toast;
  }

  var toastTimer = null;
  function showToast(state, kicker, message, linkUrl) {
    var toast = ensureToast();
    toast.hidden = false;
    toast.dataset.state = state || "";
    toast.querySelector(".clip-toast-kicker").textContent = kicker || "";
    toast.querySelector(".clip-toast-msg").textContent = message || "";
    var link = toast.querySelector(".clip-toast-link");
    if (linkUrl) {
      link.hidden = false;
      link.href = linkUrl;
      link.textContent = "Clip öffnen";
    } else {
      link.hidden = true;
      link.removeAttribute("href");
      link.textContent = "";
    }
    if (toastTimer) window.clearTimeout(toastTimer);
    if (state === "success") {
      toastTimer = window.setTimeout(function () { toast.hidden = true; }, 4500);
    } else if (state === "error") {
      toastTimer = window.setTimeout(function () { toast.hidden = true; }, 6500);
    }
  }

  function initLiveClipOverlay(options) {
    var opts = options || {};
    var vPlayer = opts.player || el("player", "vodPlayer", "proxyPlayer");
    var btnOpenEditor = opts.openButton || el("btnOpenEditor", "btnOpenEditor");
    var btnInstantClip = opts.instantButton || el("btnInstantClip", "btnInstantClip");
    var clipOverlay = opts.overlay || el("vodClipOverlay", "vodClipOverlay");
    var clipForm = opts.form || el("vodClipForm", "vodClipForm");
    var settings = loadSettings(opts.settings);
    var instantUrl = opts.instantUrl || dataAttr(btnInstantClip, ["instantUrl"]) || "";
    var source = opts.source || dataAttr(btnInstantClip, ["clipSource"]) || dataAttr(btnOpenEditor, ["clipSource"]) || "live";
    var filename = opts.filename || dataAttr(btnInstantClip, ["filename"]) || "";
    var lastInstantAt = 0;
    var pollTimer = null;

    var clipPreviewVideo = document.getElementById("clipPreviewVideo");
    var clipPreviewToggle = document.getElementById("clipPreviewToggle");
    var clipStartRange = document.getElementById("clipStartRange");
    var clipEndRange = document.getElementById("clipEndRange");
    var clipRangeFill = document.getElementById("clipRangeFill");
    var clipStartInput = document.getElementById("clipStartInput");
    var clipDurationInput = document.getElementById("clipDurationInput");
    var clipStartLabel = document.getElementById("clipStartLabel");
    var clipEndLabel = document.getElementById("clipEndLabel");
    var clipDurationLabel = document.getElementById("clipDurationLabel");
    var clipTitleInput = document.getElementById("clipTitleInput");
    var clipStatus = document.getElementById("clipStatus");
    var clipResult = document.getElementById("clipResult");
    var clipSaveButton = document.getElementById("clipSaveButton");
    var clipFullEditorLink = document.getElementById("clipFullEditorLink");
    var mediaUrl = opts.mediaUrl || dataAttr(btnOpenEditor, ["mediaUrl"]) || "";

    var clipStart = 0;
    var clipEnd = CLIP_DEFAULT_DURATION;
    var clipMax = CLIP_DEFAULT_DURATION;
    var clipOrigin = 0;
    var clipInitialStart = 0;
    var clipInitialEnd = CLIP_DEFAULT_DURATION;
    var clipInitialTitle = "";
    var clipDirty = false;
    var clipSubmitting = false;
    var previewHls = null;
    var previewReady = false;
    var previewWantPlaying = false;
    var pendingPreviewOffset = null;
    var mainMuteState = null;
    var seekRetryTimer = null;
    var growTimer = null;

    function overlayOpen() {
      return Boolean(clipOverlay && !clipOverlay.hidden);
    }

    function setClipStatus(message, state) {
      if (!clipStatus) return;
      clipStatus.textContent = message || "";
      clipStatus.dataset.state = state || "";
    }

    function setClipPreviewIcon(isPlaying) {
      if (!clipPreviewToggle) return;
      clipPreviewToggle.innerHTML = isPlaying
        ? '<svg aria-hidden="true" viewBox="0 0 24 24"><path d="M8 5v14M16 5v14"/></svg>'
        : '<svg aria-hidden="true" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>';
      clipPreviewToggle.title = isPlaying ? "Vorschau pausieren" : "Vorschau starten";
      clipPreviewToggle.setAttribute("aria-label", clipPreviewToggle.title);
    }

    function setPreviewMuted(muted) {
      if (!clipPreviewVideo) return;
      clipPreviewVideo.muted = !!muted;
      clipPreviewVideo.defaultMuted = !!muted;
      if (muted) clipPreviewVideo.setAttribute("muted", "");
      else clipPreviewVideo.removeAttribute("muted");
    }

    function refreshClipWindow(preferPreview) {
      var bounds = null;
      if (preferPreview && previewReady) bounds = seekableBounds(clipPreviewVideo);
      if (!bounds) bounds = seekableBounds(vPlayer);
      if (!bounds && previewReady) bounds = seekableBounds(clipPreviewVideo);
      if (!bounds) {
        clipOrigin = 0;
        return CLIP_DEFAULT_DURATION;
      }
      clipOrigin = bounds.start;
      return Math.max(CLIP_MIN_DURATION, bounds.end - bounds.start);
    }

    function muteMainForClip() {
      if (mainMuteState || !vPlayer) return;
      mainMuteState = { muted: !!vPlayer.muted, volume: Number(vPlayer.volume) };
      vPlayer.muted = true;
    }

    function restoreMainAfterClip() {
      if (!mainMuteState || !vPlayer) return;
      var state = mainMuteState;
      mainMuteState = null;
      vPlayer.muted = state.muted;
      if (isNumber(state.volume)) vPlayer.volume = state.volume;
    }

    function destroyPreviewHls() {
      if (!previewHls) return;
      try { previewHls.destroy(); } catch (_) {}
      previewHls = null;
    }

    function clearSeekRetry() {
      if (!seekRetryTimer) return;
      window.clearTimeout(seekRetryTimer);
      seekRetryTimer = null;
    }

    function setClipRangeMax(maxSeconds) {
      clipMax = Math.max(CLIP_MIN_DURATION, Number(maxSeconds) || CLIP_DEFAULT_DURATION);
      if (clipStartRange) clipStartRange.max = String(clipMax);
      if (clipEndRange) clipEndRange.max = String(clipMax);
    }

    function clampClipRange(start, end, movedHandle) {
      setClipRangeMax(refreshClipWindow(previewReady));
      var nextStart = Math.max(0, Math.min(Number(start) || 0, clipMax - CLIP_MIN_DURATION));
      var nextEnd = Math.max(CLIP_MIN_DURATION, Math.min(Number(end) || CLIP_MIN_DURATION, clipMax));
      if (nextEnd - nextStart < CLIP_MIN_DURATION) {
        if (movedHandle === "start") nextStart = Math.max(0, nextEnd - CLIP_MIN_DURATION);
        else nextEnd = Math.min(clipMax, nextStart + CLIP_MIN_DURATION);
      }
      if (nextEnd - nextStart > CLIP_MAX_DURATION) {
        if (movedHandle === "start") nextEnd = Math.min(clipMax, nextStart + CLIP_MAX_DURATION);
        else nextStart = Math.max(0, nextEnd - CLIP_MAX_DURATION);
      }
      return [nextStart, nextEnd];
    }

    function updateClipUi(local) {
      local = local || {};
      var duration = Math.max(0, clipEnd - clipStart);
      if (clipStartLabel) clipStartLabel.textContent = formatTime(clipStart);
      if (clipEndLabel) clipEndLabel.textContent = formatTime(clipEnd);
      if (clipDurationLabel) clipDurationLabel.textContent = duration.toFixed(1) + "s";
      if (clipStartInput) clipStartInput.value = clipStart.toFixed(2);
      if (clipDurationInput) clipDurationInput.value = duration.toFixed(2);
      if (clipStartRange) clipStartRange.value = clipStart.toFixed(1);
      if (clipEndRange) clipEndRange.value = clipEnd.toFixed(1);
      if (clipRangeFill) {
        var startPct = clipMax > 0 ? (clipStart / clipMax) * 100 : 0;
        var endPct = clipMax > 0 ? (clipEnd / clipMax) * 100 : 0;
        clipRangeFill.style.left = startPct + "%";
        clipRangeFill.style.width = Math.max(0, endPct - startPct) + "%";
      }
      if (clipFullEditorLink && btnOpenEditor) {
        clipFullEditorLink.href = btnOpenEditor.href + "?t=" + encodeURIComponent(clipStart.toFixed(2));
      }
      if (clipSaveButton) {
        clipSaveButton.disabled = clipSubmitting || duration < CLIP_MIN_DURATION;
      }
      if (local.seekPreview) seekClipPreview(clipStart);
    }

    function setClipRange(start, end, local) {
      local = local || {};
      var clamped = clampClipRange(start, end, local.movedHandle);
      clipStart = clamped[0];
      clipEnd = clamped[1];
      if (local.dirty) clipDirty = true;
      updateClipUi(local);
    }

    function applyPreviewSeek(offsetSeconds, done) {
      if (!clipPreviewVideo || !isNumber(offsetSeconds)) {
        if (done) done(false);
        return;
      }
      if (!previewReady) {
        pendingPreviewOffset = offsetSeconds;
        if (done) done(false);
        return;
      }
      refreshClipWindow(true);
      var maxOffset = Math.max(0, clipMax - 0.05);
      var safeOffset = Math.max(0, Math.min(offsetSeconds, maxOffset));
      var target = clipOrigin + safeOffset;
      clearSeekRetry();
      var trySeek = function (attempt) {
        try {
          var bounds = seekableBounds(clipPreviewVideo);
          if (bounds) {
            clipPreviewVideo.currentTime = Math.min(bounds.end - 0.05, Math.max(bounds.start, target));
          } else {
            clipPreviewVideo.currentTime = target;
          }
        } catch (_) {}
        var current = Number(clipPreviewVideo.currentTime) || 0;
        if (Math.abs(current - target) >= 1.25 && attempt < 14) {
          seekRetryTimer = window.setTimeout(function () { trySeek(attempt + 1); }, 140);
          return;
        }
        if (done) done(true);
      };
      trySeek(0);
    }

    function seekClipPreview(offsetSeconds) {
      applyPreviewSeek(offsetSeconds, null);
    }

    function startPreviewPlayback() {
      if (!clipPreviewVideo) return;
      setPreviewMuted(false);
      var playPromise = clipPreviewVideo.play();
      if (playPromise && typeof playPromise.then === "function") {
        playPromise.then(function () {
          setPreviewMuted(false);
          setClipPreviewIcon(true);
        }).catch(function () {
          setPreviewMuted(true);
          var retry = clipPreviewVideo.play();
          if (retry && typeof retry.then === "function") {
            retry.then(function () {
              setPreviewMuted(false);
              setClipPreviewIcon(true);
            }).catch(function () { setClipPreviewIcon(false); });
          }
        });
      } else {
        setClipPreviewIcon(!clipPreviewVideo.paused);
      }
    }

    function playClipPreview() {
      if (!clipPreviewVideo) return;
      previewWantPlaying = true;
      ensureClipPreviewSource();
      var currentOffset = Math.max(0, (Number(clipPreviewVideo.currentTime) || 0) - clipOrigin);
      if (!previewReady || currentOffset < clipStart - 0.2 || currentOffset >= clipEnd) {
        applyPreviewSeek(clipStart, function () {
          if (previewWantPlaying) startPreviewPlayback();
        });
        return;
      }
      startPreviewPlayback();
    }

    function pauseClipPreview() {
      previewWantPlaying = false;
      if (!clipPreviewVideo) return;
      clipPreviewVideo.pause();
      setClipPreviewIcon(false);
    }

    function onPreviewMediaReady() {
      if (previewReady) return;
      previewReady = true;
      var maxSeconds = refreshClipWindow(true);
      setClipRangeMax(maxSeconds);
      var clamped = clampClipRange(clipStart, clipEnd);
      clipStart = clamped[0];
      clipEnd = clamped[1];
      updateClipUi({});
      var target = pendingPreviewOffset != null ? pendingPreviewOffset : clipStart;
      pendingPreviewOffset = null;
      applyPreviewSeek(target, function () {
        if (previewWantPlaying) startPreviewPlayback();
      });
    }

    function ensureClipPreviewSource() {
      if (!clipPreviewVideo || !mediaUrl) return;
      if (previewHls || clipPreviewVideo.getAttribute("src")) return;
      if (window.Hls && window.Hls.isSupported() && /\.m3u8(\?|$)/i.test(mediaUrl)) {
        previewHls = new window.Hls({
          lowLatencyMode: false,
          liveSyncDurationCount: 3,
          maxBufferLength: 90,
          backBufferLength: 180,
          enableWorker: true
        });
        previewHls.on(window.Hls.Events.MANIFEST_PARSED, onPreviewMediaReady);
        previewHls.loadSource(mediaUrl);
        previewHls.attachMedia(clipPreviewVideo);
        return;
      }
      clipPreviewVideo.addEventListener("loadedmetadata", onPreviewMediaReady, { once: true });
      clipPreviewVideo.src = mediaUrl;
      clipPreviewVideo.load();
    }

    function openClipOverlay() {
      if (!clipOverlay) return;
      // Instant clip works without a local <video>; trim needs time bounds from player or media URL.
      if (!vPlayer && !mediaUrl) return;
      if (vPlayer) muteMainForClip();
      var maxSeconds = refreshClipWindow(false);
      if (!isNumber(maxSeconds) || maxSeconds < CLIP_MIN_DURATION) {
        maxSeconds = CLIP_DEFAULT_DURATION;
      }
      setClipRangeMax(maxSeconds);
      var playerTime = (vPlayer && isNumber(vPlayer.currentTime)) ? vPlayer.currentTime : clipOrigin;
      var nextEnd = Math.min(clipMax, Math.max(CLIP_MIN_DURATION, playerTime - clipOrigin));
      var nextStart = Math.max(0, nextEnd - Math.min(CLIP_DEFAULT_DURATION, clipMax));
      if (nextEnd - nextStart < Math.min(CLIP_DEFAULT_DURATION, clipMax) && clipMax >= CLIP_DEFAULT_DURATION) {
        nextEnd = Math.min(clipMax, nextStart + CLIP_DEFAULT_DURATION);
      }
      clipInitialStart = nextStart;
      clipInitialEnd = nextEnd;
      clipInitialTitle = "";
      clipDirty = false;
      clipSubmitting = false;
      previewReady = false;
      pendingPreviewOffset = nextStart;
      previewWantPlaying = true;
      if (clipTitleInput) clipTitleInput.value = "";
      if (clipResult) {
        clipResult.hidden = true;
        clipResult.replaceChildren();
      }
      setClipStatus(maxSeconds < 5 ? "Puffer noch kurz — das Clip-Fenster wächst mit." : "", "");
      setClipRange(nextStart, nextEnd, { dirty: false, seekPreview: false });
      clipOverlay.hidden = false;
      clipOverlay.setAttribute("aria-hidden", "false");
      document.body.classList.add("clip-overlay-open");
      ensureClipPreviewSource();
      playClipPreview();
      if (!growTimer) {
        growTimer = window.setInterval(function () {
          if (clipOverlay.hidden) return;
          var nextMax = refreshClipWindow(previewReady);
          if (nextMax > clipMax + 0.4) {
            setClipRangeMax(nextMax);
            updateClipUi({});
          }
        }, 2000);
      }
    }

    function clipHasUnsavedChanges() {
      var titleChanged = ((clipTitleInput && clipTitleInput.value) || "").trim() !== clipInitialTitle;
      var rangeChanged = Math.abs(clipStart - clipInitialStart) > 0.2 || Math.abs(clipEnd - clipInitialEnd) > 0.2;
      return clipDirty || titleChanged || rangeChanged;
    }

    function closeClipOverlay(force) {
      if (!clipOverlay || clipOverlay.hidden) return;
      if (!force && clipHasUnsavedChanges()) {
        if (!window.confirm("Clip verwerfen? Nicht gespeicherte Änderungen gehen verloren.")) return;
      }
      pauseClipPreview();
      clearSeekRetry();
      if (growTimer) {
        window.clearInterval(growTimer);
        growTimer = null;
      }
      destroyPreviewHls();
      previewReady = false;
      pendingPreviewOffset = null;
      if (clipPreviewVideo) {
        setPreviewMuted(true);
        clipPreviewVideo.removeAttribute("src");
        clipPreviewVideo.load();
      }
      clipOverlay.hidden = true;
      clipOverlay.setAttribute("aria-hidden", "true");
      document.body.classList.remove("clip-overlay-open");
      restoreMainAfterClip();
    }

    function stopPolling() {
      if (pollTimer) {
        window.clearTimeout(pollTimer);
        pollTimer = null;
      }
    }

    function pollJob(jobId) {
      stopPolling();
      var started = Date.now();
      var tick = function () {
        fetch("/clip/job/" + encodeURIComponent(jobId), {
          headers: { Accept: "application/json", "X-Requested-With": "XMLHttpRequest" }
        }).then(function (response) {
          return response.json().then(function (data) {
            return { okHttp: response.ok, data: data };
          });
        }).then(function (result) {
          var data = result.data || {};
          var status = data.status || "";
          if (status === "done") {
            showToast("success", "✓ CLIP GESPEICHERT", data.message || "Clip gespeichert.", data.clip_url || "");
            return;
          }
          if (status === "error" || !result.okHttp) {
            showToast("error", "✕ CLIP FEHLGESCHLAGEN", data.message || data.error || "Clip fehlgeschlagen.");
            return;
          }
          if (Date.now() - started > POLL_TIMEOUT_MS) {
            showToast("error", "✕ CLIP FEHLGESCHLAGEN", "Clip dauert zu lange.");
            return;
          }
          showToast("pending", "● CLIP", data.message || "Clip wird erstellt...");
          pollTimer = window.setTimeout(tick, POLL_MS);
        }).catch(function () {
          showToast("error", "✕ CLIP FEHLGESCHLAGEN", "Status konnte nicht geladen werden.");
        });
      };
      tick();
    }

    function triggerInstantClip() {
      if (!instantUrl) {
        showToast("error", "✕ CLIP FEHLGESCHLAGEN", "Clip-URL fehlt auf dieser Seite.");
        return;
      }
      var now = Date.now();
      if (now - lastInstantAt < CLIENT_DEBOUNCE_MS) {
        showToast("pending", "● CLIP", "Clip wird bereits gestartet…");
        return;
      }
      lastInstantAt = now;
      showToast("pending", "● CLIP", "Clip wird erstellt...");
      var body = new FormData();
      body.set("csrf_token", csrfToken());
      body.set("source", source);
      body.set("title", "");
      if (filename) body.set("filename", filename);
      if (vPlayer && isNumber(vPlayer.currentTime)) {
        body.set("position", String(vPlayer.currentTime.toFixed(2)));
      }
      fetch(instantUrl, {
        method: "POST",
        body: body,
        headers: {
          "X-Requested-With": "XMLHttpRequest",
          Accept: "application/json"
        }
      }).then(function (response) {
        return response.json().then(function (data) {
          return { okHttp: response.ok, data: data };
        }).catch(function () {
          return {
            okHttp: response.ok,
            data: { ok: false, status: "error", message: "Ungültige Server-Antwort (" + response.status + ")." }
          };
        });
      }).then(function (result) {
        var data = result.data || {};
        if (data.status === "error" || data.ok === false) {
          showToast("error", "✕ CLIP FEHLGESCHLAGEN", data.message || data.error || "Clip fehlgeschlagen.");
          return;
        }
        var jobId = data.job_id || data.id;
        if (!jobId) {
          showToast("error", "✕ CLIP FEHLGESCHLAGEN", data.message || "Clip fehlgeschlagen.");
          return;
        }
        showToast("pending", "● CLIP", data.message || "Clip wird erstellt...");
        pollJob(jobId);
      }).catch(function () {
        showToast("error", "✕ CLIP FEHLGESCHLAGEN", "Clip konnte nicht gestartet werden.");
      });
    }

    window.openClipOverlay = openClipOverlay;

    if (btnOpenEditor && clipOverlay && clipForm) {
      btnOpenEditor.addEventListener("click", function (event) {
        event.preventDefault();
        openClipOverlay();
      });
      document.querySelectorAll("[data-clip-close], #clipCloseButton, #clipCancelButton").forEach(function (control) {
        control.addEventListener("click", function () { closeClipOverlay(false); });
      });
      document.querySelectorAll("[data-clip-nudge]").forEach(function (control) {
        control.addEventListener("click", function () {
          var parts = String(control.dataset.clipNudge || "").split(":");
          var target = parts[0];
          var delta = Number.parseFloat(parts[1] || "0");
          if (target === "start") setClipRange(clipStart + delta, clipEnd, { dirty: true, seekPreview: true, movedHandle: "start" });
          else if (target === "end") setClipRange(clipStart, clipEnd + delta, { dirty: true, seekPreview: true, movedHandle: "end" });
          if (previewWantPlaying) playClipPreview();
        });
      });
      document.querySelectorAll("[data-clip-set]").forEach(function (control) {
        control.addEventListener("click", function () {
          var target = control.dataset.clipSet;
          var previewOffset = clipPreviewVideo && isNumber(clipPreviewVideo.currentTime)
            ? Math.max(0, clipPreviewVideo.currentTime - clipOrigin)
            : Math.max(0, (Number(vPlayer && vPlayer.currentTime) || 0) - clipOrigin);
          if (target === "start") setClipRange(previewOffset, clipEnd, { dirty: true, seekPreview: true, movedHandle: "start" });
          else if (target === "end") setClipRange(clipStart, previewOffset, { dirty: true, seekPreview: true, movedHandle: "end" });
          if (previewWantPlaying) playClipPreview();
        });
      });
      if (clipStartRange) {
        clipStartRange.addEventListener("input", function () {
          setClipRange(Number.parseFloat(clipStartRange.value), clipEnd, { dirty: true, seekPreview: true, movedHandle: "start" });
          if (previewWantPlaying) playClipPreview();
        });
      }
      if (clipEndRange) {
        clipEndRange.addEventListener("input", function () {
          setClipRange(clipStart, Number.parseFloat(clipEndRange.value), { dirty: true, seekPreview: true, movedHandle: "end" });
          if (previewWantPlaying) playClipPreview();
        });
      }
      if (clipTitleInput) {
        clipTitleInput.addEventListener("input", function () {
          clipDirty = true;
          updateClipUi({});
        });
      }
      if (clipPreviewToggle) {
        clipPreviewToggle.addEventListener("click", function () {
          if (clipPreviewVideo && !clipPreviewVideo.paused) pauseClipPreview();
          else playClipPreview();
        });
      }
      if (clipPreviewVideo) {
        clipPreviewVideo.addEventListener("timeupdate", function () {
          if (!previewWantPlaying || clipPreviewVideo.paused) return;
          var offset = Math.max(0, (Number(clipPreviewVideo.currentTime) || 0) - clipOrigin);
          if (offset >= clipEnd - 0.08) {
            applyPreviewSeek(clipStart, function () {
              if (previewWantPlaying) startPreviewPlayback();
            });
          }
        });
        clipPreviewVideo.addEventListener("play", function () { setClipPreviewIcon(true); });
        clipPreviewVideo.addEventListener("pause", function () {
          if (!previewWantPlaying) setClipPreviewIcon(false);
        });
      }
      document.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && overlayOpen()) closeClipOverlay(false);
      });
      clipForm.addEventListener("submit", function (event) {
        if (!window.fetch) return;
        event.preventDefault();
        clipSubmitting = true;
        updateClipUi({});
        setClipStatus("Clip wird erstellt...", "pending");
        if (clipResult) {
          clipResult.hidden = true;
          clipResult.replaceChildren();
        }
        var formData = new FormData(clipForm);
        formData.set("start", clipStart.toFixed(2));
        formData.set("duration", (clipEnd - clipStart).toFixed(2));

        function finishSuccess(data) {
          clipDirty = false;
          clipInitialStart = clipStart;
          clipInitialEnd = clipEnd;
          clipInitialTitle = ((clipTitleInput && clipTitleInput.value) || "").trim();
          setClipStatus(data.message || "Clip erstellt.", "success");
          if (clipResult) {
            var link = document.createElement("a");
            link.href = data.clip_url || "#";
            link.textContent = "Clip öffnen";
            clipResult.replaceChildren(document.createTextNode("Gespeichert. "), link);
            clipResult.hidden = false;
          }
        }

        function pollOverlayJob(jobId) {
          stopPolling();
          var started = Date.now();
          var tick = function () {
            fetch("/clip/job/" + encodeURIComponent(jobId), {
              headers: { Accept: "application/json", "X-Requested-With": "XMLHttpRequest" }
            }).then(function (response) {
              return response.json().then(function (data) {
                return { okHttp: response.ok, data: data };
              });
            }).then(function (result) {
              var data = result.data || {};
              var status = data.status || "";
              if (status === "done") {
                finishSuccess(data);
                clipSubmitting = false;
                updateClipUi({});
                return;
              }
              if (status === "error" || !result.okHttp) {
                setClipStatus(data.message || data.error || "Clip konnte nicht erstellt werden.", "error");
                clipSubmitting = false;
                updateClipUi({});
                return;
              }
              if (Date.now() - started > POLL_TIMEOUT_MS) {
                setClipStatus("Clip dauert zu lange.", "error");
                clipSubmitting = false;
                updateClipUi({});
                return;
              }
              setClipStatus(data.message || "Clip wird erstellt...", "pending");
              pollTimer = window.setTimeout(tick, POLL_MS);
            }).catch(function () {
              setClipStatus("Status konnte nicht geladen werden.", "error");
              clipSubmitting = false;
              updateClipUi({});
            });
          };
          tick();
        }

        fetch(clipForm.action, {
          method: "POST",
          body: formData,
          headers: {
            "X-Requested-With": "XMLHttpRequest",
            Accept: "application/json"
          }
        }).then(function (response) {
          return response.json().then(function (data) {
            if (!response.ok || data.ok === false || data.status === "error") {
              throw new Error(data.message || data.error || "Clip konnte nicht erstellt werden.");
            }
            return data;
          });
        }).then(function (data) {
          var jobId = data.job_id || data.id;
          if (jobId && data.status && data.status !== "done") {
            setClipStatus(data.message || "Clip wird erstellt...", "pending");
            pollOverlayJob(jobId);
            return;
          }
          finishSuccess(data);
          clipSubmitting = false;
          updateClipUi({});
        }).catch(function (error) {
          setClipStatus((error && error.message) || "Clip konnte nicht erstellt werden.", "error");
          clipSubmitting = false;
          updateClipUi({});
        });
      });
    }

    if (btnInstantClip) {
      btnInstantClip.addEventListener("click", function (event) {
        event.preventDefault();
        triggerInstantClip();
      });
    }

    document.addEventListener("keydown", function (event) {
      if (event.repeat) return;
      if (event.key !== settings.hotkey && event.code !== settings.hotkey) return;
      var target = event.target;
      var tag = (target && target.tagName) || "";
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (target && target.isContentEditable)) return;
      if (overlayOpen()) return;
      if (!btnInstantClip && !instantUrl) return;
      event.preventDefault();
      triggerInstantClip();
    });

    return {
      open: openClipOverlay,
      close: closeClipOverlay,
      instant: triggerInstantClip,
      isOpen: overlayOpen
    };
  }

  var clipApi = {
    init: initLiveClipOverlay,
    isOverlayOpen: function () {
      return document.body.classList.contains("clip-overlay-open");
    }
  };
  window.StreamVaultLiveClip = clipApi;
})();
