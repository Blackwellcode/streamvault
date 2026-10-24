(function () {
  function isFiniteNumber(value) {
    return typeof value === "number" && Number.isFinite(value);
  }

  function dbg(kind, detail) {
    try {
      if (window.StreamVaultVodDebug && typeof window.StreamVaultVodDebug.log === "function") {
        window.StreamVaultVodDebug.log("playback", kind, detail || {});
      }
    } catch (_) {}
  }

  function hasBufferedAhead(video, minAhead) {
    try {
      var ranges = video.buffered;
      var t = video.currentTime;
      if (!ranges || !ranges.length || !isFiniteNumber(t)) return false;
      for (var i = 0; i < ranges.length; i += 1) {
        if (ranges.start(i) <= t + 0.35 && ranges.end(i) > t + minAhead) {
          return true;
        }
      }
    } catch (_) {}
    return false;
  }

  function guard(video, options) {
    if (!video || video._streamVaultPlaybackGuardInstalled) return;
    video._streamVaultPlaybackGuardInstalled = true;
    const opts = options || {};
    const knownDuration = isFiniteNumber(Number(opts.knownDuration))
      ? Number(opts.knownDuration)
      : 0;
    const isDriveOnly = !!opts.driveOnly;
    const isLocalVod = !isDriveOnly && !opts.onDrive;
    const setCurrentTime = typeof opts.setCurrentTime === "function"
      ? opts.setCurrentTime
      : function (seconds) {
          if (!isFiniteNumber(seconds)) return;
          video.currentTime = Math.max(0, seconds);
        };
    const play = typeof opts.play === "function"
      ? opts.play
      : function () {
          internalActionUntil = Date.now() + 150;
          const promise = video.play();
          if (promise && typeof promise.catch === "function") {
            promise.catch(function () {});
          }
        };

    let lastGoodTime = 0;
    let lastProgressAt = Date.now();
    let prematureRecoveries = 0;
    let restoring = false;
    let wantPlaying = !video.paused;
    let userPaused = false;
    let gestureUntil = 0;
    let pendingUserPause = false;
    let scrubbing = false;
    let scrubClearTimer = null;
    let stallTimer = null;
    let stallAttempts = 0;
    let freezeTimer = null;
    let lastPlayKickAt = 0;
    let lastNudgeAt = 0;
    let internalActionUntil = 0;
    let seekSeq = 0;
    let ignoreSeekSeq = -1;
    let ignoreSeekUntil = 0;
    let lastUserSeekAt = 0;
    let lastUserSeekTarget = 0;
    let lastPlayheadBeforeSeek = 0;
    let seekGen = 0;
    let lastRefreshSeekGen = -1;
    let quotaHoldUntil = 0;

    function expectedDuration() {
      if (knownDuration > 1) return knownDuration;
      if (isFiniteNumber(video.duration) && video.duration > 1) return video.duration;
      return NaN;
    }

    function isPrematureEnd() {
      const now = isFiniteNumber(video.currentTime) && video.currentTime > 0
        ? video.currentTime
        : lastGoodTime;
      const expected = expectedDuration();
      return isFiniteNumber(expected) && now + 5 < expected;
    }

    function isIgnoredSeek() {
      return seekSeq === ignoreSeekSeq && Date.now() < ignoreSeekUntil;
    }

    function noteUserSeek(seconds) {
      seekGen += 1;
      lastRefreshSeekGen = -1;
      if (isFiniteNumber(video.currentTime) && video.currentTime > 0.5) {
        lastPlayheadBeforeSeek = video.currentTime;
      } else if (lastGoodTime > 2) {
        lastPlayheadBeforeSeek = lastGoodTime;
      }
      lastUserSeekAt = Date.now();
      if (isFiniteNumber(seconds) && seconds > 0) {
        lastUserSeekTarget = seconds;
        lastGoodTime = seconds;
      }
      pendingUserPause = false;
      userPaused = false;
      wantPlaying = true;
      clearMediaSessionPause();
      clearStallTimer();
      stallAttempts = 0;
      dbg("note-user-seek", {
        seconds: seconds,
        seekGen: seekGen,
        from: lastPlayheadBeforeSeek,
      });
    }

    function restorePlayheadIfReset() {
      if (restoring || video.ended || scrubbing || isIgnoredSeek()) return;
      if (!(isFiniteNumber(video.currentTime) && video.currentTime < 0.75)) return;

      // After a scrub the browser/Drive stack often snaps back to 0 — put the
      // playhead back on the user target instead of restarting the VOD.
      var target = 0;
      if (Date.now() - lastUserSeekAt < 20000 && lastUserSeekTarget > 2) {
        target = lastUserSeekTarget;
      } else if (lastGoodTime > 2) {
        target = lastGoodTime;
      }
      if (!(target > 2)) return;

      restoring = true;
      ignoreSeekUntil = Date.now() + 800;
      seekSeq += 1;
      ignoreSeekSeq = seekSeq;
      try {
        video.currentTime = target;
      } catch (_) {
        setCurrentTime(target);
      }
      window.setTimeout(function () { restoring = false; }, 300);
    }

    function recoverPrematureEnd() {
      if (prematureRecoveries >= 8) return false;
      if (!isPrematureEnd()) return false;
      prematureRecoveries += 1;
      const target = Math.max(0, lastGoodTime > 0.5 ? lastGoodTime - 0.15 : 0);
      programmaticSeek(target);
      forcePlay();
      return true;
    }

    function clearStallTimer() {
      if (stallTimer != null) {
        window.clearTimeout(stallTimer);
        stallTimer = null;
      }
    }

    function clearMediaSessionPause() {
      if (typeof video._streamVaultSetUserPaused === "function") {
        try { video._streamVaultSetUserPaused(false); } catch (_) {}
      }
    }

    function syncMediaSessionPaused(paused) {
      if (typeof video._streamVaultSetUserPaused === "function") {
        try { video._streamVaultSetUserPaused(!!paused); } catch (_) {}
      }
    }

    function acceptUserPause(reason) {
      userPaused = true;
      wantPlaying = false;
      pendingUserPause = false;
      scrubbing = false;
      if (scrubClearTimer != null) {
        window.clearTimeout(scrubClearTimer);
        scrubClearTimer = null;
      }
      clearStallTimer();
      stallAttempts = 0;
      syncMediaSessionPaused(true);
      dbg("user-pause", { reason: reason || "pause" });
    }

    function markGesture() {
      // Native controls often fire pause slightly after pointerdown.
      gestureUntil = Date.now() + 2500;
      // Always treat a control gesture while playing as a pause candidate —
      // even during a scrub window (otherwise Pause feels sticky/laggy).
      if (video.seeking || scrubbing || (Date.now() - lastUserSeekAt < 1200)) {
        pendingUserPause = false;
        return;
      }
      pendingUserPause = !video.paused;
    }

    function recentUserGesture() {
      return Date.now() <= gestureUntil;
    }

    function mediaSessionSaysPaused() {
      return typeof video._streamVaultUserPaused === "function"
        && video._streamVaultUserPaused();
    }

    function forcePlay() {
      if (userPaused || video.ended || !wantPlaying) return;
      if (Date.now() < quotaHoldUntil) {
        dbg("force-play-quota-hold", { until: quotaHoldUntil });
        return;
      }
      lastPlayKickAt = 0;
      play();
      lastPlayKickAt = Date.now();
    }

    function kickPlay() {
      if (userPaused || video.ended || !wantPlaying) return;
      if (Date.now() < quotaHoldUntil) return;
      var now = Date.now();
      if (now - lastPlayKickAt < 120) return;
      lastPlayKickAt = now;
      play();
    }

    function noteQuotaHold(seconds) {
      var holdMs = Math.max(60, Number(seconds) || 60) * 1000;
      quotaHoldUntil = Math.max(quotaHoldUntil, Date.now() + Math.min(holdMs, 3600 * 1000));
      wantPlaying = false;
      internalActionUntil = Date.now() + 150;
      try { video.pause(); } catch (_) {}
      dbg("quota-hold", { seconds: Math.round(holdMs / 1000) });
    }

    function beginScrubWindow(ms) {
      scrubbing = true;
      pendingUserPause = false;
      userPaused = false;
      wantPlaying = true;
      clearMediaSessionPause();
      lastProgressAt = Date.now();
      if (scrubClearTimer != null) window.clearTimeout(scrubClearTimer);
      scrubClearTimer = window.setTimeout(function () {
        scrubClearTimer = null;
        scrubbing = false;
      }, ms || 3500);
    }

    function programmaticSeek(seconds) {
      seekSeq += 1;
      ignoreSeekSeq = seekSeq;
      ignoreSeekUntil = Date.now() + 1000;
      beginScrubWindow(1800);
      setCurrentTime(seconds);
    }

    function noteScrub() {
      if (isIgnoredSeek()) return;
      seekSeq += 1;
      seekGen += 1;
      lastRefreshSeekGen = -1;
      lastUserSeekAt = Date.now();
      if (isFiniteNumber(video.currentTime) && video.currentTime > 0.5) {
        lastUserSeekTarget = video.currentTime;
      }
      // Cancel recoveries from the previous seek — they fight the new Range.
      clearStallTimer();
      stallAttempts = 0;
      beginScrubWindow(4500);
    }

    function finishScrub() {
      if (isIgnoredSeek()) {
        lastProgressAt = Date.now();
        dbg("finish-scrub-ignored", { seekSeq: seekSeq });
        return;
      }
      lastUserSeekAt = Date.now();
      if (isFiniteNumber(video.currentTime) && video.currentTime > 0.5) {
        lastUserSeekTarget = video.currentTime;
        lastGoodTime = video.currentTime;
      }
      clearStallTimer();
      stallAttempts = 0;
      beginScrubWindow(4500);
      dbg("finish-scrub", {
        t: video.currentTime,
        paused: !!video.paused,
        rs: video.readyState,
        seekGen: seekGen,
      });
      if (!video.ended) {
        // Always resume after a user seek — never drop this to coalesce.
        userPaused = false;
        pendingUserPause = false;
        wantPlaying = true;
        forcePlay();
        scheduleStallRecover(600, false);
      }
    }

function refreshDecodeAtPlayhead(forceReconnect) {
      var now = Date.now();
      if (!forceReconnect && now - lastNudgeAt < 2500) {
        dbg("refresh-decode-skip-throttled", { delta: now - lastNudgeAt });
        return;
      }
      lastNudgeAt = now;
      lastRefreshSeekGen = seekGen;

      var t = (isFiniteNumber(video.currentTime) && video.currentTime > 0.5)
        ? video.currentTime
        : lastGoodTime;
      if (!isFiniteNumber(t)) return;

      if (
        Date.now() - lastUserSeekAt < 20000
        && lastUserSeekTarget > 2
        && Math.abs(t - lastUserSeekTarget) > 1.5
      ) {
        t = lastUserSeekTarget;
      }

      // Blink / Chromium / WebKit WebMediaPlayer completely ignores currentTime assignment
      // if target == currentTime! We MUST nudge by -0.04s so the browser treats it as a seek,
      // terminates the dead/stalled HTTP connection, and issues a fresh Range request.
      var nudgeTarget = Math.max(0, t > 0.1 ? t - 0.04 : 0);

      dbg("refresh-decode-nudge", { t: t, nudgeTarget: nudgeTarget, seekGen: seekGen });
      programmaticSeek(nudgeTarget);
      forcePlay();
    }

    function recoverFromStall(forceNudge) {
      if (userPaused || video.ended || !wantPlaying) return;
      if (scrubbing || video.seeking || isIgnoredSeek()) {
        dbg("stall-kick-while-scrub", {
          scrubbing: !!scrubbing,
          seeking: !!video.seeking,
        });
        kickPlay();
        return;
      }
      if (
        typeof video._streamVaultUserPaused === "function"
        && video._streamVaultUserPaused()
      ) {
        userPaused = true;
        wantPlaying = false;
        dbg("stall-abort-user-paused", {});
        return;
      }

      var sinceSeek = Date.now() - lastUserSeekAt;
      if (sinceSeek < 2500 && !forceNudge) {
        dbg("stall-soft-play", { sinceSeek: sinceSeek });
        kickPlay();
        return;
      }

      stallAttempts += 1;
      if (stallAttempts > 25) return;

      var buffered = hasBufferedAhead(video, 0.75);
      dbg("stall-recover", {
        forceNudge: !!forceNudge,
        sinceSeek: sinceSeek,
        attempts: stallAttempts,
        buffered: !!buffered,
        paused: !!video.paused,
        rs: video.readyState,
      });

      if (video.paused || video.readyState < 3 || !buffered) {
        kickPlay();
      }

      var now = Date.now();
      // If buffer is empty, reconnect stream via nudge (auto-resumes across 2GB stream boundaries)
      if (
        !buffered
        && (forceNudge || stallAttempts >= 2 || now - lastNudgeAt > 2500)
      ) {
        refreshDecodeAtPlayhead(forceNudge);
      }
    }

    function scheduleStallRecover(delay, allowNudge) {
      if (userPaused || video.ended || !wantPlaying) return;
      clearStallTimer();
      stallTimer = window.setTimeout(function () {
        stallTimer = null;
        recoverFromStall(!!allowNudge || stallAttempts >= 2);
        if (!userPaused && wantPlaying && !video.ended) {
          if (video.paused) return;
          var needsHelp = video.readyState < 3
            || (scrubbing && Date.now() - lastProgressAt > 1200);
          if (needsHelp) {
            scheduleStallRecover(
              scrubbing ? 900 : (stallAttempts < 3 ? 500 : 1200),
              !scrubbing
            );
          }
        }
      }, delay);
    }

    function checkFrozenPlayhead() {
      if (userPaused || mediaSessionSaysPaused()) {
        wantPlaying = false;
        return;
      }
      if (video.ended || !wantPlaying || video.seeking) return;
      if (isIgnoredSeek()) return;
      if (video.paused) return;

      var now = Date.now();
      var stuckFor = now - lastProgressAt;
      var sinceSeek = now - lastUserSeekAt;

      // Settle period right after a user seek
      if (sinceSeek < 3500) {
        if (
          isDriveOnly
          && sinceSeek > 2500
          && !hasBufferedAhead(video, 0.35)
          && lastPlayheadBeforeSeek > 2
          && lastUserSeekTarget > 2
          && Math.abs((video.currentTime || 0) - lastUserSeekTarget) < 2
          && Math.abs(lastPlayheadBeforeSeek - lastUserSeekTarget) > 5
        ) {
          dbg("seek-quota-fallback", {
            from: video.currentTime,
            to: lastPlayheadBeforeSeek,
            target: lastUserSeekTarget,
          });
          try {
            if (window.StreamVaultVodDebug && window.StreamVaultVodDebug.showQuotaToast) {
              window.StreamVaultVodDebug.showQuotaToast(60);
            }
          } catch (_) {}
          noteQuotaHold(90);
          programmaticSeek(Math.max(0, lastPlayheadBeforeSeek));
          return;
        }
        return;
      }

      if (scrubbing && stuckFor < 2000) return;
      if (stuckFor < 2000) return;

      var hasBuffer = hasBufferedAhead(video, 0.5);

      // Playhead stuck and buffer empty (connection dropped at 2GB limit, network blip, or server yield):
      if (!hasBuffer) {
        if (stuckFor > 2400 && stuckFor < 12000) {
          // Reconnect stream immediately via nudge
          recoverFromStall(true);
          return;
        }
        // If stuck for over 12 seconds with repeated reconnects:
        if (stuckFor >= 12000) {
          if (isDriveOnly) {
            dbg("stall-drive-quota-timeout", { stuckFor: stuckFor, t: video.currentTime });
            try {
              if (window.StreamVaultVodDebug && window.StreamVaultVodDebug.showQuotaToast) {
                window.StreamVaultVodDebug.showQuotaToast(60);
              }
            } catch (_) {}
            noteQuotaHold(60);
          } else {
            // Local SSD / cached VOD: do NOT lock out for an hour. Keep reconnecting.
            dbg("stall-local-stream-retry", { stuckFor: stuckFor, attempts: stallAttempts });
            recoverFromStall(true);
          }
          return;
        }
      }

      // Playing UI but clock frozen despite having buffer (decode glitch):
      if (stuckFor > 2200) {
        recoverFromStall(stuckFor > 4500);
      }
    }

    var lastBlurAt = 0;
    window.addEventListener("blur", function () {
      lastBlurAt = Date.now();
      if (wantPlaying && !userPaused && !video.ended && video.paused) {
        play();
      }
    });
    document.addEventListener("visibilitychange", function () {
      if (document.hidden && wantPlaying && !userPaused && !video.ended && video.paused) {
        play();
      }
    });

    function isBackgrounded() {
      return !!(
        document.hidden
        || document.visibilityState === "hidden"
        || (typeof document.hasFocus === "function" && !document.hasFocus())
        || (Date.now() - lastBlurAt < 4000)
      );
    }

    video.addEventListener("pointerdown", markGesture, true);
    video.addEventListener("click", markGesture, true);
    video.addEventListener("keydown", function (event) {
      if (!event) return;
      var key = event.key || "";
      if (key === "k" || key === "K" || key === " " || key === "MediaPlayPause") {
        markGesture();
      }
    }, true);

    video.addEventListener("seeking", noteScrub);
    video.addEventListener("seeked", finishScrub);

    video.addEventListener("timeupdate", function () {
      if (restoring) return;
      if (isFiniteNumber(video.currentTime) && video.currentTime > 0.5) {
        if (Math.abs(video.currentTime - lastGoodTime) > 0.04) {
          lastProgressAt = Date.now();
          stallAttempts = 0;
          lastRefreshSeekGen = -1;
        }
        lastGoodTime = video.currentTime;
      }
    });
    video.addEventListener("playing", function () {
      if (isBackgrounded()) {
        userPaused = false;
        wantPlaying = true;
        clearMediaSessionPause();
      } else if ((userPaused || mediaSessionSaysPaused()) && !recentUserGesture()) {
        wantPlaying = false;
        userPaused = true;
        dbg("playing-ignored-user-pause", {});
        internalActionUntil = Date.now() + 150;
        try { video.pause(); } catch (_) {}
        return;
      }
      userPaused = false;
      prematureRecoveries = 0;
      stallAttempts = 0;
      clearStallTimer();
      lastProgressAt = Date.now();
      wantPlaying = true;
      pendingUserPause = false;
    });
    video.addEventListener("play", function (e) {
      if (pendingUserPause && !isBackgrounded()) {
        // A click/gesture was made to PAUSE, but play fired (likely a delayed promise or native clash).
        // Force it back to pause and ignore this play.
        try { video.pause(); } catch (_) {}
        dbg("play-blocked-pending-user-pause", {});
        return;
      }
      if (Date.now() > internalActionUntil || isBackgrounded()) {
        // Play triggered from outside our logic (e.g., native play button)
        userPaused = false;
        clearMediaSessionPause();
      }
      if (!isBackgrounded() && (userPaused || mediaSessionSaysPaused()) && !recentUserGesture()) {
        wantPlaying = false;
        userPaused = true;
        // One immediate re-pause only if something else forced play().
        internalActionUntil = Date.now() + 150;
        try { video.pause(); } catch (_) {}
        dbg("play-blocked-user-pause", {});
        return;
      }
      userPaused = false;
      wantPlaying = true;
      pendingUserPause = false;
      lastProgressAt = Date.now();
      restorePlayheadIfReset();
    });
    video.addEventListener("pause", function () {
      if (video.ended) {
        wantPlaying = false;
        clearStallTimer();
        return;
      }

      // If page is backgrounded or recently blurred, and no explicit user gesture:
      // this is an OS/Chromium background suspension, NOT a user pause!
      if (isBackgrounded() && !pendingUserPause) {
        dbg("background-pause-recovered", { blurDiff: Date.now() - lastBlurAt });
        userPaused = false;
        wantPlaying = true;
        clearMediaSessionPause();
        play();
        window.setTimeout(play, 50);
        window.setTimeout(play, 200);
        window.setTimeout(play, 600);
        return;
      }

      // If seeking or recently scrubbed, this is a seek-transient pause, NOT user pause!
      if (video.seeking || scrubbing || (Date.now() - lastUserSeekAt < 1500)) {
        pendingUserPause = false;
        userPaused = false;
        wantPlaying = true;
        kickPlay();
        return;
      }

      // User pause always wins — latch immediately, no delayed recover.
      if (
        ((pendingUserPause || recentUserGesture()) && !isBackgrounded())
        || mediaSessionSaysPaused()
        || userPaused
      ) {
        acceptUserPause(
          pendingUserPause || recentUserGesture()
            ? "gesture"
            : (mediaSessionSaysPaused() ? "mediasession" : "controls")
        );
        return;
      }
      // Scrub/seek path without a user gesture: browser sometimes pauses briefly on some platforms.
      // But if we forcefully resume here, we break the native Pause button (the "Double Pause" bug).
      // We will only schedule a very delayed recovery. If it was a real pause, the user won't notice.
      // If it was a browser glitch, it recovers eventually.
      if (video.seeking || isIgnoredSeek()) {
        pendingUserPause = false;
        wantPlaying = true;
        // Delayed kick so if it was a real user pause, they have time to trigger a second gesture,
        // or we just let it stay paused. Actually, if we just accept it as a control pause, it's safer.
        scheduleStallRecover(1500, false);
        return;
      }

      // If backgrounded at all, never accept "controls" pause
      if (isBackgrounded()) {
        dbg("background-controls-pause-ignored", {});
        userPaused = false;
        wantPlaying = true;
        clearMediaSessionPause();
        play();
        return;
      }

      acceptUserPause("controls");
    });
    video.addEventListener("waiting", function () {
      if (!userPaused && wantPlaying && !video.ended && !video.paused) {
        scheduleStallRecover(scrubbing ? 800 : 500, true);
      }
    });
    video.addEventListener("stalled", function () {
      if (!userPaused && wantPlaying && !video.ended && !video.paused) {
        scheduleStallRecover(scrubbing ? 1000 : 700, true);
      }
    });
    video.addEventListener("error", function () {
      if (!userPaused && wantPlaying && !video.ended && !video.paused) {
        scheduleStallRecover(150, true);
      }
    });
    video.addEventListener("ended", function (event) {
      clearStallTimer();
      if (recoverPrematureEnd()) {
        if (event && typeof event.stopImmediatePropagation === "function") {
          event.stopImmediatePropagation();
        }
      }
    }, true);

    freezeTimer = window.setInterval(checkFrozenPlayhead, 800);
    window.addEventListener("pagehide", function () {
      if (freezeTimer != null) {
        window.clearInterval(freezeTimer);
        freezeTimer = null;
      }
      if (scrubClearTimer != null) {
        window.clearTimeout(scrubClearTimer);
        scrubClearTimer = null;
      }
    });

    function requestPlay() {
      userPaused = false;
      pendingUserPause = false;
      wantPlaying = true;
      quotaHoldUntil = 0;
      clearMediaSessionPause();
      forcePlay();
    }

    video._streamVaultRequestPlay = requestPlay;
    video._streamVaultNoteUserSeek = noteUserSeek;
    video._streamVaultNoteQuotaHold = noteQuotaHold;
    video._streamVaultPlaybackUserPaused = function () {
      return !!userPaused || mediaSessionSaysPaused();
    };
    video._streamVaultPlaybackGuard = {
      lastGoodTime: function () { return lastGoodTime; },
      wantPlaying: function () { return wantPlaying; },
      userPaused: function () { return !!userPaused; },
      requestPlay: requestPlay,
      noteUserSeek: noteUserSeek,
      noteQuotaHold: noteQuotaHold,
    };
  }

  window.StreamVaultVodPlayback = {
    guard: guard,
  };
})();
