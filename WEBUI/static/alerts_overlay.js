/* Alert sound overlay (OBS browser source). External file: the enforced CSP blocks inline scripts.
   Auth: token from ?token= of the overlay URL, sent to the API only as X-Alert-Token header. */
(function () {
    const token = new URLSearchParams(location.search).get("token") || "";
    const hud = document.getElementById("hud");
    const player = document.getElementById("player");
    const AUTH_HEADERS = { "X-Alert-Token": token };

    const audio = window.SVOverlayAudio.create(player, AUTH_HEADERS);
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
      } catch (e) {
        console.error("[alerts-overlay] ack failed for item", id, e);
      }
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
        await audio.load(item.audio_url);
        await player.play();
      } catch (e) {
        console.error("[alerts-overlay] loading or playing the next alert failed", e);
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
      audio.release();
      await ack(id);
      tick();
    });

    player.addEventListener("error", async () => {
      const id = currentId;
      currentId = null;
      busy = false;
      hud.classList.remove("show");
      audio.release();
      await ack(id);
    });

    window.addEventListener("pointerdown", () => {
      player.play().catch((e) => console.error("[alerts-overlay] unlock play failed", e));
    }, { once: true });

    setInterval(tick, 900);
    tick();
})();
