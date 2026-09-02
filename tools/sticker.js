(function () {
  "use strict";
  window.LectureLiteToolRegistry.register({
    type: "sticker",
    label: "贴纸",
    order: 60,
    group: "more",
    input: "sticker",
    variants: [
      {
        id: "focus", label: "聚焦",
        svg: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 4H5a1 1 0 0 0-1 1v3M16 4h3a1 1 0 0 1 1 1v3M20 16v3a1 1 0 0 1-1 1h-3M8 20H5a1 1 0 0 1-1-1v-3"/><circle cx="12" cy="12" r="2.5"/></svg>'
      },
      {
        id: "check", label: "确认",
        svg: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8"/><path d="m8.5 12 2.3 2.3 4.9-5"/></svg>'
      },
      {
        id: "pin", label: "定位",
        svg: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 21s6-5.1 6-11a6 6 0 1 0-12 0c0 5.9 6 11 6 11Z"/><circle cx="12" cy="10" r="2"/></svg>'
      },
      {
        id: "bookmark", label: "收藏",
        svg: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 4.5A1.5 1.5 0 0 1 8.5 3h7A1.5 1.5 0 0 1 17 4.5V21l-5-3-5 3V4.5Z"/></svg>'
      },
      {
        id: "flag", label: "标记",
        svg: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 21V4m0 1h10l-1.6 3L16 11H6"/></svg>'
      },
      {
        id: "insight", label: "启发",
        svg: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v3M4.2 7.5l2.6 1.4M19.8 7.5l-2.6 1.4M8.5 17h7M9.5 20h5"/><path d="M8.2 14.5A5 5 0 1 1 15.8 14.5c-.8.7-1.1 1.2-1.2 2.5h-5.2c-.1-1.3-.4-1.8-1.2-2.5Z"/></svg>'
      }
    ],
    render: {
      kind: "sticker",
      anchorClass: "annotation-sticker-anchor"
    }
  });
})();
