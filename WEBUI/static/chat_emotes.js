(function () {
  'use strict';
  var svT = window.svT || function (s) { return s; };

  const nativeIdPattern = /^[A-Za-z0-9_-]{1,64}$/;
  const badgePartPattern = /^[A-Za-z0-9_-]{1,64}$/;
  const localBadgeUrlPattern = /^\/chat\/badge-assets\/[A-Za-z0-9_-]{1,64}\/[A-Za-z0-9_-]{1,64}\/[124]x\.(png|webp|gif|jpg)$/;
  const userColorPattern = /^#[0-9A-Fa-f]{6}$/;
  const emoteMap = new Map();
  const badgeMap = new Map();
  const loadCache = new Map();
  const badgeLoadCache = new Map();
  const badgeTitles = {
    admin: 'Admin',
    bits: 'Bits',
    broadcaster: 'Broadcaster',
    founder: 'Founder',
    moderator: 'Moderator',
    partner: 'Partner',
    premium: 'Prime',
    staff: 'Staff',
    sub_gifter: 'Sub-Gifter',
    subscriber: 'Subscriber',
    turbo: 'Turbo',
    vip: 'VIP'
  };

  function escapeHtml(value) {
    const div = document.createElement('div');
    div.textContent = value == null ? '' : String(value);
    return div.innerHTML;
  }

  function normalizeUrl(url) {
    const value = String(url || '').trim();
    if (value.startsWith('//')) return `https:${value}`;
    if (value.startsWith('https://')) return value;
    return '';
  }

  function normalizeBadgeUrl(url) {
    const raw = String(url || '').trim();
    if (localBadgeUrlPattern.test(raw)) return raw;
    const value = normalizeUrl(raw);
    try {
      const parsed = new URL(value);
      if (parsed.hostname === 'static-cdn.jtvnw.net' && parsed.pathname.startsWith('/badges/')) return value;
    } catch (e) {}
    return '';
  }

  function twitchEmoteUrl(id) {
    if (!nativeIdPattern.test(String(id || ''))) return '';
    return `https://static-cdn.jtvnw.net/emoticons/v2/${id}/default/dark/1.0`;
  }

  function userColor(value) {
    const color = String(value || '').trim();
    return userColorPattern.test(color) ? color : '';
  }

  function normalizeBadgeSet(value) {
    return String(value || '').trim().replace(/-/g, '_');
  }

  function badgeFallbackIconPath(setId) {
    const key = normalizeBadgeSet(setId);
    const icons = {
      admin: '<path d="M12 2.4 4.6 5.4v5.7c0 4.5 3.1 8.5 7.4 10.1 4.3-1.6 7.4-5.6 7.4-10.1V5.4L12 2.4Zm4.2 7.4-4.8 4.8a1 1 0 0 1-1.4 0l-2.2-2.2 1.4-1.4 1.5 1.5 4.1-4.1 1.4 1.4Z"/>',
      bits: '<path d="m12 2.4 7.4 9.1L12 21.6 4.6 11.5 12 2.4Zm-2.5 9.2 2.5 5.6 2.5-5.6H9.5Zm.4-1.9h4.2L12 5.5 9.9 9.7Z"/>',
      broadcaster: '<path d="M5.8 5.2h9.5A3.2 3.2 0 0 1 18.5 8v.9l2.2-1.4A.8.8 0 0 1 22 8.2v7.6a.8.8 0 0 1-1.3.7l-2.2-1.4v.9a3.2 3.2 0 0 1-3.2 2.8H5.8A3.2 3.2 0 0 1 2.6 16V8.4a3.2 3.2 0 0 1 3.2-3.2Zm4.1 4.1v5.4l4.6-2.7-4.6-2.7Z"/>',
      founder: '<path d="M12 2.7 19.3 8l-2.8 11.3h-9L4.7 8 12 2.7Zm4.8 5.9-3.2-.3L12 5.5l-1.6 2.8-3.2.3 2.2 2.4-.7 3.1 3.3-1.5 3.3 1.5-.7-3.1 2.2-2.4Z"/>',
      moderator: '<path d="M12 2.4 4.6 5.4v5.7c0 4.5 3.1 8.5 7.4 10.1 4.3-1.6 7.4-5.6 7.4-10.1V5.4L12 2.4Zm4.8 7.5-5.4 5.4a1 1 0 0 1-1.4 0l-2.8-2.8 1.5-1.5 2 2 4.7-4.7 1.4 1.6Z"/>',
      partner: '<path d="M12 2.4 4.6 5.4v5.7c0 4.5 3.1 8.5 7.4 10.1 4.3-1.6 7.4-5.6 7.4-10.1V5.4L12 2.4Zm0 3.6 1.8 3.6 4 .6-2.9 2.8.7 4-3.6-1.9L8.4 17l.7-4-2.9-2.8 4-.6L12 6Z"/>',
      premium: '<path d="M4.2 7.3a1 1 0 0 1 1.4-.1l3.1 2.7 2.4-5a1 1 0 0 1 1.8 0l2.4 5 3.1-2.7a1 1 0 0 1 1.6 1l-2.1 8.6a2.4 2.4 0 0 1-2.3 1.8H8.4a2.4 2.4 0 0 1-2.3-1.8L4 8.2a1 1 0 0 1 .2-.9Zm4.4 8.2h6.8v-1.8H8.6v1.8Z"/>',
      staff: '<path d="M12 2.4 4.6 5.4v5.7c0 4.5 3.1 8.5 7.4 10.1 4.3-1.6 7.4-5.6 7.4-10.1V5.4L12 2.4Zm1.2 4.8h-2.4v3.6H7.2v2.4h3.6v3.6h2.4v-3.6h3.6v-2.4h-3.6V7.2Z"/>',
      sub_gifter: '<path d="M4.5 10.2h15v7.1a2.5 2.5 0 0 1-2.5 2.5H7a2.5 2.5 0 0 1-2.5-2.5v-7.1Zm6.4 0v9.6h2.2v-9.6h-2.2ZM3.4 7.1A1.9 1.9 0 0 1 5.3 5.2h3.1A2.3 2.3 0 0 1 12 3.5a2.3 2.3 0 0 1 3.6 1.7h3.1a1.9 1.9 0 0 1 1.9 1.9v1.6H3.4V7.1Zm5.4-1.8c-.8 0-.9 1.1-.1 1.1h2.1c-.4-.6-1-1.1-2-1.1Zm6.4 0c-1 0-1.6.5-2 1.1h2.1c.8 0 .7-1.1-.1-1.1Z"/>',
      subscriber: '<path d="m12 2.8 2.7 5.4 5.9.9-4.3 4.2 1 5.9-5.3-2.8-5.3 2.8 1-5.9-4.3-4.2 5.9-.9L12 2.8Z"/>',
      turbo: '<path d="M13.2 2.7c2.6-.6 5.2-.2 6.9.6.8 1.7 1.2 4.3.6 6.9l-4.6 1.1-3.4-3.4 1.1-4.6-.6-.6ZM8 9.2l6.8 6.8-1.4 1.4-6.8-6.8L8 9.2Zm-2.8 5.1 4.5 4.5-5.8 1.3 1.3-5.8ZM17.6 7.5a1.6 1.6 0 1 0-2.2-2.2 1.6 1.6 0 0 0 2.2 2.2Z"/>',
      vip: '<path d="M12 2.8 21 9.4 12 21.2 3 9.4l9-6.6Zm-4.8 7 4.8 6.8 4.8-6.8H7.2Zm1.3-1.7h7L12 5.5 8.5 8.1Z"/>'
    };
    return icons[key] || '<path d="M12 2.8 4.4 6.2v6.4c0 4.2 3 7.5 7.6 8.6 4.6-1.1 7.6-4.4 7.6-8.6V6.2L12 2.8Zm0 4.2 1.6 3.2 3.5.5-2.5 2.5.6 3.5-3.2-1.7-3.2 1.7.6-3.5-2.5-2.5 3.5-.5L12 7Z"/>';
  }

  function badgeTitle(badge) {
    const key = normalizeBadgeSet(badge.set_id);
    let title = badgeTitles[key] || badge.set_id;
    if (badge.info) {
      title += key === 'subscriber' ? ` (${badge.info} months)` : ` (${badge.info})`;
    } else if (badge.id && badge.id !== '0' && badge.id !== '1') {
      title += ` (${badge.id})`;
    }
    return title;
  }

  function badgeKey(setId, id) {
    return `${String(setId || '').trim()}/${String(id || '').trim()}`;
  }

  function addBadgeAsset(raw) {
    if (!raw || typeof raw !== 'object') return;
    const setId = String(raw.set_id || '').trim();
    const id = String(raw.id || '').trim();
    if (!badgePartPattern.test(setId) || !badgePartPattern.test(id)) return;
    const image1x = normalizeBadgeUrl(raw.image_url_1x);
    const image2x = normalizeBadgeUrl(raw.image_url_2x);
    const image4x = normalizeBadgeUrl(raw.image_url_4x);
    if (!image1x && !image2x && !image4x) return;
    badgeMap.set(badgeKey(setId, id), {
      set_id: setId,
      id,
      image_url_1x: image1x || image2x || image4x,
      image_url_2x: image2x,
      image_url_4x: image4x,
      title: String(raw.title || raw.description || '').slice(0, 120)
    });
  }

  function addHelixBadgePayload(payload) {
    if (!payload || !Array.isArray(payload.badges)) return;
    payload.badges.forEach(addBadgeAsset);
  }

  function addPublicBadgePayload(payload) {
    const sets = payload && payload.badge_sets;
    if (!sets || typeof sets !== 'object') return;
    Object.entries(sets).forEach(([setId, setData]) => {
      const versions = setData && setData.versions;
      if (!versions || typeof versions !== 'object') return;
      Object.entries(versions).forEach(([id, version]) => {
        addBadgeAsset({
          set_id: setId,
          id,
          image_url_1x: version && version.image_url_1x,
          image_url_2x: version && version.image_url_2x,
          image_url_4x: version && version.image_url_4x,
          title: version && version.title,
          description: version && version.description
        });
      });
    });
  }

  function badgeAssetFor(badge) {
    const exact = badgeMap.get(badgeKey(badge.set_id, badge.id));
    if (exact) return exact;
    const setId = String(badge.set_id || '').trim();
    const idNumber = Number(badge.id);
    const candidates = [];
    badgeMap.forEach((asset, key) => {
      if (!key.startsWith(`${setId}/`)) return;
      const value = Number(asset.id);
      if (Number.isFinite(idNumber) && Number.isFinite(value) && value <= idNumber) {
        candidates.push(asset);
      }
    });
    if (candidates.length) {
      candidates.sort((a, b) => Number(b.id) - Number(a.id));
      return candidates[0];
    }
    return badgeMap.get(badgeKey(setId, '1')) || badgeMap.get(badgeKey(setId, '0')) || null;
  }

  function renderBadgeImage(asset, badge) {
    const title = escapeHtml(asset.title || badgeTitle(badge));
    const src = escapeHtml(asset.image_url_1x || asset.image_url_2x || asset.image_url_4x);
    const srcSet = [
      asset.image_url_1x ? `${escapeHtml(asset.image_url_1x)} 1x` : '',
      asset.image_url_2x ? `${escapeHtml(asset.image_url_2x)} 2x` : '',
      asset.image_url_4x ? `${escapeHtml(asset.image_url_4x)} 4x` : ''
    ].filter(Boolean).join(', ');
    const srcSetAttr = srcSet ? ` srcset="${srcSet}"` : '';
    return `<img class="chat-badge-img" src="${src}"${srcSetAttr} alt="${title}" title="${title}" loading="lazy" decoding="async">`;
  }

  function renderBadgeFallbackIcon(badge) {
    const title = escapeHtml(badgeTitle(badge));
    return `<svg class="chat-badge-fallback-icon" viewBox="0 0 24 24" role="img" aria-label="${title}" focusable="false">${badgeFallbackIconPath(badge.set_id)}</svg>`;
  }

  function normalizeBadges(badges) {
    if (!Array.isArray(badges)) return [];
    const clean = [];
    badges.slice(0, 24).forEach(raw => {
      const setId = String(raw && raw.set_id || '').trim();
      const id = String(raw && raw.id || '').trim();
      const info = String(raw && raw.info || '').trim();
      if (!badgePartPattern.test(setId) || !badgePartPattern.test(id)) return;
      if (info && !badgePartPattern.test(info)) return;
      clean.push({set_id: setId, id, info});
    });
    return clean;
  }

  function renderBadgesHtml(item) {
    const data = item && typeof item === 'object' ? item : {};
    return normalizeBadges(data.badges).map(badge => {
      const setId = normalizeBadgeSet(badge.set_id);
      const title = escapeHtml(badgeTitle(badge));
      const asset = badgeAssetFor(badge);
      const content = asset ? renderBadgeImage(asset, badge) : renderBadgeFallbackIcon(badge);
      const imageClass = asset ? ' has-badge-image' : ' has-badge-fallback-icon';
      return `<span class="chat-badge chat-badge-${escapeHtml(setId)}${imageClass}" data-badge="${escapeHtml(setId)}" title="${title}" aria-label="${title}">${content}</span>`;
    }).join('');
  }

  function emoteImg(src, alt, extraClass) {
    const safeSrc = normalizeUrl(src);
    if (!safeSrc) return escapeHtml(alt);
    const safeAlt = escapeHtml(alt || '');
    const klass = extraClass ? `chat-emote ${extraClass}` : 'chat-emote';
    return `<img class="${klass}" src="${escapeHtml(safeSrc)}" alt="${safeAlt}" title="${safeAlt}" loading="lazy" decoding="async">`;
  }

  function addNamedEmote(code, url) {
    const safeUrl = normalizeUrl(url);
    if (code && safeUrl) emoteMap.set(String(code), safeUrl);
  }

  function addTwitchGlobalFallbacks() {
    [
      ['Kappa', '25'], ['PogChamp', '305954156'], ['Kreygasm', '41'], ['LUL', '425618'],
      ['BibleThump', '86'], ['ResidentSleeper', '245'], ['4Head', '354'], ['FailFish', '360'],
      ['Jebaited', '114836'], ['MrDestructoid', '28'], ['NotLikeThis', '58765'], ['WutFace', '28087'],
      ['VoHiYo', '81274'], ['PJSalt', '36'], ['DansGame', '33']
    ].forEach(([code, id]) => addNamedEmote(code, twitchEmoteUrl(id)));
  }

  async function fetchJson(url, options) {
    const opts = options && typeof options === 'object' ? options : {};
    const timeoutMs = Number(opts.timeoutMs);
    const controller = (Number.isFinite(timeoutMs) && timeoutMs > 0 && typeof AbortController !== 'undefined')
      ? new AbortController()
      : null;
    let timer = null;
    if (controller) {
      timer = setTimeout(() => {
        try { controller.abort(); } catch (e) {}
      }, timeoutMs);
    }
    try {
      const response = await fetch(url, {
        cache: opts.cache || 'force-cache',
        credentials: opts.credentials || 'same-origin',
        signal: controller ? controller.signal : undefined
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return await response.json();
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  function seedCoreTwitchBadges() {
    // Official Twitch global badge assets (static CDN). Ensures originals even if
    // /chat/badges is empty, slow, or cached from an older unavailable response.
    const verified = {
      'moderator/1': '3267646d-33f0-4b17-b3df-f923a41db1d0',
      'broadcaster/1': '5527c58c-fb7d-422d-b71b-f309dcb85cc1',
      'vip/1': 'b817aba4-fad8-49e2-b88a-7cc744dfa6ec',
      'founder/0': '511b78a9-ab37-472f-9569-457753bbe7d3',
      'premium/1': 'bbbe0db0-a598-423e-86d0-f9fb98ca1933',
      'turbo/1': 'bd444ec6-8f34-4bf9-91f4-af1e3428d80f',
      'partner/1': 'd12a2e27-16f6-41d0-ab77-b780518f00a3',
      'staff/1': 'd97c37bd-a6f5-4c38-8f57-4e4bef88af34',
      'admin/1': '9ef7e029-4cdf-4d4d-a0d5-e2b3fb2583fe',
      'subscriber/0': '5d9f2208-5dd8-11e7-8513-2ff4adfae661',
      'subscriber/1': '5d9f2208-5dd8-11e7-8513-2ff4adfae661',
      'subscriber/2': '25a03e36-2bb2-4625-bd37-d6d9d406238d',
      'subscriber/3': 'e8984705-d091-4e54-8241-e53b30a84b0e',
      'subscriber/4': '2d2485f6-d19b-4daa-8393-9493b019156b',
      'subscriber/5': 'b4e6b13a-a76f-4c56-87e1-9375a7aaa610',
      'subscriber/6': 'ed51a614-2c44-4a60-80b6-62908436b43a',
      'bits/1': '73b5c3fb-24f9-4a82-a852-2f475b59411c',
      'bits/100': '09d93036-e7ce-431c-9a9e-7044297133f2',
      'bits/1000': '0d85a29e-79ad-4c63-a285-3acd2c66f2ba',
      'bits/5000': '57cd97fc-3e9e-4c6d-9d41-60147137234e',
      'bits/10000': '68af213b-a771-4124-b6e3-9bb6d98aa732',
      'sub-gifter/1': 'a5ef6c17-2e5b-4d8f-9b80-2779fd722414',
      'sub-gifter/5': 'ee113e59-c839-4472-969a-1e16d20f3962',
      'sub-gifter/10': 'd333288c-65d7-4c7b-b691-cdd7b3484bf8',
      'sub-gifter/25': '052a5d41-f1cc-455c-bc7b-fe841ffaf17f',
      'sub-gifter/50': 'c4a29737-e8a5-4420-917a-314a447f083e',
      'sub-gifter/100': '8343ada7-3451-434e-91c4-e82bdcf54460'
    };
    Object.entries(verified).forEach(([key, uuid]) => {
      const [setId, id] = key.split('/');
      const base = `https://static-cdn.jtvnw.net/badges/v1/${uuid}`;
      addBadgeAsset({
        set_id: setId,
        id,
        title: badgeTitles[normalizeBadgeSet(setId)] || setId,
        image_url_1x: `${base}/1`,
        image_url_2x: `${base}/2`,
        image_url_4x: `${base}/3`
      });
    });
  }

  async function loadGlobalEmotes() {
    const remoteOpts = { timeoutMs: 2500, credentials: 'omit' };
    await Promise.allSettled([
      fetchJson('https://api.betterttv.net/3/cached/emotes/global', remoteOpts).then(data => {
        if (Array.isArray(data)) data.forEach(e => addNamedEmote(e.code, `https://cdn.betterttv.net/emote/${e.id}/1x`));
      }),
      fetchJson('https://api.frankerfacez.com/v1/set/global', remoteOpts).then(data => {
        if (!data || !data.sets) return;
        Object.values(data.sets).forEach(set => {
          if (Array.isArray(set.emoticons)) set.emoticons.forEach(e => addNamedEmote(e.name, e.urls && e.urls['1']));
        });
      }),
      fetchJson('https://7tv.io/v3/emote-sets/global', remoteOpts).then(data => {
        if (Array.isArray(data && data.emotes)) {
          data.emotes.forEach(e => addNamedEmote(e.name, `https://cdn.7tv.app/emote/${e.id}/1x.webp`));
        }
      })
    ]);
  }

  async function loadChannelEmotes(channel) {
    const safeChannel = encodeURIComponent(String(channel || '').trim());
    if (!safeChannel) return;
    const remoteOpts = { timeoutMs: 2500, credentials: 'omit' };

    let subemotesOk = false;
    await Promise.allSettled([
      fetchJson(`https://api.ivr.fi/v2/twitch/subemotes/${safeChannel}`, remoteOpts).then(data => {
        if (Array.isArray(data) && data.length > 0) {
          subemotesOk = true;
          data.forEach(e => addNamedEmote(e.code, twitchEmoteUrl(e.id)));
        }
      })
    ]);
    if (!subemotesOk) return;

    let userId = '';
    try {
      const data = await fetchJson(`https://api.ivr.fi/v2/twitch/user?login=${safeChannel}`, remoteOpts);
      userId = data && data[0] && data[0].id ? String(data[0].id) : '';
    } catch (e) {
      return;
    }
    if (!userId) return;

    const safeUserId = encodeURIComponent(userId);
    await Promise.allSettled([
      fetchJson(`https://api.betterttv.net/3/cached/users/twitch/${safeUserId}`, remoteOpts).then(data => {
        if (Array.isArray(data && data.channelEmotes)) {
          data.channelEmotes.forEach(e => addNamedEmote(e.code, `https://cdn.betterttv.net/emote/${e.id}/1x`));
        }
        if (Array.isArray(data && data.sharedEmotes)) {
          data.sharedEmotes.forEach(e => addNamedEmote(e.code, `https://cdn.betterttv.net/emote/${e.id}/1x`));
        }
      }),
      fetchJson(`https://api.frankerfacez.com/v1/room/id/${safeUserId}`, remoteOpts).then(data => {
        if (!data || !data.sets) return;
        Object.values(data.sets).forEach(set => {
          if (Array.isArray(set.emoticons)) set.emoticons.forEach(e => addNamedEmote(e.name, e.urls && e.urls['1']));
        });
      }),
      fetchJson(`https://7tv.io/v3/users/twitch/${safeUserId}`, remoteOpts).then(data => {
        const emotes = data && data.emote_set && data.emote_set.emotes;
        if (Array.isArray(emotes)) {
          emotes.forEach(e => addNamedEmote(e.name, `https://cdn.7tv.app/emote/${e.id}/1x.webp`));
        }
      })
    ]);
  }

  function loadEmotes(channel) {
    const key = String(channel || '').trim().toLowerCase();
    if (loadCache.has(key)) return loadCache.get(key);
    addTwitchGlobalFallbacks();
    const promise = Promise.allSettled([loadGlobalEmotes(), loadChannelEmotes(channel)]).then(() => emoteMap);
    loadCache.set(key, promise);
    return promise;
  }

  function loadBadges(channel) {
    const key = String(channel || '').trim().toLowerCase();
    if (badgeLoadCache.has(key)) return badgeLoadCache.get(key);
    seedCoreTwitchBadges();
    const safeChannel = encodeURIComponent(String(channel || '').trim());
    const promise = Promise.allSettled([
      safeChannel
        ? fetchJson(`/chat/badges/${safeChannel}`, {cache: 'no-cache', timeoutMs: 3000}).then((payload) => {
            if (payload && Array.isArray(payload.badges) && payload.badges.length) {
              addHelixBadgePayload(payload);
            }
          }).catch(() => {})
        : Promise.resolve(),
      // Also try anonymous global warm endpoint if present.
      fetchJson('/chat/badges/_global', {cache: 'no-cache', timeoutMs: 3000}).then((payload) => {
        if (payload && Array.isArray(payload.badges) && payload.badges.length) {
          addHelixBadgePayload(payload);
        }
      }).catch(() => {})
    ]).then(() => badgeMap);
    badgeLoadCache.set(key, promise);
    return promise;
  }

  function renderTextSegment(text) {
    return String(text || '').split(/(\s+)/u).map(part => {
      if (!part) return '';
      if (/^\s+$/u.test(part)) return escapeHtml(part);
      const mapped = emoteMap.get(part);
      if (mapped) return emoteImg(mapped, part, 'chat-emote-provider');
      return escapeHtml(part);
    }).join('');
  }

  function rangeText(text, start, end, mode, chars) {
    if (mode === 'codeunit') return text.slice(start, end + 1);
    return chars.slice(start, end + 1).join('');
  }

  function scoreRangeMode(text, emotes, mode, chars) {
    const length = mode === 'codeunit' ? text.length : chars.length;
    let score = 0;
    emotes.forEach(emote => {
      if (emote.end >= length) {
        score += 20;
        return;
      }
      const label = rangeText(text, emote.start, emote.end, mode, chars);
      if (!label) score += 10;
      if (/\s/u.test(label)) score += 4;
      if (label.length > 80) score += 2;
    });
    return score;
  }

  function normalizeNativeEmotes(emotes, text) {
    if (!Array.isArray(emotes) || !emotes.length) return null;
    const parsed = [];
    emotes.forEach(raw => {
      const id = String(raw && raw.id || '').trim();
      const start = Number(raw && raw.start);
      const end = Number(raw && raw.end);
      if (!nativeIdPattern.test(id) || !Number.isInteger(start) || !Number.isInteger(end)) return;
      if (start < 0 || end < start || end - start > 128) return;
      parsed.push({id, start, end});
    });
    if (!parsed.length) return null;

    const chars = Array.from(text);
    const codePointScore = scoreRangeMode(text, parsed, 'codepoint', chars);
    const codeUnitScore = scoreRangeMode(text, parsed, 'codeunit', chars);
    const mode = codeUnitScore < codePointScore ? 'codeunit' : 'codepoint';
    const length = mode === 'codeunit' ? text.length : chars.length;
    const clean = [];
    let cursor = 0;
    parsed.sort((a, b) => a.start - b.start || a.end - b.end).forEach(emote => {
      if (emote.start < cursor || emote.end >= length) return;
      clean.push(emote);
      cursor = emote.end + 1;
    });
    return clean.length ? {mode, chars, emotes: clean} : null;
  }

  function renderRedeemHtml(item) {
    const reward = item && typeof item === 'object' ? item.reward : null;
    if (!reward || typeof reward !== 'object') return '';
    const title = String(reward.title || 'Channel Points').trim() || 'Channel Points';
    const cost = Number(reward.cost);
    let html = '<span class="chat-redeem-badge">' + escapeHtml(svT('Points')) + '</span> ';
    const redeemed = escapeHtml(svT('Redeemed “%(title)s”')).replace('%(title)s', () => escapeHtml(title));
    html += `<span class="chat-redeem-title">${redeemed}</span>`;
    if (Number.isFinite(cost) && cost > 0) {
      html += ` <span class="chat-redeem-cost">${escapeHtml(String(cost))}</span>`;
    }
    return html;
  }

  function renderModerationHtml(item) {
    const data = item && typeof item === 'object' ? item : null;
    const event = data && data.event && typeof data.event === 'object' ? data.event : null;
    if (!event) return '';
    const type = String(event.type || '').trim().toLowerCase();
    const labels = {
      timeout: 'Timeout',
      ban: 'Ban',
      delete: 'Delete',
      clear: 'Clear'
    };
    const label = labels[type] || 'Mod';
    const text = String(data.msg || '').trim();
    return (
      `<span class="chat-mod-badge chat-mod-badge-${escapeHtml(type)}">${escapeHtml(label)}</span>` +
      (text ? ` <span class="chat-mod-text">${escapeHtml(text)}</span>` : '')
    );
  }

  function renderBodyHtml(item) {
    const data = item && typeof item === 'object' ? item : {msg: item};
    const text = String(data.msg || '');
    if (!text) return '';
    const native = normalizeNativeEmotes(data.emotes, text);
    if (!native) return renderTextSegment(text);

    let html = '';
    let cursor = 0;
    native.emotes.forEach(emote => {
      const before = native.mode === 'codeunit'
        ? text.slice(cursor, emote.start)
        : native.chars.slice(cursor, emote.start).join('');
      html += renderTextSegment(before);
      const label = rangeText(text, emote.start, emote.end, native.mode, native.chars);
      html += emoteImg(twitchEmoteUrl(emote.id), label, 'chat-emote-native');
      cursor = emote.end + 1;
    });
    const rest = native.mode === 'codeunit'
      ? text.slice(cursor)
      : native.chars.slice(cursor).join('');
    return html + renderTextSegment(rest);
  }

  function chatLineClassName(item) {
    const data = item && typeof item === 'object' ? item : null;
    if (data && data.event && data.event.type) {
      const type = String(data.event.type || '').replace(/[^a-z0-9_-]/gi, '') || 'mod';
      return `chat-line chat-line-mod chat-line-mod-${type}`;
    }
    if (data && data.reward) return 'chat-line chat-line-redeem';
    return 'chat-line';
  }

  function renderMessageHtml(item) {
    const moderation = renderModerationHtml(item);
    if (moderation) return moderation;
    const redeem = renderRedeemHtml(item);
    const body = renderBodyHtml(item);
    if (redeem && body) {
      return `${redeem}<span class="chat-redeem-sep"> · </span><span class="chat-redeem-msg">${body}</span>`;
    }
    return redeem || body;
  }

  // Seed immediately so first paint already uses official Twitch icons.
  seedCoreTwitchBadges();

  window.StreamVaultChat = {
    escapeHtml,
    loadBadges,
    loadEmotes,
    renderBadgesHtml,
    userColor,
    chatLineClassName,
    renderRedeemHtml,
    renderModerationHtml,
    renderMessageHtml
  };
}());
