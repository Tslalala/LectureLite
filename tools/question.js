(function () {
  "use strict";
  window.LectureLiteToolRegistry.register({
    type: "question",
    label: "问号",
    svg: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 2.6-3 2.6"/><path d="M12 17h.01"/></svg>',
    order: 50,
    group: "more",
    render: {
      kind: "anchor",
      anchorClass: "annotation-question-anchor",
      text: () => "?"
    }
  });
})();
