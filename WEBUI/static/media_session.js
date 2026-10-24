(function () {
  function text(value, fallback) {
    const raw = value == null ? "" : String(value).trim();
    return raw || fallback || "";
  }

  function safePlay(video) {
    if (!video) return;
    // Keep audible so Chromium/Edge is less likely to freeze/suspend the PWA.
    if (video.muted) {
      video.muted = false;
      video.defaultMuted = false;
      video.removeAttribute("muted");
    }
    const promise = video.play();
    if (promise && typeof promise.catch === "function") {
      promise.catch(function () {});
    }
  }

  function seekBy(video, seconds) {
    if (!video || !Number.isFinite(video.duration)) return;
    const next = Math.max(0, Math.min(video.duration, video.currentTime + seconds));
    if (typeof video.fastSeek === "function") {
      try {
        video.fastSeek(next);
        return;
      } catch (_) {}
    }
    video.currentTime = next;
  }

  function setAction(action, handler) {
    try {
      navigator.mediaSession.setActionHandler(action, handler);
    } catch (_) {}
  }

  function updatePosition(video, isLive) {
    if (!navigator.mediaSession || typeof navigator.mediaSession.setPositionState !== "function") return;
    if (isLive || !Number.isFinite(video.duration) || video.duration <= 0) return;
    try {
      navigator.mediaSession.setPositionState({
        duration: video.duration,
        playbackRate: video.playbackRate || 1,
        position: Math.max(0, Math.min(video.currentTime || 0, video.duration)),
      });
    } catch (_) {}
  }

  var lastBlurTime = 0;
  window.addEventListener("blur", function () {
    lastBlurTime = Date.now();
  });

  function pageIsBackgrounded() {
    return !!(
      document.hidden
      || document.visibilityState === "hidden"
      || (typeof document.hasFocus === "function" && !document.hasFocus())
      || (Date.now() - lastBlurTime < 4000)
    );
  }

  // Nearly-silent looping tone so Edge/Chromium treats the PWA as "audible"
  // and is less aggressive about freezing timers / media when minimized.
  var keepAliveAudio = null;
  function ensureKeepAliveAudio() {
    if (keepAliveAudio) return keepAliveAudio;
    try {
      // 0.25s mono WAV, very quiet sine-ish noise via constant PCM not needed —
      // use a tiny silent-ish wav with non-zero samples so the tab stays audible.
      var wav =
        "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA=";
      // Prefer WebAudio silent oscillator (more reliable than empty wav).
      var Ctx = window.AudioContext || window.webkitAudioContext;
      if (Ctx) {
        var ctx = new Ctx();
        var osc = ctx.createOscillator();
        var gain = ctx.createGain();
        gain.gain.value = 0.00001;
        osc.frequency.value = 440;
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.start();
        keepAliveAudio = {
          ctx: ctx,
          stop: function () {
            try { osc.stop(); } catch (_) {}
            try { ctx.close(); } catch (_) {}
          },
          resume: function () {
            if (ctx.state === "suspended") {
              ctx.resume().catch(function () {});
            }
          },
        };
        return keepAliveAudio;
      }
      var audio = new Audio(wav);
      audio.loop = true;
      audio.volume = 0.01;
      keepAliveAudio = {
        stop: function () {
          try { audio.pause(); } catch (_) {}
        },
        resume: function () {
          var p = audio.play();
          if (p && p.catch) p.catch(function () {});
        },
      };
      return keepAliveAudio;
    } catch (_) {
      return null;
    }
  }

  /**
   * Keep playback running when the Windows PWA is minimized or covered.
   * Only an intentional user pause (controls / keys / focused Media Session)
   * should stick — occlusion often leaves document.visibilityState=visible.
   */
  function keepPlaying(video) {
    if (!video || video._streamVaultKeepPlaying) return;
    video._streamVaultKeepPlaying = true;

    var wantPlaying = !video.paused;
    var intentionalPause = false;
    var gestureUntil = 0;
    var scrubUntil = 0;
    var watchdog = null;

    function markGesture() {
      gestureUntil = Date.now() + 2500;
    }

    function markScrub() {
      scrubUntil = Date.now() + 500;
      intentionalPause = false;
      wantPlaying = true;
    }

    function noteIntentionalPause() {
      intentionalPause = true;
      wantPlaying = false;
      stopWatchdog();
      var alive = keepAliveAudio;
      if (alive && alive.stop) {
        // leave audio context; just don't force resume
      }
    }

    function noteWantPlaying() {
      intentionalPause = false;
      wantPlaying = true;
      var alive = ensureKeepAliveAudio();
      if (alive && alive.resume) alive.resume();
      startWatchdog();
    }

    function resumeIfNeeded() {
      if (!wantPlaying || intentionalPause || video.ended) return;
      if (!video.paused) {
        var alive = ensureKeepAliveAudio();
        if (alive && alive.resume) alive.resume();
        return;
      }
      safePlay(video);
    }

    function startWatchdog() {
      if (watchdog) return;
      watchdog = window.setInterval(function () {
        if (!wantPlaying || intentionalPause || video.ended) {
          stopWatchdog();
          return;
        }
        if (video.paused && !video.ended) {
          resumeIfNeeded();
        }
        try {
          if (navigator.mediaSession) {
            navigator.mediaSession.playbackState = "playing";
          }
        } catch (_) {}
      }, 700);
    }

    function stopWatchdog() {
      if (!watchdog) return;
      window.clearInterval(watchdog);
      watchdog = null;
    }

    function maybeUserPausedAfterGesture() {
      if (!video.paused) return;
      if (Date.now() > gestureUntil) return;
      if (video.seeking || Date.now() <= scrubUntil) return;
      // User interacted recently and now paused while page could receive input.
      if (!pageIsBackgrounded()) {
        noteIntentionalPause();
      }
    }

    video.addEventListener("play", function () {
      // Do not clear intentionalPause on spurious play() — only Media Session
      // play action / _streamVaultSetUserPaused(false) may resume.
      if (intentionalPause) return;
      noteWantPlaying();
    });
    video.addEventListener("playing", function () {
      if (intentionalPause) return;
      noteWantPlaying();
    });
    video.addEventListener("seeking", markScrub);
    video.addEventListener("seeked", function () {
      markScrub();
      // Foreground seek resume is owned by StreamVaultVodPlayback — only help
      // when the PWA/tab is backgrounded and Chromium paused media.
      if (wantPlaying && !intentionalPause && !video.ended && pageIsBackgrounded()) {
        safePlay(video);
      }
    });

    video.addEventListener("pause", function () {
      if (intentionalPause || !wantPlaying || video.ended) return;

      // If page is backgrounded or recently blurred, this is an OS/Chromium suspension, NOT a user pause!
      if (pageIsBackgrounded()) {
        resumeIfNeeded();
        window.setTimeout(resumeIfNeeded, 50);
        window.setTimeout(resumeIfNeeded, 200);
        window.setTimeout(resumeIfNeeded, 600);
        return;
      }

      // Recent click/key while focused → real pause (before scrub handling).
      if (Date.now() <= gestureUntil && !pageIsBackgrounded()) {
        noteIntentionalPause();
        return;
      }
      if (video.seeking || Date.now() <= scrubUntil) {
        wantPlaying = true;
        intentionalPause = false;
        // Background only — foreground scrub resume is handled by vod_playback.
        if (pageIsBackgrounded()) {
          window.setTimeout(resumeIfNeeded, 40);
        }
        return;
      }

      // Foreground pause without a gesture (e.g. iOS native controls or VOD slot claim).
      // We assume pauses that occur while the page is fully in the foreground are intentional.
      if (!pageIsBackgrounded()) {
        noteIntentionalPause();
        return;
      }

      // System / occlusion / minimize pause → kick playback immediately.
      resumeIfNeeded();
      window.setTimeout(resumeIfNeeded, 50);
      window.setTimeout(resumeIfNeeded, 250);
      window.setTimeout(resumeIfNeeded, 800);
    });

    ["pointerdown", "touchstart"].forEach(function (evt) {
      video.addEventListener(evt, markGesture, true);
    });
    document.addEventListener(
      "keydown",
      function (event) {
        var key = event.key || "";
        if (key !== " " && key !== "k" && key !== "K" && event.code !== "Space" && key !== "MediaPlayPause") {
          return;
        }
        var tag = (event.target && event.target.tagName) || "";
        if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (event.target && event.target.isContentEditable)) {
          return;
        }
        markGesture();
        window.setTimeout(maybeUserPausedAfterGesture, 30);
      },
      true
    );
    video.addEventListener("click", function () {
      markGesture();
      window.setTimeout(maybeUserPausedAfterGesture, 30);
    });

    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "hidden" || document.hidden) {
        resumeIfNeeded();
        startWatchdog();
      }
    });
    window.addEventListener("blur", function () {
      lastBlurTime = Date.now();
      // Covered by another window or switched away: often blur without visibilitychange.
      resumeIfNeeded();
      startWatchdog();
    });
    window.addEventListener("focus", function () {
      resumeIfNeeded();
    });
    window.addEventListener("freeze", resumeIfNeeded);
    window.addEventListener("pageshow", function (event) {
      if (event && event.persisted) resumeIfNeeded();
    });

    if (wantPlaying) {
      noteWantPlaying();
    }

    video._streamVaultUserPaused = function () {
      return intentionalPause;
    };
    video._streamVaultSetUserPaused = function (value) {
      if (value) noteIntentionalPause();
      else noteWantPlaying();
    };
  }

  function attach(video, options) {
    if (!video) return;
    keepPlaying(video);
    if (!("mediaSession" in navigator)) return;
    const data = video.dataset || {};
    const isLive = data.mediaLive === "true" || (options && options.live === true);
    const artwork = text(data.mediaArtwork || (options && options.artwork), "/app-icon-512.png");
    const title = text(data.mediaTitle || (options && options.title), document.title || "StreamVault");
    const artist = text(data.mediaArtist || (options && options.artist), "StreamVault");
    const album = text(data.mediaAlbum || (options && options.album), isLive ? "Live" : "Archiv");

    try {
      navigator.mediaSession.metadata = new MediaMetadata({
        title: title,
        artist: artist,
        album: album,
        artwork: [
          { src: artwork, sizes: "512x512", type: "image/png" },
          { src: "/app-icon-192.png", sizes: "192x192", type: "image/png" },
        ],
      });
    } catch (_) {}

    setAction("play", function () {
      if (typeof video._streamVaultSetUserPaused === "function") {
        video._streamVaultSetUserPaused(false);
      }
      safePlay(video);
    });
    setAction("pause", function () {
      if (typeof video._streamVaultSetUserPaused === "function") {
        video._streamVaultSetUserPaused(true);
      }
      video.pause();
    });
    setAction("stop", function () {
      if (typeof video._streamVaultSetUserPaused === "function") {
        video._streamVaultSetUserPaused(true);
      }
      video.pause();
    });
    setAction("seekbackward", function (event) {
      seekBy(video, -(event && event.seekOffset ? event.seekOffset : 15));
    });
    setAction("seekforward", function (event) {
      seekBy(video, event && event.seekOffset ? event.seekOffset : 30);
    });
    setAction("seekto", function (event) {
      if (!event || !Number.isFinite(event.seekTime) || isLive) return;
      if (event.fastSeek && typeof video.fastSeek === "function") {
        try {
          video.fastSeek(event.seekTime);
          return;
        } catch (_) {}
      }
      video.currentTime = event.seekTime;
    });

    function updateState() {
      var forcePlaying =
        typeof video._streamVaultUserPaused === "function"
          ? !video._streamVaultUserPaused() && !video.ended
          : !video.paused;
      try {
        navigator.mediaSession.playbackState =
          forcePlaying || !video.paused ? "playing" : "paused";
      } catch (_) {}
      updatePosition(video, isLive);
    }

    video.addEventListener("play", function () {
      if (video.muted) {
        video.muted = false;
        video.defaultMuted = false;
        video.removeAttribute("muted");
      }
      updateState();
    });
    video.addEventListener("pause", updateState);
    video.addEventListener("ended", updateState);
    video.addEventListener("durationchange", updateState);
    video.addEventListener("ratechange", updateState);
    video.addEventListener("timeupdate", function () { updatePosition(video, isLive); });
    updateState();
  }

  window.StreamVaultMediaSession = {
    attach: attach,
    keepPlaying: keepPlaying,
    safePlay: safePlay,
  };
})();
