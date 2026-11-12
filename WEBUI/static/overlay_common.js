/* Shared helpers for the OBS overlays (tts_overlay.js, alerts_overlay.js).
 *
 * - readToken(): token from #token= (preferred, never sent to the server) or a legacy
 *   ?token=; a query token is moved into the fragment with history.replaceState.
 * - createAudio(): <audio src> cannot send headers, so audio is fetched with the overlay
 *   token header and played from a blob URL that is revoked after playback.
 * - createPoller(): polls a task; 401 stops polling, errors back off exponentially.
 *
 * Plain functions without DOM access at load time, so tests can require() this file in node.
 */
(function (root) {
  "use strict";

  const QUERY_TOKEN_WARNING =
    "[overlay] ?token= in the URL reaches server and proxy logs; the address was rewritten to #token=. " +
    "Use the #token= URL in OBS.";

  /**
   * @param {string} href  full URL of the overlay page
   * @returns {{token: string, fromQuery: boolean, cleanUrl: string, params: URLSearchParams}}
   *   cleanUrl: same URL with the token moved from the query into the fragment
   *   params:   query and fragment parameters merged (fragment wins), token removed
   */
  function parseToken(href) {
    const url = new URL(href);
    const hash = new URLSearchParams(url.hash.replace(/^#/, ""));
    const query = url.searchParams;
    let token = (hash.get("token") || "").trim();
    let fromQuery = false;
    if (!token && (query.get("token") || "").trim()) {
      token = query.get("token").trim();
      fromQuery = true;
    }
    if (query.has("token")) {
      query.delete("token");
      if (token) hash.set("token", token);
    }
    const fragment = hash.toString();
    const cleanUrl = url.origin + url.pathname + (query.toString() ? "?" + query.toString() : "") +
      (fragment ? "#" + fragment : "");
    const params = new URLSearchParams(query);
    hash.forEach(function (value, key) { if (key !== "token") params.set(key, value); });
    params.delete("token");
    return { token: token, fromQuery: fromQuery, cleanUrl: cleanUrl, params: params };
  }

  /** Browser side of parseToken(): rewrites the visible URL and warns once about ?token=. */
  function readToken() {
    const parsed = parseToken(root.location.href);
    if (parsed.cleanUrl !== root.location.href && root.history && root.history.replaceState) {
      root.history.replaceState(null, "", parsed.cleanUrl);
    }
    if (parsed.fromQuery) root.console.warn(QUERY_TOKEN_WARNING);
    return parsed;
  }

  function createAudio(player, headers) {
    let objectUrl = null;

    function release() {
      if (objectUrl) {
        URL.revokeObjectURL(objectUrl);
        objectUrl = null;
      }
    }

    async function load(audioUrl) {
      const u = new URL(audioUrl, root.location.origin);
      u.searchParams.delete("token");
      const res = await root.fetch(u.pathname + u.search, { headers: headers });
      if (!res.ok) throw new Error("audio http " + res.status + " for " + u.pathname);
      release();
      objectUrl = URL.createObjectURL(await res.blob());
      player.src = objectUrl;
    }

    return { load: load, release: release };
  }

  /**
   * @param {() => Promise<"ok"|"unauthorized"|"error">} task
   * @param {{interval: number, maxInterval?: number, onUnauthorized?: Function,
   *          schedule?: Function, cancel?: Function, label?: string}} opts
   */
  function createPoller(task, opts) {
    const base = opts.interval;
    const max = opts.maxInterval || 30000;
    const schedule = opts.schedule || function (fn, ms) { return setTimeout(fn, ms); };
    const cancel = opts.cancel || function (id) { clearTimeout(id); };
    let delay = base;
    let timer = null;
    let stopped = true;
    let running = false;

    async function run() {
      if (stopped || running) return;
      running = true;
      let result;
      try {
        result = await task();
      } catch (e) {
        root.console.error("[overlay] " + (opts.label || "poll") + " failed", e);
        result = "error";
      }
      running = false;
      if (stopped) return;
      if (result === "unauthorized") {
        stopped = true;
        if (opts.onUnauthorized) opts.onUnauthorized();
        return;
      }
      delay = result === "error" ? Math.min(delay * 2, max) : base;
      timer = schedule(run, delay);
    }

    return {
      start: function () { stopped = false; run(); },
      stop: function () { stopped = true; if (timer !== null) cancel(timer); },
      now: function () { if (timer !== null) cancel(timer); run(); },
      get delay() { return delay; },
      get stopped() { return stopped; },
    };
  }

  const api = {
    parseToken: parseToken,
    readToken: readToken,
    createAudio: createAudio,
    createPoller: createPoller,
    QUERY_TOKEN_WARNING: QUERY_TOKEN_WARNING,
  };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    root.SVOverlay = api;
  }
})(typeof window !== "undefined" ? window : globalThis);
