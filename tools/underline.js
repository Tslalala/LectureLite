(function () {
  "use strict";
  window.LectureLiteToolRegistry.register({
    type: "underline",
    label: "下划线",
    svg: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 4v6a6 6 0 0 0 12 0V4"/><path d="M4 20h16"/></svg>',
    order: 20,
    render: { kind: "rect", className: "annotation-underline" }
  });
})();
