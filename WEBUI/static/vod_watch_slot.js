(function () {
  function post(url, fields, csrf, beacon) {
    const body = new FormData();
    body.append("csrf_token", csrf || "");
    Object.keys(fields || {}).forEach(function (key) {
      if (fields[key] != null) body.append(key, String(fields[key]));
    });
    if (beacon && navigator.sendBeacon) {
      return navigator.sendBeacon(url, body);
    }
    return fetch(url, {
      method: "POST",
      body: body,
      credentials: "same-origin",
      headers: { "X-CSRFToken": csrf || "", "X-Requested-With": "XMLHttpRequest" },
    }).then(function (response) {
      return response.json().then(function (data) {
        data._status = response.status;
        return data;
      }).catch(function () {
        return { ok: false, _status: response.status };
      });
    });
  }

  function attach(video, options) {
    const opts = options || {};
    if (!video || !opts.enabled || !opts.claim_url) return;
    const overlay = document.getElementById("vodSlotBusy");
    const messageEl = overlay ? overlay.querySelector("[data-slot-message]") : null;
    let holding = false;
    let claiming = false;
    let heartbeatTimer = null;
    const heartbeatMs = Math.max(5, Number(opts.heartbeat_sec) || 15) * 1000;

    function showBusy(payload) {
      holding = false;
      stopHeartbeat();
      if (messageEl) {
        messageEl.textContent = (payload && payload.message) || opts.message || "";
      }
      if (overlay) overlay.hidden = false;
      try {
        video.pause();
      } catch (_) {}
    }

    function hideBusy() {
      if (overlay) overlay.hidden = true;
    }

    function stopHeartbeat() {
      window.clearInterval(heartbeatTimer);
      heartbeatTimer = null;
    }

    function startHeartbeat() {
      window.clearInterval(heartbeatTimer);
      heartbeatTimer = window.setInterval(function () {
        post(opts.heartbeat_url, {}, opts.csrf_token).then(function (data) {
          if (data && data.ok === false) showBusy(data);
        }).catch(function () {});
      }, heartbeatMs);
    }

    function release(useBeacon) {
      stopHeartbeat();
      if (!holding) return;
      holding = false;
      post(opts.release_url, {}, opts.csrf_token, !!useBeacon);
    }

    function claimThen(playAfter) {
      if (holding || claiming) {
        if (holding && playAfter) playAfter();
        else if (claiming && playAfter) pendingPlayAfterClaim = true;
        return;
      }
      claiming = true;
      post(
        opts.claim_url,
        { streamer: opts.streamer || "", filename: opts.filename || "" },
        opts.csrf_token
      ).then(function (data) {
        claiming = false;
        if (!data || data.ok === false) {
          showBusy(data || {});
          pendingPlayAfterClaim = false;
          return;
        }
        holding = true;
        hideBusy();
        startHeartbeat();
        var shouldPlay = pendingPlayAfterClaim;
        pendingPlayAfterClaim = false;
        if (playAfter) playAfter();
        else if (shouldPlay && video.paused && !video.ended) {
          if (
            typeof video._nachtSiloPlaybackUserPaused === "function"
            && video._nachtSiloPlaybackUserPaused()
          ) {
            return;
          }
          if (
            typeof video._nachtSiloUserPaused === "function"
            && video._nachtSiloUserPaused()
          ) {
            return;
          }
          var playPromise = video.play();
          if (playPromise && typeof playPromise.catch === "function") {
            playPromise.catch(function () {});
          }
        }
      }).catch(function () {
        claiming = false;
      });
    }

    var pendingPlayAfterClaim = false;

    // Pre-claim so the first play() keeps the user-gesture / autoplay token.
    claimThen(null);

    video.addEventListener("play", function () {
      if (holding) {
        startHeartbeat();
        return;
      }
      // Do not pause+replay across an async claim — that drops autoplay
      // permission and leaves Drive VODs stuck at 0:00.
      pendingPlayAfterClaim = true;
      claimThen(function () {
        if (
          typeof video._nachtSiloPlaybackUserPaused === "function"
          && video._nachtSiloPlaybackUserPaused()
        ) {
          return;
        }
        if (
          typeof video._nachtSiloUserPaused === "function"
          && video._nachtSiloUserPaused()
        ) {
          return;
        }
        if (video.paused && !video.ended) {
          const playPromise = video.play();
          if (playPromise && typeof playPromise.catch === "function") {
            playPromise.catch(function () {});
          }
        }
      });
    });

    document.addEventListener("visibilitychange", function () {
      if (!holding) return;
      if (document.visibilityState === "hidden" || document.hidden) {
        // Background timers are throttled in minimized PWAs — keep the seat alive.
        post(opts.heartbeat_url, {}, opts.csrf_token, true);
        return;
      }
      post(opts.heartbeat_url, {}, opts.csrf_token).then(function (data) {
        if (data && data.ok === false) showBusy(data);
      }).catch(function () {});
    });

    video.addEventListener("ended", function () {
      release(false);
    });

    video.addEventListener("error", function () {
      if (holding) return;
      post(opts.claim_url, { streamer: opts.streamer || "", filename: opts.filename || "" }, opts.csrf_token)
        .then(function (data) {
          if (data && data.ok === false) showBusy(data);
        })
        .catch(function () {});
    });

    window.addEventListener("pagehide", function (event) {
      if (event && event.persisted) {
        if (holding) post(opts.heartbeat_url, {}, opts.csrf_token, true);
        return;
      }
      release(true);
    });
  }

  window.NachtSiloVodSlot = { attach: attach };
})();
