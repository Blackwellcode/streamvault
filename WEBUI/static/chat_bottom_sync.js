(function () {
  'use strict';

  function syncChatBottom() {
    if (window.innerWidth < 961) {
      var cp = document.querySelector('.chat-panel');
      if (cp) {
        cp.style.height = '';
        cp.style.maxHeight = '';
      }
      return;
    }

    var vd = document.querySelector('.vod-details');
    var cp = document.querySelector('.chat-panel');
    if (!vd || !cp) return;

    var vdRect = vd.getBoundingClientRect();
    var cpRect = cp.getBoundingClientRect();
    var targetHeight = Math.round(vdRect.bottom - cpRect.top);

    if (targetHeight > 100) {
      cp.style.setProperty('height', targetHeight + 'px', 'important');
      cp.style.setProperty('max-height', targetHeight + 'px', 'important');
    }
  }

  window.addEventListener('resize', syncChatBottom);

  document.addEventListener('DOMContentLoaded', function () {
    syncChatBottom();
    var vd = document.querySelector('.vod-details');
    var mc = document.querySelector('.main-col');
    if (window.ResizeObserver) {
      var ro = new ResizeObserver(function () {
        syncChatBottom();
      });
      if (vd) ro.observe(vd);
      if (mc) ro.observe(mc);
    }
  });

  window.addEventListener('load', syncChatBottom);

  if (document.readyState !== 'loading') {
    syncChatBottom();
  }
})();
