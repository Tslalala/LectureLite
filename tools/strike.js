(function () {
  "use strict";
  window.LectureLiteToolRegistry.register({
    type: "strike",
    label: "删除线",
    svg: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M16 4H9a3 3 0 0 0-2.83 4"/><path d="M14 12a4 4 0 0 1 0 8H6"/><path d="M4 12h16"/></svg>',
    order: 30,
    render: { kind: "rect", className: "annotation-strike" }
  });
})();
