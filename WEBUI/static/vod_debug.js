(function () {
  var STORAGE_KEY = "nachtsilo_vod_debug";
  var UI_KEY = "nachtsilo_vod_debug_ui";
  var MAX_EVENTS = 500;
  var events = [];
  var seq = 0;
  var panel = null;
  var bodyEl = null;
  var flushTimer = null;
  var heartbeatTimer = null;
  var videoRef = null;
  var meta = {};
  var lastFlushLen = 0;
  var hookedFetch = false;

  function queryFlag(name) {
    try {
      var params = new URLSearchParams(window.location.search || "");
      var raw = (params.get(name) || "").toLowerCase();
      if (raw === "1" || raw === "true" || raw === "yes") return true;
      if (raw === "0" || raw === "false" || raw === "no") return false;
    } catch (_) {}
    return null;
  }

  function enabled() {
    // Background debug is on by default for VOD pages; ?vod_debug=0 disables.
    var q = queryFlag("vod_debug");
    if (q === false) {
      try { window.localStorage.setItem(STORAGE_KEY, "0"); } catch (_) {}
      return false;
    }
    if (q === true) {
      try { window.localStorage.setItem(STORAGE_KEY, "1"); } catch (_) {}
      return true;
    }
    try {
      if (window.localStorage.getItem(STORAGE_KEY) === "0") return false;
    } catch (_) {}
    if (window.NACHTSILO_VOD_DEBUG_DEFAULT === true) return true;
    try {
      return window.localStorage.getItem(STORAGE_KEY) === "1";
    } catch (_) {
      return false;
    }
  }

  function uiEnabled() {
    var q = queryFlag("vod_debug_ui");
    if (q === true) return true;
    if (q === false) return false;
    try {
      return window.localStorage.getItem(UI_KEY) === "1";
    } catch (_) {
      return false;
    }
  }

  function nowMs() {
    return Date.now();
  }

  function fmt(seconds) {
    if (!(typeof seconds === "number") || !Number.isFinite(seconds)) return "-";
    var t = Math.max(0, seconds);
    var h = Math.floor(t / 3600);
    var m = Math.floor((t % 3600) / 60);
    var s = Math.floor(t % 60);
    var core = h > 0
      ? h + ":" + String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0")
      : m + ":" + String(s).padStart(2, "0");
    return core;
  }

  function bufferedSummary(video) {
    try {
      var ranges = video.buffered;
      var parts = [];
      for (var i = 0; i < Math.min(ranges.length, 8); i += 1) {
        parts.push(fmt(ranges.start(i)) + "-" + fmt(ranges.end(i)));
      }
      if (ranges.length > 8) parts.push("+" + (ranges.length - 8));
      return parts.join(" | ") || "none";
    } catch (_) {
      return "err";
    }
  }

  function snapshot(video) {
    video = video || videoRef;
    if (!video) return {};
    var err = null;
    try {
      if (video.error) {
        err = { code: video.error.code, message: video.error.message || "" };
      }
    } catch (_) {}
    return {
      t: Number(video.currentTime) || 0,
      dur: Number(video.duration) || 0,
      paused: !!video.paused,
      seeking: !!video.seeking,
      ended: !!video.ended,
      rs: video.readyState,
      ns: video.networkState,
      muted: !!video.muted,
      buf: bufferedSummary(video),
      err: err,
    };
  }

  function push(source, kind, detail, opts) {
    if (!enabled()) return null;
    opts = opts || {};
    seq += 1;
    var row = {
      id: seq,
      ts: nowMs(),
      src: source || "app",
      kind: kind || "log",
      detail: detail || {},
      snap: snapshot(),
      meta: meta,
    };
    events.push(row);
    if (events.length > MAX_EVENTS) events.splice(0, events.length - MAX_EVENTS);
    // Background: only noisy console for hangs/errors unless UI is open.
    try {
      if (opts.console || kind === "error" || kind === "hang" || kind === "quota" || uiEnabled()) {
        var line = "[VODDBG #" + row.id + " " + row.src + "/" + row.kind + "] "
          + JSON.stringify({ detail: row.detail, snap: row.snap });
        if (kind === "error" || kind === "hang" || kind === "quota") console.warn(line);
        else console.log(line);
      }
    } catch (_) {}
    if (uiEnabled()) renderPanel();
    scheduleFlush();
    return row;
  }

  function scheduleFlush() {
    if (flushTimer != null) return;
    flushTimer = window.setTimeout(function () {
      flushTimer = null;
      flushToServer(false);
    }, 1500);
  }

  function flushToServer(force) {
    if (!enabled() || !events.length) return;
    if (!force && events.length === lastFlushLen) return;
    var csrf = "";
    try {
      csrf = window.__NACHTSILO_CSRF
        || (document.querySelector('input[name="csrf_token"]') || {}).value
        || "";
    } catch (_) {}
    var payload = {
      page: window.location.pathname + window.location.search,
      meta: meta,
      events: events.slice(-100),
    };
    var body = JSON.stringify(payload);
    lastFlushLen = events.length;
    try {
      if (force && navigator.sendBeacon) {
        var fd = new FormData();
        fd.append("csrf_token", csrf);
        fd.append("payload", body);
        navigator.sendBeacon("/api/vod/debug", fd);
        return;
      }
      fetch("/api/vod/debug", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": csrf,
          "X-Requested-With": "XMLHttpRequest",
          "X-Vod-Debug": "1",
        },
        body: body,
      }).catch(function () {});
    } catch (_) {}
  }

  function ensurePanel() {
    if (!uiEnabled() || panel || !enabled()) return;
    panel = document.createElement("div");
    panel.id = "vodDebugPanel";
    panel.innerHTML = ""
      + '<div class="vod-debug-head"><strong>VOD Debug</strong>'
      + '<span data-dbg-status></span>'
      + '<button type="button" data-dbg-copy>Copy</button>'
      + '<button type="button" data-dbg-hide>Hide</button></div>'
      + '<pre class="vod-debug-body" data-dbg-body></pre>';
    panel.style.cssText = "position:fixed;right:8px;bottom:8px;z-index:99999;width:min(520px,96vw);max-height:40vh;display:flex;flex-direction:column;background:rgba(8,10,14,.94);color:#e8eef7;border:1px solid rgba(255,255,255,.18);border-radius:10px;font:12px/1.35 ui-monospace,Consolas,monospace;box-shadow:0 12px 40px rgba(0,0,0,.45)";
    panel.querySelector(".vod-debug-head").style.cssText = "display:flex;gap:8px;align-items:center;padding:8px 10px;border-bottom:1px solid rgba(255,255,255,.12)";
    bodyEl = panel.querySelector("[data-dbg-body]");
    bodyEl.style.cssText = "margin:0;padding:8px 10px;overflow:auto;white-space:pre-wrap;word-break:break-word;flex:1";
    panel.querySelector("[data-dbg-copy]").onclick = function () {
      var text = JSON.stringify(events.slice(-150), null, 2);
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).catch(function () {});
      }
    };
    panel.querySelector("[data-dbg-hide]").onclick = function () {
      try { window.localStorage.setItem(UI_KEY, "0"); } catch (_) {}
      if (panel && panel.parentNode) panel.parentNode.removeChild(panel);
      panel = null;
      bodyEl = null;
    };
    document.documentElement.appendChild(panel);
  }

  function renderPanel() {
    if (!uiEnabled()) return;
    ensurePanel();
    if (!bodyEl) return;
    bodyEl.textContent = events.slice(-35).map(function (row) {
      return "#" + row.id + " " + row.src + "/" + row.kind + " t=" + fmt((row.snap || {}).t)
        + " " + JSON.stringify(row.detail || {});
    }).join("\n");
    bodyEl.scrollTop = bodyEl.scrollHeight;
  }

  function showQuotaToast(retryAfter) {
    var minutes = Math.max(15, Math.ceil((retryAfter || 3600) / 60));
    var existing = document.getElementById("vodQuotaToast");
    if (existing) {
      existing.textContent = "Drive-Quota aktiv — nicht erneut Play drücken (verlängert die Sperre). "
        + "Zurückspulen in den schon geladenen Teil, oder ca. "
        + minutes
        + " Min. warten.";
      return;
    }
    var el = document.createElement("div");
    el.id = "vodQuotaToast";
    el.textContent = "Drive-Quota aktiv — nicht erneut Play drücken (verlängert die Sperre). "
      + "Zurückspulen in den schon geladenen Teil, oder ca. "
      + minutes
      + " Min. warten.";
    el.style.cssText = "position:fixed;left:50%;bottom:72px;transform:translateX(-50%);z-index:99998;max-width:min(520px,92vw);padding:10px 14px;border-radius:10px;background:rgba(120,20,20,.94);color:#fff;font:13px/1.4 system-ui,sans-serif;box-shadow:0 10px 30px rgba(0,0,0,.4)";
    document.documentElement.appendChild(el);
    window.setTimeout(function () {
      if (el.parentNode) el.parentNode.removeChild(el);
    }, 20000);
  }

  function hookPageFetch() {
    if (hookedFetch || typeof window.fetch !== "function") return;
    hookedFetch = true;
    var nativeFetch = window.fetch.bind(window);
    window.fetch = function (input, init) {
      var url = "";
      try {
        url = typeof input === "string" ? input : (input && input.url) || "";
      } catch (_) {}
      var started = nowMs();
      return nativeFetch(input, init).then(function (response) {
        try {
          if (url.indexOf("/media/") !== -1 || url.indexOf("/api/vod/") !== -1 || url.indexOf("/chat") !== -1) {
            push("fetch", "response", {
              url: String(url).replace(/^https?:\/\/[^/]+/, "").slice(0, 160),
              status: response.status,
              ms: nowMs() - started,
              quota: response.headers.get("X-Vod-Quota") || "",
              source: response.headers.get("X-Vod-Source") || "",
            });
            if (response.status === 503 && (response.headers.get("X-Vod-Quota") === "1" || url.indexOf("/media/") !== -1)) {
              var retry = parseInt(response.headers.get("Retry-After") || "120", 10);
              push("fetch", "quota", { url: url.slice(0, 120), retry: retry });
              showQuotaToast(retry);
              try {
                var v = videoRef || document.getElementById("vodPlayer") || document.getElementById("player");
                if (v && typeof v._nachtSiloNoteQuotaHold === "function") {
                  v._nachtSiloNoteQuotaHold(retry);
                }
              } catch (_) {}
            }
          }
        } catch (_) {}
        return response;
      }, function (err) {
        push("fetch", "error", { url: String(url).slice(0, 120), message: String(err || "") });
        throw err;
      });
    };
  }

  function wrapFn(scopeName, obj, key) {
    if (!obj || typeof obj[key] !== "function" || obj[key]._nachtSiloDbgWrapped) return;
    var original = obj[key];
    function wrapped() {
      var args;
      try {
        args = Array.prototype.slice.call(arguments, 0, 3).map(function (v) {
          if (typeof v === "number" && Number.isFinite(v)) return Math.round(v * 1000) / 1000;
          if (typeof v === "string") return v.slice(0, 80);
          if (v && typeof v === "object") return Object.keys(v).slice(0, 6);
          return typeof v;
        });
      } catch (_) {
        args = [];
      }
      push(scopeName, "call:" + key, { args: args });
      return original.apply(this, arguments);
    }
    wrapped._nachtSiloDbgWrapped = true;
    obj[key] = wrapped;
  }

  function instrumentPage() {
    hookPageFetch();
    // Lightweight wraps for known page helpers once they exist.
    try {
      if (window.NachtSiloVodPlayback && window.NachtSiloVodPlayback.guard) {
        wrapFn("api", window.NachtSiloVodPlayback, "guard");
      }
      if (window.NachtSiloSeekHover && window.NachtSiloSeekHover.attach) {
        wrapFn("api", window.NachtSiloSeekHover, "attach");
      }
    } catch (_) {}
    [
      "playVodFromCurrentPosition",
      "setVodCurrentTime",
      "attemptVodAutoplay",
      "fetchChat",
      "renderTo",
      "shareItem",
    ].forEach(function (name) {
      try {
        if (typeof window[name] === "function") wrapFn("page", window, name);
      } catch (_) {}
    });
  }

  function attachVideo(video, options) {
    if (!enabled()) return;
    if (!video) return;
    videoRef = video;
    meta = Object.assign({}, meta, options || {});
    instrumentPage();
    push("debug", "attach", {
      src: (video.currentSrc || video.src || "").slice(-100),
      ui: uiEnabled(),
    });

    [
      "loadstart", "loadedmetadata", "loadeddata", "canplay", "canplaythrough",
      "play", "playing", "pause", "seeking", "seeked", "waiting", "stalled",
      "suspend", "abort", "error", "ended", "ratechange", "durationchange",
      "progress",
    ].forEach(function (name) {
      video.addEventListener(name, function () {
        var detail = { event: name };
        if (name === "error" && video.error) {
          detail.code = video.error.code;
          detail.message = video.error.message || "";
        }
        // progress is frequent — sample.
        if (name === "progress") {
          var last = video._nachtSiloDbgLastProg || 0;
          if (nowMs() - last < 2000) return;
          video._nachtSiloDbgLastProg = nowMs();
        }
        push("video", name, detail);
      });
    });

    video.addEventListener("timeupdate", function () {
      var last = video._nachtSiloDbgLastTu || 0;
      var t = nowMs();
      if (t - last < 2500 && !video.seeking) return;
      video._nachtSiloDbgLastTu = t;
      push("video", "timeupdate", {});
    });

    try {
      if (typeof PerformanceObserver === "function") {
        var obs = new PerformanceObserver(function (list) {
          list.getEntries().forEach(function (entry) {
            var name = String(entry.name || "");
            if (name.indexOf("/media/") === -1) return;
            push("net", "media", {
              url: name.replace(/^https?:\/\/[^/]+/, "").slice(0, 160),
              dur_ms: Math.round(entry.duration || 0),
              transfer: entry.transferSize || 0,
              encoded: entry.encodedBodySize || 0,
            });
          });
        });
        obs.observe({ type: "resource", buffered: true });
      }
    } catch (_) {}

    video.addEventListener("seeking", function () {
      video._nachtSiloDbgSeekStart = nowMs();
    });
    video.addEventListener("seeked", function () {
      video._nachtSiloDbgSeekStart = 0;
    });
    video.addEventListener("waiting", function () {
      if (!video._nachtSiloDbgWaitStart) video._nachtSiloDbgWaitStart = nowMs();
    });
    video.addEventListener("playing", function () {
      video._nachtSiloDbgWaitStart = 0;
    });

    if (heartbeatTimer) window.clearInterval(heartbeatTimer);
    heartbeatTimer = window.setInterval(function () {
      if (!enabled() || !videoRef) return;
      var v = videoRef;
      var stuckSeeking = v.seeking && (nowMs() - (v._nachtSiloDbgSeekStart || nowMs())) > 4000;
      var stuckWait = !v.paused && v.readyState < 3 && (nowMs() - (v._nachtSiloDbgWaitStart || 0)) > 4000;
      var frozen = !v.paused && !v.seeking && v.readyState >= 2
        && (nowMs() - (v._nachtSiloDbgLastTu || nowMs())) > 5000
        && (v._nachtSiloDbgLastTu || 0) > 0;
      if (stuckSeeking || stuckWait || frozen) {
        push("watchdog", "hang", {
          stuckSeeking: !!stuckSeeking,
          stuckWait: !!stuckWait,
          frozen: !!frozen,
        }, { console: true });
        // Native <video> Range 503s are invisible to fetch(); surface quota UI
        // when playback freezes without buffered media ahead.
        try {
          if (window.NachtSiloVodDebug && window.NachtSiloVodDebug.showQuotaToast) {
            window.NachtSiloVodDebug.showQuotaToast(180);
          }
        } catch (_) {}
      }
      // Heartbeat snapshot every 10s while playing.
      push("watchdog", "hb", { playing: !v.paused });
      flushToServer(false);
    }, 10000);

    window.addEventListener("pagehide", function () {
      flushToServer(true);
      if (heartbeatTimer) window.clearInterval(heartbeatTimer);
    });
  }

  function log(source, kind, detail) {
    return push(source, kind, detail || {});
  }

  window.NachtSiloVodDebug = {
    enabled: enabled,
    attach: attachVideo,
    log: log,
    snapshot: snapshot,
    events: function () { return events.slice(); },
    flush: function () { flushToServer(true); },
    showQuotaToast: showQuotaToast,
  };

  if (enabled()) {
    instrumentPage();
  }
})();
