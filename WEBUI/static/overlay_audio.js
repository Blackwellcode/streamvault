/* Shared helper for the OBS overlays (tts_overlay.js, alerts_overlay.js).
 * <audio src> cannot send request headers, so the audio file is fetched with the
 * overlay token header and played from a blob URL that is revoked after playback.
 */
(function () {
  "use strict";

  window.SVOverlayAudio = {
    /**
     * @param {HTMLAudioElement} player
     * @param {Object<string,string>} headers  e.g. {"X-TTS-Token": token}
     */
    create: function (player, headers) {
      let objectUrl = null;

      function release() {
        if (objectUrl) {
          URL.revokeObjectURL(objectUrl);
          objectUrl = null;
        }
      }

      async function load(audioUrl) {
        const u = new URL(audioUrl, location.origin);
        u.searchParams.delete("token");
        const res = await fetch(u.pathname + u.search, { headers: headers });
        if (!res.ok) throw new Error("audio http " + res.status + " for " + u.pathname);
        release();
        objectUrl = URL.createObjectURL(await res.blob());
        player.src = objectUrl;
      }

      return { load: load, release: release };
    },
  };
})();
