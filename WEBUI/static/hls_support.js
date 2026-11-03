(function (global) {
  "use strict";

  function canNativeHls(video) {
    if (!video || typeof video.canPlayType !== "function") return false;
    return !!(
      video.canPlayType("application/vnd.apple.mpegurl") ||
      video.canPlayType("application/x-mpegURL")
    );
  }

  function isAppleDevice() {
    return /iPad|iPhone|iPod|Macintosh/i.test(navigator.userAgent || "");
  }

  // Only Apple devices decode HLS reliably via <video src>; everything else uses MSE + hls.js.
  function preferNativeHls(video) {
    return canNativeHls(video) && isAppleDevice();
  }

  // StreamVault does not support casting: hide the browsers' built-in remote playback UI.
  function disableRemotePlayback(video) {
    if (!video) return;
    try {
      video.disableRemotePlayback = true;
    } catch (_) {}
    video.setAttribute("disableremoteplayback", "");
    video.setAttribute("x-webkit-airplay", "deny");
  }

  global.StreamVaultHls = {
    canNativeHls: canNativeHls,
    preferNativeHls: preferNativeHls,
    disableRemotePlayback: disableRemotePlayback
  };

  function disableAll() {
    document.querySelectorAll("video").forEach(disableRemotePlayback);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", disableAll, { once: true });
  } else {
    disableAll();
  }
})(window);
