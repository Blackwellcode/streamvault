/* Alert sound overlay (OBS browser source). External file: the enforced CSP blocks inline scripts.
   Auth: token from ?token= of the overlay URL, sent to the API only as X-Alert-Token header. */
(function () {
    const token = new URLSearchParams(location.search).get("token") || "";
    const hud = document.getElementById("hud");
    const player = document.getElementById("player");
    const AUTH_HEADERS = { "X-Alert-Token": token };

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

    function url(path) {
      const u = new URL(path, location.origin);
      return u.pathname + u.search;
    }

    async function ack(id) {
      if (!id) return;
      try {
        await fetch(url("/alerts/api/ack?id=" + encodeURIComponent(id)), { method: "POST", headers: AUTH_HEADERS });
      } catch (e) {}
    }

    async function tick() {
      if (busy) return;
      try {
        const res = await fetch(url("/alerts/api/next"), { headers: AUTH_HEADERS });
        if (!res.ok) return;
        const data = await res.json();
        if (!data || !data.item) return;
        const item = data.item;
        busy = true;
        currentId = item.id;
        hud.textContent = "🔔 " + (item.label || item.event);
        hud.classList.add("show");
        await loadAudio(item.audio_url);
        await player.play();
      } catch (e) {
        hud.textContent = "click once to unlock audio";
        hud.classList.add("show");
        busy = false;
        currentId = null;
      }
    }

    player.addEventListener("ended", async () => {
      const id = currentId;
      currentId = null;
      busy = false;
      hud.classList.remove("show");
      releaseAudio();
      await ack(id);
      tick();
    });

    player.addEventListener("error", async () => {
      const id = currentId;
      currentId = null;
      busy = false;
      hud.classList.remove("show");
      releaseAudio();
      await ack(id);
    });

    window.addEventListener("pointerdown", () => {
      player.play().catch(() => {});
    }, { once: true });

    setInterval(tick, 900);
    tick();
})();
