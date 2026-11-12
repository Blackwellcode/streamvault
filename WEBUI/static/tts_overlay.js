/* TTS overlay (OBS browser source). External file: the enforced CSP blocks inline scripts.
   Auth: token from #token= of the overlay URL (a legacy ?token= is moved into the fragment),
   sent to the API only as X-TTS-Token header. No token or HTTP 401: polling stops. */
(function () {
    const overlay = window.SVOverlay;
    const parsed = overlay.readToken();
    const token = parsed.token;
    const streamer = parsed.params.get("streamer") || "";
    const showToast = parsed.params.get("toast") !== "0";
    const statusEl = document.getElementById("status");
    const toastEl = document.getElementById("toast");
    const userEl = toastEl.querySelector(".user");
    const textEl = toastEl.querySelector(".text");
    const player = document.getElementById("player");
    const AUTH_HEADERS = { "X-TTS-Token": token };

    function setStatus(msg) {
      statusEl.textContent = msg;
    }

    if (!token) {
      setStatus("Token fehlt – Overlay-URL mit #token=… verwenden");
      return;
    }
    if (!streamer) {
      setStatus("streamer fehlt");
      return;
    }

    const audio = overlay.createAudio(player, AUTH_HEADERS);
    let busy = false;
    let currentId = null;

    function showItem(user, text) {
      if (!showToast) return;
      userEl.textContent = user ? (user + ":") : "";
      textEl.textContent = text || "";
      toastEl.classList.add("show");
    }

    function hideToast() {
      toastEl.classList.remove("show");
    }

    function withStreamer(path) {
      const u = new URL(path, location.origin);
      u.searchParams.set("streamer", streamer);
      return u.pathname + u.search;
    }

    /** @returns {Promise<{status: number, data: any}>} */
    async function api(path, opts) {
      const o = Object.assign({}, opts || {});
      o.headers = Object.assign({}, o.headers || {}, AUTH_HEADERS);
      const res = await fetch(withStreamer(path), o);
      return { status: res.status, data: res.ok ? await res.json() : null };
    }

    async function ack(id) {
      try {
        await api("/tts/api/ack?id=" + encodeURIComponent(id), {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({id: id}),
        });
      } catch (e) {
        console.error("[tts-overlay] ack failed for item", id, e);
      }
    }

    async function pollSkip() {
      if (!currentId || !busy) return "ok";
      const res = await api("/tts/api/skipcheck?id=" + encodeURIComponent(currentId));
      if (res.status === 401) return "unauthorized";
      if (res.status !== 200) return "error";
      if (res.data && res.data.skip) {
        player.pause();
        hideToast();
        const id = currentId;
        currentId = null;
        busy = false;
        await ack(id);
        setStatus("skipped");
      }
      return "ok";
    }

    async function tick() {
      if (busy) return "ok";
      const res = await api("/tts/api/next");
      if (res.status === 401) return "unauthorized";
      if (res.status !== 200) {
        setStatus("error " + res.status);
        return "error";
      }
      if (!res.data || !res.data.item) {
        setStatus("TTS idle");
        return "ok";
      }
      const item = res.data.item;
      busy = true;
      currentId = item.id;
      setStatus("playing · " + (item.user || ""));
      showItem(item.user, item.text);
      try {
        await audio.load(item.audio_url);
      } catch (e) {
        console.error("[tts-overlay] loading audio failed for item", item.id, e);
        busy = false;
        currentId = null;
        setStatus("audio error");
        return "error";
      }
      try {
        await player.play();
      } catch (err) {
        console.error("[tts-overlay] autoplay blocked", err);
        setStatus("autoplay blocked — Interact + click once");
        const retry = async () => {
          try {
            player.muted = false;
            await player.play();
            setStatus("playing · " + (item.user || ""));
          } catch (e2) {
            console.error("[tts-overlay] autoplay retry failed", e2);
            setStatus("autoplay still blocked");
          }
        };
        window.addEventListener("pointerdown", retry, { once: true });
      }
      return "ok";
    }

    function stopAll() {
      nextPoller.stop();
      skipPoller.stop();
      setStatus("Token ungültig – Abfrage angehalten");
    }
    const nextPoller = overlay.createPoller(tick, { interval: 1200, maxInterval: 30000, label: "tts next", onUnauthorized: stopAll });
    const skipPoller = overlay.createPoller(pollSkip, { interval: 800, maxInterval: 30000, label: "tts skipcheck", onUnauthorized: stopAll });

    player.addEventListener("ended", async () => {
      const id = currentId;
      hideToast();
      currentId = null;
      busy = false;
      audio.release();
      if (id) await ack(id);
      setStatus("TTS idle");
      nextPoller.now();
    });

    player.addEventListener("error", async () => {
      const id = currentId;
      hideToast();
      currentId = null;
      busy = false;
      audio.release();
      if (id) await ack(id);
      setStatus("audio error");
    });

    // Unlock autoplay on first user gesture (OBS interaction / click)
    window.addEventListener("pointerdown", () => {
      player.muted = false;
      player.play().catch((e) => console.error("[tts-overlay] unlock play failed", e));
    }, {once: true});

    nextPoller.start();
    skipPoller.start();
})();
