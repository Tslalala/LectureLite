(function () {
  "use strict";
  window.LectureLiteToolRegistry.register({
    type: "highlight",
    label: "高亮",
    svg: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 11-6 6v3h9l3-3"/><path d="m22 12-4.6 4.6a2 2 0 0 1-2.8 0l-5.2-5.2a2 2 0 0 1 0-2.8L14 4"/></svg>',
    order: 10,
    render: { kind: "rect", className: "annotation-highlight" }
  });
})();
