/* Alert sound overlay (OBS browser source). External file: the enforced CSP blocks inline scripts.
   Auth: token from #token= of the overlay URL (a legacy ?token= is moved into the fragment),
   sent to the API only as X-Alert-Token header. No token or HTTP 401: polling stops. */
(function () {
    const overlay = window.SVOverlay;
    const token = overlay.readToken().token;
    const hud = document.getElementById("hud");
    const player = document.getElementById("player");
    const AUTH_HEADERS = { "X-Alert-Token": token };

    function showHud(text) {
      hud.textContent = text;
      hud.classList.add("show");
    }

    if (!token) {
      showHud("Token fehlt – Overlay-URL mit #token=… verwenden");
      return;
    }

    const audio = overlay.createAudio(player, AUTH_HEADERS);
    let busy = false;
    let currentId = null;

    async function ack(id) {
      if (!id) return;
      try {
        await fetch("/alerts/api/ack?id=" + encodeURIComponent(id), { method: "POST", headers: AUTH_HEADERS });
      } catch (e) {
        console.error("[alerts-overlay] ack failed for item", id, e);
      }
    }

    async function tick() {
      if (busy) return "ok";
      const res = await fetch("/alerts/api/next", { headers: AUTH_HEADERS });
      if (res.status === 401) return "unauthorized";
      if (!res.ok) return "error";
      const data = await res.json();
      if (!data || !data.item) return "ok";
      const item = data.item;
      busy = true;
      currentId = item.id;
      showHud("🔔 " + (item.label || item.event));
      try {
        await audio.load(item.audio_url);
        await player.play();
      } catch (e) {
        console.error("[alerts-overlay] loading or playing the next alert failed", e);
        showHud("click once to unlock audio");
        busy = false;
        currentId = null;
      }
      return "ok";
    }

    const poller = overlay.createPoller(tick, {
      interval: 900,
      maxInterval: 30000,
      label: "alerts next",
      onUnauthorized: function () { showHud("Token ungültig – Abfrage angehalten"); },
    });

    player.addEventListener("ended", async () => {
      const id = currentId;
      currentId = null;
      busy = false;
      hud.classList.remove("show");
      audio.release();
      await ack(id);
      poller.now();
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

    poller.start();
})();
