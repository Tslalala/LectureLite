(function () {
  "use strict";

  const tools = [];

  window.LectureLiteToolRegistry = {
    register(tool) {
      if (!tool || !tool.type || tools.some(item => item.type === tool.type)) return;
      tools.push(Object.freeze({ ...tool }));
    },
    all() {
      return tools.slice().sort((a, b) => (a.order || 0) - (b.order || 0));
    },
    get(type) {
      return tools.find(item => item.type === type) || null;
    }
  };
})();
