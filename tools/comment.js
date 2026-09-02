(function () {
  "use strict";
  window.LectureLiteToolRegistry.register({
    type: "comment",
    label: "批注",
    order: 40,
    input: "comment",
    render: {
      kind: "comment",
      rangeClass: "annotation-comment-range"
    }
  });
})();
