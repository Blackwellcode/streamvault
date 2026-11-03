(function (global) {
  "use strict";
  var svT = window.svT || function (s) { return s; };

  const body = document.body;
  if (!body || !body.classList.contains("page-mobile-player")) return;

  const chatBtn = document.getElementById("landscapeChatToggle");
  const fsBtn = document.getElementById("forceLandscapeToggle");
  const KEY = "streamvault_landscape_chat_open_v2";
  const landscapeMq = window.matchMedia ? window.matchMedia("(orientation: landscape)") : null;

  let chatOpen = false;
  let hadPageFullscreen = false;

  function naturalLandscape() {
    try {
      if (window.innerWidth > window.innerHeight) return true;
      if (landscapeMq && landscapeMq.matches) return true;
    } catch (_) {}
    return window.innerWidth > window.innerHeight;
  }

  function forcedLandscape() {
    return body.classList.contains("is-landscape-layout");
  }

  function chatToggleAllowed() {
    return naturalLandscape() || (forcedLandscape() && window.innerWidth > window.innerHeight);
  }

  function fullscreenTarget() {
    // Always target full HTML document so overlay-header, chat toggle, and chat-container stay visible!
    return document.documentElement || body;
  }

  function emitForceChange() {
    try {
      body.dispatchEvent(
        new CustomEvent("streamvault:landscape-force", {
          detail: { forced: forcedLandscape(), active: body.classList.contains("is-landscape-active") },
        })
      );
    } catch (_) {}
  }

  function syncLandscapeActive() {
    const isLandscape = naturalLandscape();
    const forced = forcedLandscape();
    // CRITICAL: is-landscape-active MUST ONLY BE TRUE when width > height!
    // Never allow a portrait viewport (width <= height) to get landscape styling.
    const active = isLandscape || (forced && window.innerWidth > window.innerHeight);

    body.classList.toggle("is-landscape-active", active);
    body.classList.remove("is-fake-landscape");
    document.documentElement.classList.remove("is-mobile-fake-landscape");
    body.classList.remove("is-portrait-live");

    if (fsBtn) {
      fsBtn.setAttribute("aria-pressed", forced ? "true" : "false");
      fsBtn.setAttribute("aria-label", forced ? svT("Exit fullscreen") : svT("Fullscreen"));
      fsBtn.title = forced ? svT("Exit fullscreen") : svT("Fullscreen");
      fsBtn.classList.toggle("is-active", forced);
      fsBtn.style.display = active ? "none" : "";
    }
    if (chatBtn) {
      chatBtn.classList.toggle("is-visible", active);
      chatBtn.style.display = active ? "inline-flex" : "";
    }
    return active;
  }

  function setChatOpen(open) {
    chatOpen = !!open;
    body.classList.toggle("landscape-chat-open", chatOpen);
    if (chatBtn) {
      chatBtn.setAttribute("aria-pressed", chatOpen ? "true" : "false");
      chatBtn.setAttribute(
        "aria-label",
        chatOpen ? svT("Collapse chat") : svT("Expand chat")
      );
      chatBtn.title = chatOpen ? svT("Collapse chat") : svT("Expand chat");
    }
    try {
      localStorage.setItem(KEY, chatOpen ? "1" : "0");
    } catch (_) {}
    try {
      body.dispatchEvent(
        new CustomEvent("streamvault:landscape-chat", {
          detail: { open: chatOpen },
        })
      );
    } catch (_) {}
  }

  function tryLockLandscape() {
    try {
      const orient = screen.orientation;
      if (orient && typeof orient.lock === "function") {
        return Promise.resolve(orient.lock("landscape")).catch(function () {});
      }
    } catch (_) {}
    return Promise.resolve();
  }

  function tryUnlockOrientation() {
    try {
      const orient = screen.orientation;
      if (orient && typeof orient.unlock === "function") orient.unlock();
    } catch (_) {}
  }

  function tryRequestFullscreen(el) {
    if (!el) return Promise.resolve(false);
    const req =
      el.requestFullscreen ||
      el.webkitRequestFullscreen ||
      el.webkitEnterFullscreen ||
      el.msRequestFullscreen;
    if (!req) return Promise.resolve(false);
    try {
      const result = req.call(el);
      if (result && typeof result.then === "function") {
        return result
          .then(function () {
            return !!(document.fullscreenElement || document.webkitFullscreenElement);
          })
          .catch(function () {
            return false;
          });
      }
      return Promise.resolve(true);
    } catch (_) {
      return Promise.resolve(false);
    }
  }

  function tryExitFullscreen() {
    const exit =
      document.exitFullscreen ||
      document.webkitExitFullscreen ||
      document.msExitFullscreen;
    if (!exit) return;
    try {
      if (document.fullscreenElement || document.webkitFullscreenElement) {
        exit.call(document);
      }
    } catch (_) {}
  }

  function enterForcedLandscape() {
    body.classList.add("is-landscape-layout");
    syncLandscapeActive();
    emitForceChange();
    const target = fullscreenTarget();
    tryRequestFullscreen(target).then(function (ok) {
      hadPageFullscreen = !!ok;
      return tryLockLandscape();
    });
  }

  function exitForcedLandscape() {
    body.classList.remove("is-landscape-layout");
    hadPageFullscreen = false;
    tryUnlockOrientation();
    tryExitFullscreen();
    syncLandscapeActive();
    setChatOpen(true);
    emitForceChange();
  }

  function toggleForcedLandscape() {
    if (forcedLandscape()) exitForcedLandscape();
    else enterForcedLandscape();
  }

  function bindTap(el, fn) {
    if (!el) return;
    let lastAt = 0;
    function run(e) {
      if (e) {
        if (e.cancelable) e.preventDefault();
        e.stopPropagation();
      }
      const now = Date.now();
      if (now - lastAt < 300) return;
      lastAt = now;
      fn(e);
    }
    el.addEventListener("click", run);
    el.addEventListener("touchend", run, { passive: false });
  }

  global.StreamVaultMobileLandscape = {
    enter: enterForcedLandscape,
    exit: exitForcedLandscape,
    toggle: toggleForcedLandscape,
    isForced: forcedLandscape,
    isActive: function () {
      return naturalLandscape() || (forcedLandscape() && window.innerWidth > window.innerHeight);
    },
    openChat: function () { setChatOpen(true); },
    closeChat: function () { setChatOpen(false); },
    toggleChat: function () { setChatOpen(!body.classList.contains("landscape-chat-open")); }
  };

  let preferred = true;
  try {
    const saved = localStorage.getItem(KEY);
    if (saved === "0") preferred = false;
    else if (saved === "1") preferred = true;
  } catch (_) {}
  syncLandscapeActive();
  if (naturalLandscape() || (forcedLandscape() && window.innerWidth > window.innerHeight)) {
    setChatOpen(preferred);
  } else {
    setChatOpen(true);
  }

  bindTap(chatBtn, function () {
    if (!chatToggleAllowed()) return;
    setChatOpen(!body.classList.contains("landscape-chat-open"));
  });

  
  // Mobile Quick Controls (Play/Pause & Mute)
  const playPauseBtn = document.getElementById("livePlayPauseBtn");
  const muteBtn = document.getElementById("liveMuteBtn");

  function getVideo() {
    return document.querySelector("video");
  }

  function syncMediaState() {
    const vid = getVideo();
    if (!vid) return;

    if (playPauseBtn) {
      const isPaused = vid.paused;
      playPauseBtn.classList.toggle("is-paused", isPaused);
      playPauseBtn.setAttribute("aria-label", isPaused ? svT("Resume playback") : svT("Pause playback"));
      playPauseBtn.title = isPaused ? svT("Resume playback") : svT("Pause playback");
      const playIcon = playPauseBtn.querySelector(".icon-play");
      const pauseIcon = playPauseBtn.querySelector(".icon-pause");
      const label = playPauseBtn.querySelector(".btn-label");
      if (playIcon && pauseIcon) {
        playIcon.style.display = isPaused ? "inline-block" : "none";
        pauseIcon.style.display = isPaused ? "none" : "inline-block";
      }
      if (label) {
        label.textContent = isPaused ? svT("Play") : svT("Pause");
      }
    }

    if (muteBtn) {
      const isMuted = vid.muted || vid.volume === 0;
      muteBtn.classList.toggle("is-muted", isMuted);
      muteBtn.setAttribute("aria-label", isMuted ? svT("Unmute") : svT("Mute"));
      muteBtn.title = isMuted ? svT("Unmute") : svT("Mute");
      const unmuteIcon = muteBtn.querySelector(".icon-unmute");
      const muteIcon = muteBtn.querySelector(".icon-mute");
      const label = muteBtn.querySelector(".btn-label");
      if (unmuteIcon && muteIcon) {
        unmuteIcon.style.display = isMuted ? "none" : "inline-block";
        muteIcon.style.display = isMuted ? "inline-block" : "none";
      }
      if (label) {
        label.textContent = isMuted ? svT("Muted") : svT("Sound");
      }
    }
  }

  if (playPauseBtn) {
    bindTap(playPauseBtn, function () {
      const vid = getVideo();
      if (!vid) return;
      if (vid.paused) {
        vid.play().catch(function () {});
      } else {
        vid.pause();
      }
      syncMediaState();
    });
  }

  if (muteBtn) {
    bindTap(muteBtn, function () {
      const vid = getVideo();
      if (!vid) return;
      vid.muted = !vid.muted;
      if (!vid.muted) {
        vid.defaultMuted = false;
        vid.removeAttribute("muted");
      }
      syncMediaState();
    });
  }

  function hookVideoEvents() {
    const vid = getVideo();
    if (vid) {
      vid.addEventListener("play", syncMediaState);
      vid.addEventListener("pause", syncMediaState);
      vid.addEventListener("volumechange", syncMediaState);
      vid.addEventListener("loadedmetadata", syncMediaState);
    }
    syncMediaState();
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", hookVideoEvents);
  } else {
    hookVideoEvents();
  }

  bindTap(fsBtn, function () {
    toggleForcedLandscape();
  });

  function onOrientationChange() {
    syncLandscapeActive();
    if (naturalLandscape() || (forcedLandscape() && window.innerWidth > window.innerHeight)) {
      let open = true;
      try {
        const saved = localStorage.getItem(KEY);
        if (saved !== null) open = saved === "1";
      } catch (_) {}
      setChatOpen(open);
    } else if (forcedLandscape()) {
      exitForcedLandscape();
    } else {
      setChatOpen(true);
    }
    emitForceChange();
  }

  if (landscapeMq) {
    if (typeof landscapeMq.addEventListener === "function") {
      landscapeMq.addEventListener("change", onOrientationChange);
    } else if (typeof landscapeMq.addListener === "function") {
      landscapeMq.addListener(onOrientationChange);
    }
  }
  window.addEventListener("orientationchange", onOrientationChange);
  window.addEventListener("resize", onOrientationChange);

  function onFsChange() {
    const fsEl = document.fullscreenElement || document.webkitFullscreenElement;
    if (!fsEl && hadPageFullscreen && forcedLandscape()) {
      hadPageFullscreen = false;
      exitForcedLandscape();
    }
  }
  document.addEventListener("fullscreenchange", onFsChange);
  document.addEventListener("webkitfullscreenchange", onFsChange);
})(window);
