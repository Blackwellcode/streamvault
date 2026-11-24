"""Live watch player: start buffer, rebuffering, end detection and HLS fallbacks.

The JavaScript runs as rendered by the watch page, in Node with a fake clock.
"""

from __future__ import annotations

import json
import re
import unittest
from unittest import mock

try:
    from test_recorder_reliability import IsolatedBaseTest  # noqa: F401  (sets VOD_* env first)
    from test_webui_frontend import TEMPLATES, require_node, run_node
    from webui_test_helpers import admin_test_client
except ImportError:  # run as tests.test_livebot_player
    from tests.test_recorder_reliability import IsolatedBaseTest  # noqa: F401
    from tests.test_webui_frontend import TEMPLATES, require_node, run_node
    from tests.webui_test_helpers import admin_test_client

from core import config, twitch_api  # noqa: E402

DESKTOP_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"

CLOCK = r"""
let t = 0;
const timers = [];
Date.now = function() { return t; };
global.setTimeout = function(fn, ms) { timers.push({ at: t + (ms || 0), fn: fn }); return timers.length; };
function tick(ms) {
  const end = t + ms;
  while (true) {
    timers.sort(function(a, b) { return a.at - b.at; });
    if (!timers.length || timers[0].at > end) break;
    const next = timers.shift();
    t = next.at;
    next.fn();
  }
  t = end;
}
"""

START_BUFFER = CLOCK + r"""
%(whenBuffered)s
function run(dataStartMs) {
  const start = t;
  const video = {
    currentTime: 0,
    get buffered() {
      if (dataStartMs === null || t - start < dataStartMs) return { length: 0 };
      const ahead = 0.2 + (t - start - dataStartMs) / 1000;
      return { length: 1, end: function() { return ahead; } };
    }
  };
  const out = { doneAt: null, ahead: null };
  whenBuffered(video, 1.5, 5000, function() {
    out.doneAt = t - start;
    out.ahead = video.buffered.length ? video.buffered.end(0) : 0;
  });
  for (let i = 0; i < 1200 && out.doneAt === null; i++) tick(100);
  return out;
}
console.log(JSON.stringify({ cold: run(8000), warm: run(0), never: run(null) }));
"""

REBUFFER = CLOCK + r"""
const listeners = {};
const video = {
  paused: false, currentTime: 0, ahead: 2.0, plays: 0, pauses: 0,
  get buffered() { const self = this; return { length: 1, end: function() { return self.currentTime + self.ahead; } }; },
  addEventListener: function(name, fn) { (listeners[name] = listeners[name] || []).push(fn); },
  pause: function() { this.paused = true; this.pauses += 1; },
  play: function() { this.paused = false; this.plays += 1; return { catch: function() {} }; }
};
function emit(name) { (listeners[name] || []).forEach(function(fn) { fn(); }); }
let ready = true;
let mpegtsPlayer = { id: 1 };
%(whenBuffered)s
%(holdPlayback)s
%(releasePlayback)s
%(rebuffer)s
function advance(ms) {
  for (let i = 0; i < ms / 100; i++) { if (video.paused) video.ahead += 0.1; tick(100); }
}
const out = {};
video.ahead = 0.05; emit('waiting');
out.beforeFirstPlay = video.pauses;
emit('playing'); emit('waiting');
out.pausedOnStall = video.paused;
out.heldOnStall = video._streamVaultBuffering;
advance(600);
out.stillWaitingAt600 = video.paused;
advance(2000);
out.resumed = !video.paused; out.heldAfterResume = video._streamVaultBuffering; out.aheadAtResume = Math.round(video.ahead * 100) / 100;
const p0 = video.pauses; video.ahead = 0.05; emit('waiting'); emit('waiting');
out.pausesForDoubleWaiting = video.pauses - p0;
advance(3000);
mpegtsPlayer = null; const p1 = video.pauses; video.ahead = 0.05; emit('waiting');
out.hlsPauses = video.pauses - p1;
mpegtsPlayer = { id: 2 }; video.paused = false; emit('waiting');
mpegtsPlayer = { id: 3 }; const plays = video.plays; advance(3000);
out.resumedReplaced = video.plays - plays;
console.log(JSON.stringify(out));
"""

FALLBACK = CLOCK + r"""
function dbg() {}
let tsPlayed = false, tsFailuresBeforePlay = 0, tsFailuresAfterPlay = [], preferLowLatencyHls = false;
let directHlsFailed = false;
const playlistUrl = '/live/playlist/demochan';
%(noteTsFailure)s
%(hlsSourceUrl)s
%(shouldUseFlaskPlaylist)s
const out = {};
noteTsFailure(); out.before1 = preferLowLatencyHls;
noteTsFailure(); out.before2 = preferLowLatencyHls;
preferLowLatencyHls = false; tsPlayed = true; out.after = [];
t = 1000; noteTsFailure(); t = 130000; noteTsFailure(); t = 140000; noteTsFailure(); out.spread = preferLowLatencyHls;
t = 150000; noteTsFailure(); out.burst = preferLowLatencyHls;
out.direct = hlsSourceUrl('/live/demochan/stream.m3u8');
out.switchOn = ['manifestLoadError', 'manifestLoadTimeOut', 'manifestParsingError'].map(shouldUseFlaskPlaylist);
out.keepOn = ['fragLoadError', 'levelLoadError', 'bufferStalledError', ''].map(shouldUseFlaskPlaylist);
directHlsFailed = true;
out.flask = hlsSourceUrl('/live/demochan/stream.m3u8');
out.onlyOnce = shouldUseFlaskPlaylist('manifestLoadError');
console.log(JSON.stringify(out));
"""

