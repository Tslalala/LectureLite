(function () {
  "use strict";
  window.LectureLiteToolRegistry.register({
    type: "question",
    label: "问号",
    order: 50,
    group: "more",
    render: {
      kind: "anchor",
      anchorClass: "annotation-question-anchor",
      text: () => "?"
    }
  });
})();
