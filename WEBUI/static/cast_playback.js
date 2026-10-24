(function (global) {
  "use strict";

  const RECEIVER_APP_ID = "CC1AD845";
  let castReady = false;
  let castUnavailable = false;
  let senderScriptRequested = false;

  function isMobileCastSurface() {
    const body = document.body;
    if (!body || !body.classList) return false;
    // Only dedicated mobile player / mobile watch templates.
    return (
      body.classList.contains("page-mobile-player") ||
      body.classList.contains("page-twitch-watch-mobile")
    );
  }

  function canNativeHls(video) {
    if (!video || typeof video.canPlayType !== "function") return false;
    return !!(
      video.canPlayType("application/vnd.apple.mpegurl") ||
      video.canPlayType("application/x-mpegURL")
    );
  }

  function preferNativeHls(video) {
    if (!canNativeHls(video)) return false;
    // Only genuine Apple devices have reliable native HLS decoding in <video src>.
    // Android, Tizen, webOS, and Chromium-based TVs/browsers MUST use MSE + hls.js.
    return isAppleDevice();
  }

  function isAppleDevice() {
    const ua = navigator.userAgent || "";
    return /iPad|iPhone|iPod|Macintosh/i.test(ua);
  }

  function enableAirPlay(video) {
    if (!video) return;
    // Desktop Chromium/Edge shows a translucent cast/remote toolbar (empty tiles)
    // when remote playback is enabled. Keep AirPlay only where it is useful.
    if (!isMobileCastSurface() && !isAppleDevice()) {
      try {
        video.disableRemotePlayback = true;
      } catch (_) {}
      video.setAttribute("disableremoteplayback", "");
      video.removeAttribute("x-webkit-airplay");
      video.removeAttribute("airplay");
      const list = String(video.getAttribute("controlsList") || "");
      if (!/\bnoremoteplayback\b/i.test(list)) {
        video.setAttribute(
          "controlsList",
          (list + " noremoteplayback").trim().replace(/\s+/g, " ")
        );
      }
      return;
    }
    video.setAttribute("x-webkit-airplay", "allow");
    video.setAttribute("airplay", "allow");
    try {
      video.disableRemotePlayback = false;
    } catch (_) {}
    video.removeAttribute("disableremoteplayback");
  }

  function absoluteUrl(url) {
    if (!url) return "";
    try {
      return new URL(url, window.location.href).href;
    } catch (_) {
      return url;
    }
  }

  function ensureSenderScript() {
    if (!isMobileCastSurface()) return;
    if (senderScriptRequested) return;
    senderScriptRequested = true;
    if (global.cast && global.cast.framework) return;
    const script = document.createElement("script");
    script.src = "https://www.gstatic.com/cv/js/sender/v1/cast_sender.js?loadCastFramework=1";
    script.async = true;
    (document.head || document.documentElement).appendChild(script);
    window.setTimeout(function () {
      if (!castReady) castUnavailable = true;
    }, 8000);
  }

  function initCastContext() {
    if (!isMobileCastSurface()) return false;
    if (castReady) return true;
    if (!global.cast || !cast.framework || !global.chrome || !chrome.cast) return false;
    try {
      const ctx = cast.framework.CastContext.getInstance();
      ctx.setOptions({
        receiverApplicationId: chrome.cast.media.DEFAULT_MEDIA_RECEIVER_APP_ID || RECEIVER_APP_ID,
        autoJoinPolicy: chrome.cast.AutoJoinPolicy.ORIGIN_SCOPED
      });
      castReady = true;
      document.querySelectorAll(".ns-cast-btn, .ns-cast-trigger").forEach(function (btn) {
        btn.hidden = false;
        btn.removeAttribute("hidden");
      });
      return true;
    } catch (_) {
      return false;
    }
  }

  const previousCastHook = global.__onGCastApiAvailable;
  global.__onGCastApiAvailable = function (available) {
    if (typeof previousCastHook === "function") {
      try { previousCastHook(available); } catch (_) {}
    }
    if (available) initCastContext();
    else castUnavailable = true;
  };

  function playerVideo() {
    return document.getElementById("proxyPlayer")
      || document.getElementById("player")
      || document.getElementById("vodPlayer")
      || document.querySelector("video");
  }

  function labelNode(btn) {
    return btn ? btn.querySelector(".btn-label") : null;
  }

  function readLabel(btn) {
    const node = labelNode(btn);
    return ((node ? node.textContent : btn.textContent) || "").trim();
  }

  function writeLabel(btn, text) {
    const node = labelNode(btn);
    if (node) node.textContent = text;
    else btn.textContent = text;
  }

  function restoreLabel(btn) {
    writeLabel(btn, btn.dataset.nsCastLabel || "An TV");
  }

  function isCastCancel(err) {
    if (!err) return false;
    const code = err.code || err.description || err.message || err;
    const text = String(code).toLowerCase();
    return text === "cancel" || text.indexOf("cancel") !== -1;
  }

  function beginCastSession() {
    // requestSession() must run in the tap, before any await, or Chrome drops it.
    if (!initCastContext()) return null;
    try {
      const ctx = cast.framework.CastContext.getInstance();
      const current = ctx.getCurrentSession();
      if (current) return Promise.resolve(current);
      const pending = ctx.requestSession();
      if (!pending || typeof pending.then !== "function") return null;
      return pending.then(function () {
        return cast.framework.CastContext.getInstance().getCurrentSession();
      });
    } catch (_) {
      return null;
    }
  }

  function showAirPlayPicker(video, url) {
    if (!video || typeof video.webkitShowPlaybackTargetPicker !== "function") return false;
    enableAirPlay(video);
    try {
      video.muted = false;
      video.defaultMuted = false;
      video.removeAttribute("muted");
    } catch (_) {}
    if (url && !video.currentSrc && !video.src) {
      video.src = absoluteUrl(url);
    }
    try {
      video.webkitShowPlaybackTargetPicker();
      return true;
    } catch (_) {
      return false;
    }
  }

  function promptRemotePlayback(url) {
    if (!url || !global.HTMLVideoElement) return false;
    const video = document.createElement("video");
    video.setAttribute("playsinline", "");
    video.preload = "none";
    video.src = absoluteUrl(url);
    try { video.disableRemotePlayback = false; } catch (_) {}
    video.style.cssText = "position:fixed;width:1px;height:1px;opacity:0;pointer-events:none";
    document.body.appendChild(video);
    if (!video.remote || typeof video.remote.prompt !== "function") {
      video.remove();
      return false;
    }
    const pending = video.remote.prompt();
    if (pending && typeof pending.then === "function") {
      pending.catch(function () {}).then(function () {
        window.setTimeout(function () { video.remove(); }, 60000);
      });
    }
    return true;
  }

  async function resolveMediaUrl(options) {
    options = options || {};
    const fallback = options.url
      ? {
          url: options.url,
          contentType: options.contentType || "",
          live: !!options.live
        }
      : null;
    if (!options.ensureUrl) {
      if (!fallback) throw new Error("no-url");
      return fallback;
    }
    const video = playerVideo();
    if (fallback && video && video.readyState >= 2 && (video.currentSrc || video.src)) {
      return fallback;
    }
    if (typeof options.onWait === "function") {
      try { options.onWait(); } catch (_) {}
    }
    const deadline = Date.now() + (options.live === false ? 4000 : 20000);
    let lastError = "not-ready";
    while (Date.now() < deadline) {
      try {
        const res = await fetch(options.ensureUrl, {
          credentials: "same-origin",
          headers: { Accept: "application/json" },
          cache: "no-store"
        });
        if (!res.ok) {
          lastError = "cast-url";
        } else {
          const data = await res.json();
          if (data && data.url && data.ready) {
            return {
              url: data.url,
              contentType: data.contentType || options.contentType || "application/vnd.apple.mpegurl",
              live: data.live !== false
            };
          }
          lastError = "not-ready";
        }
      } catch (_) {
        lastError = "cast-url";
      }
      await new Promise(function (resolve) { setTimeout(resolve, 1000); });
    }
    if (fallback) return fallback;
    throw new Error(lastError);
  }

  function mediaTitle() {
    const el = document.getElementById("watchDisplayName")
      || document.querySelector(".vod-display-title");
    const text = el && el.textContent ? el.textContent.trim() : "";
    return text || "NachtSilo Tube";
  }

  async function loadResolvedOnSession(session, resolved) {
    if (!session) throw new Error("no-session");
    const live = !!resolved.live;
    const contentType = resolved.contentType || (live
      ? "application/vnd.apple.mpegurl"
      : "video/mp4");
    const mediaInfo = new chrome.cast.media.MediaInfo(absoluteUrl(resolved.url), contentType);
    mediaInfo.streamType = live
      ? chrome.cast.media.StreamType.LIVE
      : chrome.cast.media.StreamType.BUFFERED;
    const isHls = contentType.indexOf("mpegurl") !== -1 || contentType.indexOf("mpegURL") !== -1;
    if (isHls && chrome.cast.media.HlsSegmentFormat) {
      mediaInfo.hlsSegmentFormat = chrome.cast.media.HlsSegmentFormat.TS;
    }
    if (isHls && chrome.cast.media.HlsVideoSegmentFormat) {
      mediaInfo.hlsVideoSegmentFormat = chrome.cast.media.HlsVideoSegmentFormat.MPEG2_TS;
    }
    if (chrome.cast.media.GenericMediaMetadata) {
      const meta = new chrome.cast.media.GenericMediaMetadata();
      meta.title = mediaTitle();
      meta.subtitle = live ? "Live" : "NachtSilo Tube";
      mediaInfo.metadata = meta;
    }
    const request = new chrome.cast.media.LoadRequest(mediaInfo);
    request.autoplay = true;
    await session.loadMedia(request);
  }

  async function loadOnCast(options, sessionPromise) {
    let session = null;
    if (sessionPromise) {
      session = await sessionPromise;
    } else {
      if (!initCastContext()) throw new Error("no-cast");
      const ctx = cast.framework.CastContext.getInstance();
      session = ctx.getCurrentSession();
      if (!session) {
        await ctx.requestSession();
        session = ctx.getCurrentSession();
      }
    }
    if (!session) throw new Error("no-session");
    const resolved = await resolveMediaUrl(options);
    await loadResolvedOnSession(session, resolved);
  }

  function bindClick(btn, options) {
    if (!btn || btn.dataset.nsCastBound === "1") return;
    btn.dataset.nsCastBound = "1";
    if (!btn.dataset.nsCastLabel) btn.dataset.nsCastLabel = readLabel(btn) || "An TV";
    btn.addEventListener("click", function (event) {
      if (event && typeof event.preventDefault === "function") event.preventDefault();
      options = options || {};
      const video = playerVideo();
      const sessionPromise = beginCastSession();
      if (!sessionPromise && video && showAirPlayPicker(video, options.url || "")) {
        writeLabel(btn, "TV wählen");
        window.setTimeout(function () { restoreLabel(btn); }, 2500);
        return;
      }
      if (!sessionPromise && !castUnavailable && senderScriptRequested && !isAppleDevice()) {
        writeLabel(btn, "TV lädt…");
        window.setTimeout(function () { restoreLabel(btn); }, 2000);
        return;
      }
      if (!sessionPromise && options.url && promptRemotePlayback(options.url)) {
        writeLabel(btn, "TV wählen");
        window.setTimeout(function () { restoreLabel(btn); }, 2500);
        return;
      }
      if (!sessionPromise) {
        writeLabel(btn, "TV nicht bereit");
        window.setTimeout(function () { restoreLabel(btn); }, 2500);
        return;
      }
      btn.disabled = true;
      writeLabel(btn, "Verbinde…");
      const castOptions = Object.assign({}, options, {
        onWait: function () { writeLabel(btn, "Startet…"); }
      });
      Promise.resolve(loadOnCast(castOptions, sessionPromise)).then(function () {
        writeLabel(btn, "Läuft auf TV");
        window.setTimeout(function () { restoreLabel(btn); }, 4000);
      }).catch(function (err) {
        if (isCastCancel(err)) {
          restoreLabel(btn);
          return;
        }
        const code = err && (err.message || err.code) ? String(err.message || err.code) : "";
        writeLabel(btn, code === "not-ready" ? "Stream startet…" : "TV nicht bereit");
        window.setTimeout(function () { restoreLabel(btn); }, 2500);
      }).then(function () {
        btn.disabled = false;
      });
    });
  }

  function mountButton(host, options) {
    if (!isMobileCastSurface()) return null;
    if (!host) return null;
    options = options || {};
    if (host.matches && host.matches(".ns-cast-trigger, .ns-cast-btn")) {
      bindClick(host, options);
      ensureSenderScript();
      if (initCastContext()) {
        host.hidden = false;
        host.removeAttribute("hidden");
      }
      return host;
    }
    const wrap = host.closest(
      ".player-wrapper, .video-container, .twitch-player-frame, .twitch-mobile-player-frame, .player-container, .player"
    ) || host.parentElement || host;
    if (wrap && wrap.nodeType === 1) {
      const pos = window.getComputedStyle(wrap).position;
      if (pos === "static") wrap.style.position = "relative";
    }
    let btn = wrap.querySelector(".ns-cast-btn");
    if (!btn) {
      btn = document.createElement("button");
      btn.type = "button";
      btn.className = "ns-cast-btn";
      btn.textContent = "An TV senden";
      btn.hidden = true;
      btn.setAttribute("aria-label", "An Fernseher senden");
      wrap.appendChild(btn);
    }
    bindClick(btn, options);
    ensureSenderScript();
    if (initCastContext()) {
      btn.hidden = false;
      btn.removeAttribute("hidden");
    }
    return btn;
  }

  function bindVideo(video, options) {
    options = options || {};
    if (video) {
      // AirPlay remains available via native video controls on supported devices.
      enableAirPlay(video);
      if (!options.url) {
        options.url = video.currentSrc || video.getAttribute("src") || "";
      }
      mountButton(video, options);
    } else if (options.host) {
      mountButton(options.host, options);
    }
  }

  function startHls(video, url, options) {
    options = options || {};
    bindVideo(video, Object.assign({
      url: url,
      live: options.live !== false,
      contentType: options.contentType || "application/vnd.apple.mpegurl"
    }, options));
    if (preferNativeHls(video)) {
      video.src = url;
      if (typeof options.onNative === "function") options.onNative();
      return { mode: "native", hls: null };
    }
    if (global.Hls && Hls.isSupported()) {
      const hls = new Hls(options.hlsConfig || {});
      hls.loadSource(url);
      hls.attachMedia(video);
      return { mode: "mse", hls: hls };
    }
    if (canNativeHls(video)) {
      video.src = url;
      return { mode: "native-fallback", hls: null };
    }
    video.src = url;
    return { mode: "src", hls: null };
  }

  global.NachtSiloCast = {
    canNativeHls: canNativeHls,
    preferNativeHls: preferNativeHls,
    enableAirPlay: enableAirPlay,
    isMobileCastSurface: isMobileCastSurface,
    bindVideo: bindVideo,
    startHls: startHls,
    loadOnCast: loadOnCast,
    mountButton: mountButton
  };

  document.addEventListener("DOMContentLoaded", function () {
    if (!isMobileCastSurface()) return;
    document.querySelectorAll("[data-ns-cast-ensure], [data-ns-cast-url], .ns-cast-trigger").forEach(function (el) {
      mountButton(el, {
        url: el.getAttribute("data-ns-cast-url") || "",
        ensureUrl: el.getAttribute("data-ns-cast-ensure") || "",
        live: el.getAttribute("data-ns-cast-live") !== "0",
        contentType: el.getAttribute("data-ns-cast-type") || ""
      });
    });
  });
})(window);