HLS_PLAYER = r"""
const loaded = [];
let handlers = {}, reconnects = 0;
class Hls {
  static isSupported() { return true; }
  on(name, fn) { handlers[name] = fn; }
  loadSource(url) { loaded.push(url); }
  attachMedia() {}
  startLoad() { loaded.push('startLoad'); }
  recoverMediaError() {}
}
Hls.Events = { MANIFEST_PARSED: 'parsed', ERROR: 'error' };
Hls.ErrorTypes = { NETWORK_ERROR: 'networkError', MEDIA_ERROR: 'mediaError' };
global.window = { Hls: Hls }; global.Hls = Hls;
function dbg() {} function tryAutoplay() {} function startTelemetry() {}
let released = 0;
function releasePlayback() { released += 1; }
function scheduleReconnect() { reconnects += 1; }
let hls = null, directHlsFailed = false;
const channelName = 'demochan', isMobilePlayer = false, playlistUrl = '/live/playlist/demochan';
%(hlsSourceUrl)s
%(shouldUseFlaskPlaylist)s
%(initLowLatencyHlsPlayer)s
initLowLatencyHlsPlayer({}, '/live/demochan/stream.m3u8');
const notFound = { fatal: true, type: 'networkError', details: 'manifestLoadError', response: { code: 404 } };
handlers.error(null, notFound);
handlers.error(null, notFound);
initLowLatencyHlsPlayer({}, '/live/demochan/stream.m3u8');
console.log(JSON.stringify({ loaded: loaded, reconnects: reconnects, released: released }));
"""


MEDIA_SESSION = r"""
const listeners = {}, intervals = [];
const video = {
  paused: false, ended: false, seeking: false, plays: 0, currentTime: 1, duration: Infinity,
  addEventListener: function(name, fn) { (listeners[name] = listeners[name] || []).push(fn); },
  setAttribute: function() {}, getAttribute: function() { return null; }, closest: function() { return null; },
  play: function() { this.plays += 1; this.paused = false; return Promise.resolve(); },
  pause: function() { this.paused = true; (listeners.pause || []).forEach(function(fn) { fn(); }); }
};
global.document = {
  hidden: true, visibilityState: 'hidden', readyState: 'complete', body: {}, title: '',
  addEventListener: function() {}, querySelector: function() { return null; }, hasFocus: function() { return false; }
};
global.window = {
  document: global.document, addEventListener: function() {}, location: { href: 'http://x/' },
  setInterval: function(fn) { intervals.push(fn); return intervals.length; }, clearInterval: function() {},
  setTimeout: function(fn) { fn(); return 1; }, clearTimeout: function() {}
};
global.setInterval = global.window.setInterval;
global.Audio = function() { return { play: function() {}, pause: function() {} }; };
%(source)s
global.window.StreamVaultMediaSession.attach(video, { live: true });
(listeners.playing || []).forEach(function(fn) { fn(); });
const out = {};
video._streamVaultBuffering = true;
video.pause();
intervals.forEach(function(fn) { fn(); });
out.playsWhileBuffering = video.plays;
video._streamVaultBuffering = false;
intervals.forEach(function(fn) { if (video.paused) fn(); });
out.playsAfterRelease = video.plays;
console.log(JSON.stringify(out));
process.exit(0);
"""


