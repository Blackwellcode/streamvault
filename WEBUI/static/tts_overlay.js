/* TTS overlay (OBS browser source). External file: the enforced CSP blocks inline scripts.
   Auth: token from ?token= of the overlay URL, sent to the API only as X-TTS-Token header. */
(function () {
    // OBS cannot send headers: the token sits in the URL fragment (#token=..., never sent to
    // the server) or, for older URLs, in ?token=. It is passed to the API only as a header.
    const hashParams = new URLSearchParams(location.hash.replace(/^#/, ""));
    const queryParams = new URLSearchParams(location.search);
    const token = hashParams.get("token") || queryParams.get("token") || "";
    const streamer = queryParams.get("streamer") || hashParams.get("streamer") || "";
    const showToast = (queryParams.get("toast") || hashParams.get("toast")) !== "0";
    const statusEl = document.getElementById("status");
    const toastEl = document.getElementById("toast");
    const userEl = toastEl.querySelector(".user");
    const textEl = toastEl.querySelector(".text");
    const player = document.getElementById("player");
    const AUTH_HEADERS = { "X-TTS-Token": token };

    const audio = window.SVOverlayAudio.create(player, AUTH_HEADERS);

    let busy = false;
    let currentId = null;
    let skipTimer = null;

    function setStatus(msg) {
      statusEl.textContent = msg;
    }

    function showItem(user, text) {
      if (!showToast) return;
      userEl.textContent = user ? (user + ":") : "";
      textEl.textContent = text || "";
      toastEl.classList.add("show");
    }

    function hideToast() {
      toastEl.classList.remove("show");
    }

    function withAuth(path) {
      const u = new URL(path, location.origin);
      u.searchParams.set("streamer", streamer);
      return u.pathname + u.search;
    }

    async function api(path, opts) {
      const o = Object.assign({}, opts || {});
      o.headers = Object.assign({}, o.headers || {}, AUTH_HEADERS);
      const res = await fetch(withAuth(path), o);
      if (!res.ok) throw new Error("http " + res.status);
      return res.json();
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
      if (!currentId || !busy) return;
      try {
        const data = await api("/tts/api/skipcheck?id=" + encodeURIComponent(currentId));
        if (data && data.skip) {
          player.pause();
          hideToast();
          const id = currentId;
          currentId = null;
          busy = false;
          await ack(id);
          setStatus("skipped");
        }
      } catch (e) {
        console.error("[tts-overlay] skip check failed for item", currentId, e);
      }
    }

    async function tick() {
      if (!streamer) {
        setStatus("streamer fehlt");
        return;
      }
      if (busy) return;
      try {
        const data = await api("/tts/api/next");
        if (!data || !data.item) {
          setStatus("TTS idle");
          return;
        }
        const item = data.item;
        busy = true;
        currentId = item.id;
        setStatus("playing · " + (item.user || ""));
        showItem(item.user, item.text);
        await audio.load(item.audio_url);
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
          return;
        }
      } catch (e) {
        console.error("[tts-overlay] loading or playing the next item failed", e);
        setStatus("error");
      }
    }

    player.addEventListener("ended", async () => {
      const id = currentId;
      hideToast();
      currentId = null;
      busy = false;
      audio.release();
      if (id) await ack(id);
      setStatus("TTS idle");
      tick();
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

    setInterval(tick, 1200);
    setInterval(pollSkip, 800);
    tick();
})();
