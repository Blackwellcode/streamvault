/* TTS overlay (OBS browser source). External file: the enforced CSP blocks inline scripts.
   Auth: token from ?token= of the overlay URL, sent to the API only as X-TTS-Token header. */
(function () {
    const params = new URLSearchParams(location.search);
    const token = params.get("token") || "";
    const streamer = params.get("streamer") || "";
    const showToast = params.get("toast") !== "0";
    const statusEl = document.getElementById("status");
    const toastEl = document.getElementById("toast");
    const userEl = toastEl.querySelector(".user");
    const textEl = toastEl.querySelector(".text");
    const player = document.getElementById("player");
    const AUTH_HEADERS = { "X-TTS-Token": token };

    let currentObjectUrl = null;

    function releaseAudio() {
      if (currentObjectUrl) {
        URL.revokeObjectURL(currentObjectUrl);
        currentObjectUrl = null;
      }
    }

    // <audio src> cannot send headers: fetch the file with the token header, play it as a blob URL.
    async function loadAudio(audioUrl) {
      const u = new URL(audioUrl, location.origin);
      u.searchParams.delete("token");
      const res = await fetch(u.pathname + u.search, { headers: AUTH_HEADERS });
      if (!res.ok) throw new Error("audio http " + res.status);
      releaseAudio();
      currentObjectUrl = URL.createObjectURL(await res.blob());
      player.src = currentObjectUrl;
    }

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
      } catch (e) {}
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
      } catch (e) {}
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
        await loadAudio(item.audio_url);
        try {
          await player.play();
        } catch (err) {
          setStatus("autoplay blocked — Interact + click once");
          const retry = async () => {
            try {
              player.muted = false;
              await player.play();
              setStatus("playing · " + (item.user || ""));
            } catch (e2) {
              setStatus("autoplay still blocked");
            }
          };
          window.addEventListener("pointerdown", retry, { once: true });
          return;
        }
      } catch (e) {
        setStatus("error");
      }
    }

    player.addEventListener("ended", async () => {
      const id = currentId;
      hideToast();
      currentId = null;
      busy = false;
      releaseAudio();
      if (id) await ack(id);
      setStatus("TTS idle");
      tick();
    });

    player.addEventListener("error", async () => {
      const id = currentId;
      hideToast();
      currentId = null;
      busy = false;
      releaseAudio();
      if (id) await ack(id);
      setStatus("audio error");
    });

    // Unlock autoplay on first user gesture (OBS interaction / click)
    window.addEventListener("pointerdown", () => {
      player.muted = false;
      player.play().catch(() => {});
    }, {once: true});

    setInterval(tick, 1200);
    setInterval(pollSkip, 800);
    tick();
})();