class LivePlayerTests(IsolatedBaseTest):
    def setUp(self):
        super().setUp()
        from WEBUI import webui
        stream = {"login": "demochan", "display_name": "D", "title": "", "game_name": "", "viewer_count": 1,
                  "is_live": True, "monitored": True}
        client = admin_test_client()
        with mock.patch.object(config, "LIVE_TWITCH_EMBED", False), \
                mock.patch.object(twitch_api, "get_stream", return_value=stream), \
                mock.patch.object(twitch_api, "enrich_monitored_streams", return_value=[]), \
                mock.patch.object(webui, "touch_live_heartbeat"), \
                mock.patch.object(webui, "add_live_channel"):
            self.body = client.get(
                "/live/watch/demochan", headers={"User-Agent": DESKTOP_UA}
            ).get_data(as_text=True).replace("\r\n", "\n")

    def _function(self, name):
        match = re.search(r"\n  function " + name + r"\(.*?\n  }\n", self.body, re.S)
        self.assertIsNotNone(match, f"{name}() not found")
        return match.group(0)

    def _node(self, harness, *functions, **blocks):
        require_node(self)
        parts = {name: self._function(name) for name in functions}
        return json.loads(run_node(harness % {**parts, **blocks}))

    def test_start_buffer_counts_from_the_first_data(self):
        out = self._node(START_BUFFER, "whenBuffered")
        self.assertGreaterEqual(out["cold"]["ahead"], 1.5)
        self.assertGreaterEqual(out["warm"]["ahead"], 1.5)
        self.assertLessEqual(out["warm"]["doneAt"], 2000)
        self.assertLessEqual(out["never"]["doneAt"], 30000)

    def test_rebuffer_after_a_stall(self):
        block = re.search(r"\n  // Rebuffer:.*?'waiting', rebufferAfterStall\);\n", self.body, re.S)
        self.assertIsNotNone(block)
        out = self._node(REBUFFER, "whenBuffered", "holdPlayback", "releasePlayback", rebuffer=block.group(0))
        self.assertEqual(out["beforeFirstPlay"], 0)
        self.assertTrue(out["pausedOnStall"])
        self.assertTrue(out["heldOnStall"])
        self.assertFalse(out["heldAfterResume"])
        self.assertTrue(out["stillWaitingAt600"])
        self.assertTrue(out["resumed"])
        self.assertGreaterEqual(out["aheadAtResume"], 1.0)
        self.assertEqual(out["pausesForDoubleWaiting"], 1)
        self.assertEqual(out["hlsPauses"], 0)
        self.assertEqual(out["resumedReplaced"], 0)

    def test_media_session_ignores_the_buffering_pause(self):
        """A pause for buffering is neither resumed early nor taken for a user pause."""
        require_node(self)
        source = (TEMPLATES.parent / "static" / "media_session.js").read_text(encoding="utf-8")
        out = json.loads(run_node(MEDIA_SESSION % {"source": source}))
        self.assertEqual(out["playsWhileBuffering"], 0)
        self.assertEqual(out["playsAfterRelease"], 1)

    def test_repeated_ts_failures_switch_to_hls(self):
        out = self._node(FALLBACK, "noteTsFailure", "hlsSourceUrl", "shouldUseFlaskPlaylist")
        self.assertEqual((out["before1"], out["before2"]), (False, True))
        self.assertFalse(out["spread"], "single interruptions far apart keep the real-time stream")
        self.assertTrue(out["burst"])
        self.assertEqual(out["direct"], "/live/demochan/stream.m3u8")
        self.assertEqual(out["switchOn"], [True, True, True])
        self.assertEqual(out["keepOn"], [False, False, False, False])
        self.assertEqual(out["flask"], "/live/playlist/demochan")
        self.assertFalse(out["onlyOnce"])

    def test_missing_nginx_hls_route_uses_the_webui_playlist(self):
        out = self._node(HLS_PLAYER, "hlsSourceUrl", "shouldUseFlaskPlaylist", "initLowLatencyHlsPlayer")
        self.assertEqual(out["loaded"], [
            "/live/demochan/stream.m3u8", "/live/playlist/demochan", "startLoad", "/live/playlist/demochan",
        ])
        self.assertEqual(out["reconnects"], 0)
        self.assertEqual(out["released"], 2, "HLS must not stay held by the real-time player's buffering")

    def test_end_of_the_realtime_stream_reconnects(self):
        handler = re.search(r"mpegts\.Events\.LOADING_COMPLETE, function\(\) \{.*?\n        \}\);", self.body, re.S)
        self.assertIsNotNone(handler)
        self.assertIn("noteTsFailure();", handler.group(0))
        self.assertIn("scheduleReconnect(", handler.group(0))

    def test_boot_connects_the_realtime_stream_immediately(self):
        boot = re.search(r"'proxy_boot', \{\}\);.*?setInterval\(touchHeartbeat, 15000\);", self.body, re.S)
        self.assertIsNotNone(boot)
        self.assertRegex(boot.group(0), r"if \(canUseRealtimeTs\(\)\) \{\s*(//[^\n]*\n\s*)*startPlayer\(\);")
        self.assertRegex(boot.group(0), r"\} else \{\s*checkPlaylist\(\);\s*ensureReadyPoll\(400\);\s*\}")
        reconnect = self._function("scheduleReconnect")
        self.assertIn("checkPlaylist();", reconnect)
        self.assertNotIn("startPlayer();", reconnect)

    def test_latency_targets_leave_headroom(self):
        for text in (
            "liveSyncTargetLatency: isMobilePlayer ? 1.0 : 1.5",
            "liveSyncMaxLatency: isMobilePlayer ? 2.5 : 3.0",
            "liveBufferLatencyMaxLatency: isMobilePlayer ? 2.5 : 3.0",
            "liveBufferLatencyMinRemain: isMobilePlayer ? 0.6 : 1.0",
            "const startBufferSec = isMobilePlayer ? 1.0 : 1.5;",
            "video.autoplay = false;",
            "video.currentTime = Math.max(0, end - 1.5);",
        ):
            with self.subTest(text=text):
                self.assertIn(text, self.body)

    def test_rendered_script_is_valid_javascript(self):
        require_node(self)
        script = next(part for part in re.findall(r"<script[^>]*>(.*?)</script>", self.body, re.S)
                      if "function whenBuffered" in part)
        run_node("new Function(" + json.dumps(script) + ");")


if __name__ == "__main__":
    unittest.main()
