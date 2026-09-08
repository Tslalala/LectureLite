(function () {
  "use strict";

  class AnnotationTools {
    constructor(options) {
      this.options = options;
      this.doc = options.doc;
      this.viewer = options.viewer;
      this.annotations = [];
      this.selection = null;
      this.renderKey = "";
      this.history = [];
      this.tools = window.LectureLiteToolRegistry ? window.LectureLiteToolRegistry.all() : [];
      this.manualOpenComments = new Set();
      this.timelineOpenComments = new Set();
      this.lockedComments = new Set();
      this.commentPopovers = new Map();
      this.commentCloseTimers = new Map();
      this.buildUi();
      this.bindEvents();
    }

    buildUi() {
      this.toolbar = document.createElement("div");
      this.toolbar.className = "annotation-toolbar";
      this.toolbar.setAttribute("role", "toolbar");
      this.toolbar.setAttribute("aria-label", "选中文字后添加标注");

      this.tools.filter(tool => tool.group !== "more").forEach(tool => {
        this.toolbar.appendChild(this.createToolButton(tool));
      });

      const moreTools = this.tools.filter(tool => tool.group === "more");
      if (moreTools.length) {
        this.moreWrap = document.createElement("div");
        this.moreWrap.className = "annotation-more";
        this.moreButton = document.createElement("button");
        this.moreButton.type = "button";
        this.moreButton.className = "annotation-tool-button annotation-more-toggle";
        this.moreButton.textContent = "更多";
        this.moreButton.setAttribute("aria-haspopup", "menu");
        this.moreButton.setAttribute("aria-expanded", "false");
        this.moreButton.addEventListener("pointerdown", event => event.preventDefault());
        this.moreButton.addEventListener("click", () => this.toggleMore());
        this.moreMenu = document.createElement("div");
        this.moreMenu.className = "annotation-more-menu";
        this.moreMenu.setAttribute("role", "menu");
        moreTools.forEach(tool => this.moreMenu.appendChild(this.createToolButton(tool, true)));
        this.moreWrap.append(this.moreButton, this.moreMenu);
        this.toolbar.appendChild(this.moreWrap);
      }

      this.composer = document.createElement("div");
      this.composer.className = "annotation-composer";
      this.composer.addEventListener("pointerdown", event => event.stopPropagation());
      document.body.append(this.toolbar, this.composer);
    }

    createToolButton(tool, inMenu) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "annotation-tool-button" + (inMenu ? " annotation-more-item" : "");
      button.dataset.tool = tool.type;
      if (tool.svg) {
        const icon = document.createElement("span");
        icon.className = "annotation-tool-icon";
        icon.innerHTML = tool.svg;
        button.appendChild(icon);
      } else if (tool.icon) {
        const icon = document.createElement("span");
        icon.className = "annotation-tool-icon annotation-tool-icon-" + tool.type;
        icon.textContent = tool.icon;
        button.appendChild(icon);
      }
      button.append(document.createTextNode(tool.label));
      button.title = tool.label;
      button.setAttribute("aria-label", tool.label);
      if (inMenu) button.setAttribute("role", "menuitem");
      button.addEventListener("pointerdown", event => event.preventDefault());
      button.addEventListener("click", () => {
        this.closeMore();
        this.chooseTool(tool);
      });
      return button;
    }

    toggleMore() {
      if (!this.moreMenu) return;
      const open = !this.moreMenu.classList.contains("show");
      this.moreMenu.classList.toggle("show", open);
      this.moreButton.setAttribute("aria-expanded", String(open));
    }

    closeMore() {
      if (!this.moreMenu) return;
      this.moreMenu.classList.remove("show");
      this.moreButton.setAttribute("aria-expanded", "false");
    }

    bindEvents() {
      this.onPointerUp = event => {
        if (event.button !== 0 || this.toolbar.contains(event.target) || this.composer.contains(event.target)) return;
        window.setTimeout(() => this.offerForSelection(event.clientX, event.clientY), 0);
      };
      this.onContextMenu = event => {
        const descriptor = this.readSelection();
        if (!descriptor) return;
        event.preventDefault();
        this.selection = descriptor;
        this.showToolbar(descriptor.rect, event.clientX, event.clientY);
      };
      this.onOutsidePointerDown = event => {
        // 点击批注气泡 / 波浪线以外的地方：收起已固定的气泡
        if (this.lockedComments.size) {
          const hit = event.target.closest && event.target.closest("[data-annotation-id]");
          const keep = hit ? hit.dataset.annotationId : null;
          Array.from(this.lockedComments).forEach(id => {
            if (id !== keep) {
              this.lockedComments.delete(id);
              this.setCommentOpen(id, false);
            }
          });
        }
        if (!this.toolbar.contains(event.target) && !this.composer.contains(event.target) && !this.doc.contains(event.target)) {
          this.hide();
        }
      };
      this.layoutFrame = 0;
      this.refreshLayout = () => {
        if (this.layoutFrame) return;
        this.layoutFrame = requestAnimationFrame(() => {
          this.layoutFrame = 0;
          this.renderKey = "";
          this.render(this.options.getPlaybackTime());
        });
      };
      this.onScroll = () => { this.hide(); this.refreshLayout(); };
      this.onResize = () => {
        this.hide();
        this.renderKey = "";
        this.render(this.options.getPlaybackTime());
      };

      this.doc.addEventListener("pointerup", this.onPointerUp);
      this.doc.addEventListener("contextmenu", this.onContextMenu);
      document.addEventListener("pointerdown", this.onOutsidePointerDown, true);
      this.viewer.addEventListener("scroll", this.onScroll, { passive: true });
      window.addEventListener("resize", this.onResize);
      this.resizeObserver = typeof ResizeObserver === "function" ? new ResizeObserver(this.refreshLayout) : null;
      this.resizeObserver?.observe(this.viewer);
      this.resizeObserver?.observe(this.doc);
    }

    offerForSelection(clientX, clientY) {
      const descriptor = this.readSelection();
      if (!descriptor) {
        this.hide();
        return;
      }
      this.selection = descriptor;
      this.showToolbar(descriptor.rect, clientX, clientY);
    }

    offerExternalSelection(descriptor, rect, clientX, clientY) {
      if (this.options.getMode() !== "rec" || !descriptor || !descriptor.quote
          || !Array.isArray(descriptor.coords) || !descriptor.coords.length || !rect) return;
      this.selection = {
        fi: this.options.getFileIndex(),
        sourceType: descriptor.sourceType || this.options.getSourceType(),
        quote: String(descriptor.quote).trim().slice(0, 500),
        coords: descriptor.coords.map(item => ({ ...item }))
      };
      if (!this.selection.quote) return;
      this.showToolbar(rect, clientX, clientY);
    }

    readSelection() {
      if (this.options.getMode() !== "rec") return null;
      const selection = window.getSelection();
      if (!selection || selection.isCollapsed || !selection.rangeCount) return null;
      const range = selection.getRangeAt(0);
      if (!this.doc.contains(range.commonAncestorContainer)) return null;
      const rects = Array.from(range.getClientRects()).filter(rect => rect.width > 0 && rect.height > 0);
      if (!rects.length) return null;

      const descriptor = {
        fi: this.options.getFileIndex(),
        sourceType: this.options.getSourceType(),
        quote: selection.toString().trim().slice(0, 500),
        rect: rects[0]
      };
      if (!descriptor.quote) return null;

      if (descriptor.sourceType === "md") {
        const offsets = this.options.rangeToOffsets(range);
        if (!offsets || offsets[1] <= offsets[0]) return null;
        descriptor.offsets = offsets;
      } else {
        descriptor.coords = this.pdfCoordinates(rects);
        if (!descriptor.coords.length) return null;
      }
      return descriptor;
    }

    pdfCoordinates(rects) {
      const pages = Array.from(this.doc.querySelectorAll(".pdf-page"));
      const result = [];
      rects.forEach(rect => {
        let bestPage = -1;
        let bestArea = 0;
        pages.forEach((page, index) => {
          const pageRect = page.getBoundingClientRect();
          const width = Math.max(0, Math.min(rect.right, pageRect.right) - Math.max(rect.left, pageRect.left));
          const height = Math.max(0, Math.min(rect.bottom, pageRect.bottom) - Math.max(rect.top, pageRect.top));
          const area = width * height;
          if (area > bestArea) {
            bestArea = area;
            bestPage = index;
          }
        });
        if (bestPage < 0) return;
        const pageRect = pages[bestPage].getBoundingClientRect();
        result.push({
          page: bestPage,
          x: this.round((rect.left - pageRect.left) / pageRect.width),
          y: this.round((rect.top - pageRect.top) / pageRect.height),
          w: this.round(rect.width / pageRect.width),
          h: this.round(rect.height / pageRect.height)
        });
      });
      return result;
    }

    round(value) {
      return Math.round(value * 10000) / 10000;
    }

    showToolbar(rect, clientX, clientY) {
      this.closeComposer();
      this.closeMore();
      this.toolbar.classList.add("show");
      this.toolbar.style.visibility = "hidden";
      const toolbarRect = this.toolbar.getBoundingClientRect();
      const preferredLeft = clientX == null ? rect.left + rect.width / 2 - toolbarRect.width / 2 : clientX;
      const preferredTop = clientY == null ? rect.top - toolbarRect.height - 9 : clientY - toolbarRect.height - 5;
      const left = Math.max(8, Math.min(preferredLeft, window.innerWidth - toolbarRect.width - 8));
      const top = preferredTop < 8 ? Math.min(window.innerHeight - toolbarRect.height - 8, rect.bottom + 9) : preferredTop;
      this.toolbar.style.left = left + "px";
      this.toolbar.style.top = Math.max(8, top) + "px";
      this.toolbar.style.visibility = "visible";
    }

    chooseTool(tool) {
      if (!this.selection) return;
      if (tool.input === "comment") {
        this.openCommentComposer(tool);
      } else if (tool.input === "sticker") {
        this.openStickerPicker(tool);
      } else {
        this.commit(tool, {});
      }
    }

    openCommentComposer(tool) {
      this.composer.innerHTML = "";
      const textarea = document.createElement("textarea");
      textarea.placeholder = "写下这段文字的批注…";
      const actions = document.createElement("div");
      actions.className = "annotation-composer-actions";
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.textContent = "取消";
      const save = document.createElement("button");
      save.type = "button";
      save.className = "primary";
      save.textContent = "添加批注";
      cancel.addEventListener("click", () => this.closeComposer());
      save.addEventListener("click", () => {
        const text = textarea.value.trim();
        if (!text) {
          textarea.focus();
          return;
        }
        this.commit(tool, { text });
      });
      textarea.addEventListener("keydown", event => {
        if ((event.metaKey || event.ctrlKey) && event.key === "Enter") save.click();
        if (event.key === "Escape") this.closeComposer();
      });
      actions.append(cancel, save);
      this.composer.append(textarea, actions);
      this.openComposer();
      textarea.focus();
    }

    openStickerPicker(tool) {
      this.composer.innerHTML = "";
      this.composer.classList.add("sticker-mode");
      const heading = document.createElement("div");
      heading.className = "annotation-sticker-heading";
      heading.textContent = "选择标记";
      const choices = document.createElement("div");
      choices.className = "annotation-stickers";
      (tool.variants || []).forEach(variant => {
        const button = document.createElement("button");
        button.type = "button";
        button.title = variant.label;
        button.innerHTML = variant.svg + '<span></span>';
        button.querySelector("span").textContent = variant.label;
        button.addEventListener("click", () => this.commit(tool, { icon: variant.id }));
        choices.appendChild(button);
      });
      this.composer.append(heading, choices);
      this.openComposer();
    }

    openComposer() {
      const toolbarRect = this.toolbar.getBoundingClientRect();
      this.closeMore();
      this.toolbar.classList.remove("show");
      this.composer.classList.add("show");
      const composerRect = this.composer.getBoundingClientRect();
      const left = Math.max(8, Math.min(toolbarRect.left, window.innerWidth - composerRect.width - 8));
      const below = toolbarRect.bottom + 6;
      const top = below + composerRect.height <= window.innerHeight - 8
        ? below
        : Math.max(8, toolbarRect.top - composerRect.height - 6);
      this.composer.style.left = left + "px";
      this.composer.style.top = top + "px";
    }

    closeComposer() {
      this.composer.classList.remove("show");
      this.composer.classList.remove("sticker-mode");
      this.composer.innerHTML = "";
    }

    commit(tool, payload) {
      const selection = this.selection;
      if (!selection) return;
      const annotation = {
        id: "ann-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 7),
        t: Math.max(0, Math.round(this.options.getTimestamp() || 0)),
        type: tool.type,
        fi: selection.fi,
        quote: selection.quote
      };
      if (selection.offsets) annotation.offsets = selection.offsets.slice();
      if (selection.coords) annotation.coords = selection.coords.map(item => ({ ...item }));
      Object.assign(annotation, payload);
      this.add(annotation);
      this.selection = null;
      this.hide();
      const nativeSelection = window.getSelection();
      if (nativeSelection) nativeSelection.removeAllRanges();
    }

    // 供未来 AI 讲解调用：传入 type + fi + offsets/coords 即可复用同一时间轴。
    add(annotation) {
      if (!annotation || !annotation.type) return null;
      const tool = window.LectureLiteToolRegistry.get(annotation.type);
      if (!tool) throw new Error("未知标注工具: " + annotation.type);
      const item = {
        ...annotation,
        id: annotation.id || "ann-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 7),
        t: Math.max(0, Math.round(annotation.t == null ? this.options.getTimestamp() || 0 : annotation.t)),
        fi: annotation.fi == null ? this.options.getFileIndex() : annotation.fi
      };
      this.options.onCreate(item, tool);
      this.history.push({ kind: "add", id: item.id });
      return item;
    }

    removeAnnotation(id) {
      const annotation = this.annotations.find(item => item.id === id);
      if (!annotation || annotation.removedAt != null) return false;
      const previousRemovedAt = annotation.removedAt;
      annotation.removedAt = Math.max(0, Math.round(this.options.getTimestamp() || 0));
      this.manualOpenComments.delete(id);
      this.lockedComments.delete(id);
      this.history.push({ kind: "remove", id, previousRemovedAt });
      this.options.onUpdate && this.options.onUpdate(annotation, "remove");
      return true;
    }

    undo() {
      const action = this.history && this.history.pop();
      if (!action) return false;
      const annotation = this.annotations.find(item => item.id === action.id);
      if (!annotation) return false;
      if (action.kind === "add") {
        annotation.removedAt = 0;
        this.options.onUpdate && this.options.onUpdate(annotation, "remove");
      } else if (action.kind === "remove") {
        if (action.previousRemovedAt == null) delete annotation.removedAt;
        else annotation.removedAt = action.previousRemovedAt;
        this.options.onUpdate && this.options.onUpdate(annotation, "restore");
      }
      return true;
    }

    hide() {
      this.toolbar.classList.remove("show");
      this.closeMore();
      this.closeComposer();
    }

    closeAllComments(recordEvents = true) {
      this.lockedComments.clear();
      this.commentCloseTimers.forEach(timer => window.clearTimeout(timer));
      this.commentCloseTimers.clear();
      Array.from(this.manualOpenComments).forEach(id => this.setCommentOpen(id, false, recordEvents));
      this.timelineOpenComments.clear();
      this.updateCommentPopovers();
    }

    setAnnotations(annotations) {
      const nextAnnotations = Array.isArray(annotations) ? annotations : [];
      if (this.annotations !== nextAnnotations) this.history = [];
      this.annotations = nextAnnotations;
      this.annotations.forEach(annotation => {
        if (!annotation.id) annotation.id = "ann-legacy-" + Math.random().toString(36).slice(2, 9);
      });
      this.renderKey = "";
    }

    render(time) {
      const fi = this.options.getFileIndex();
      const sourceType = this.options.getSourceType();
      // 播放模式标注只读：不允许悬停删除 / 打开编辑入口
      const readonly = this.options.getMode() === "play";
      const visible = this.annotations.filter(item => item.fi === fi
        && (time == null || item.t <= time)
        && (item.removedAt == null || (time != null && time < item.removedAt)));
      const htmlSlide = sourceType === "html" && this.options.getHtmlSlide
        ? this.options.getHtmlSlide()
        : -1;
      const key = (readonly ? "play" : "rec") + "|" + fi + "|" + sourceType + "|"
        + htmlSlide + "|"
        + (time == null ? "all" : visible.map(item => item.id || [item.t, item.type].join("-")).join(","));
      if (key === this.renderKey && this.layer && this.doc.contains(this.layer)) return;
      this.renderKey = key;
      this.ensureLayer();
      this.layer.classList.toggle("readonly", readonly);
      this.layer.innerHTML = "";
      this.commentPopovers.clear();
      visible.forEach(annotation => this.renderAnnotation(annotation, sourceType));
      this.updateCommentPopovers();
    }

    ensureLayer() {
      if (this.layer && this.doc.contains(this.layer)) return;
      this.layer = document.createElement("div");
      this.layer.className = "annotation-layer";
      this.doc.appendChild(this.layer);
    }

    renderAnnotation(annotation, sourceType) {
      const rects = sourceType === "md"
        ? this.markdownRects(annotation)
        : sourceType === "html"
          ? this.htmlRects(annotation)
          : this.pdfRects(annotation);
      if (!rects.length) return;
      const tool = window.LectureLiteToolRegistry.get(annotation.type);
      const renderer = tool && tool.render;
      if (!renderer) return;
      if (renderer.kind === "rect") {
        rects.forEach(rect => this.addRect(rect, renderer.className, annotation));
        return;
      }
      if (renderer.kind === "comment") {
        this.addComment(rects, annotation, renderer);
        return;
      }
      if (renderer.kind === "sticker") {
        this.addStickerAnchor(rects[rects.length - 1], annotation, tool, renderer);
        return;
      }
      if (renderer.rangeClass) rects.forEach(rect => this.addRect(rect, renderer.rangeClass, annotation));
      const text = typeof renderer.text === "function" ? renderer.text(annotation) : renderer.text;
      this.addAnchor(rects[rects.length - 1], text || tool.label, renderer.anchorClass || "", annotation);
    }

    addComment(rects, annotation, renderer) {
      const id = annotation.id || [annotation.fi, annotation.t, annotation.offsets || "pdf"].join("-");
      rects.forEach(rect => {
        const wave = document.createElement("span");
        wave.className = "annotation-mark " + renderer.rangeClass;
        wave.style.left = rect.left + "px";
        wave.style.top = rect.top + rect.height - 5 + "px";
        wave.style.width = rect.width + "px";
        wave.style.height = "8px";
        wave.dataset.annotationId = id;
        wave.tabIndex = 0;
        wave.setAttribute("role", "button");
        wave.setAttribute("aria-label", "查看批注：" + (annotation.text || ""));
        wave.addEventListener("mouseenter", () => this.setCommentOpen(id, true));
        wave.addEventListener("mouseleave", () => this.scheduleCommentClose(id));
        wave.addEventListener("focus", () => this.setCommentOpen(id, true));
        wave.addEventListener("blur", () => this.scheduleCommentClose(id));
        // 点击 = 固定显示 / 收起批注内容；删除入口在气泡内
        wave.addEventListener("click", event => {
          event.stopPropagation();
          if (this.lockedComments.has(id)) {
            this.lockedComments.delete(id);
            this.setCommentOpen(id, false);
          } else {
            this.lockedComments.add(id);
            this.setCommentOpen(id, true);
          }
        });
        this.layer.appendChild(wave);
      });

      const last = rects[rects.length - 1];
      const popover = document.createElement("div");
      popover.className = "annotation-comment-popover";
      popover.dataset.annotationId = id;
      popover.style.left = Math.max(8, Math.min(last.left, this.doc.clientWidth - 300)) + "px";
      popover.style.top = last.top + last.height + 9 + "px";
      const body = document.createElement("div");
      body.className = "annotation-popover-text";
      body.textContent = annotation.text || "批注";
      popover.appendChild(body);
      if (this.options.getMode() !== "play") {
        const foot = document.createElement("div");
        foot.className = "annotation-popover-foot";
        const del = document.createElement("button");
        del.type = "button";
        del.textContent = "删除批注";
        del.addEventListener("click", event => {
          event.stopPropagation();
          this.removeAnnotation(id);
        });
        foot.appendChild(del);
        popover.appendChild(foot);
      }
      popover.addEventListener("mouseenter", () => this.setCommentOpen(id, true));
      popover.addEventListener("mouseleave", () => this.scheduleCommentClose(id));
      popover.addEventListener("click", event => {
        event.stopPropagation();
        this.lockedComments.add(id);
        this.setCommentOpen(id, true);
      });
      this.layer.appendChild(popover);
      this.commentPopovers.set(id, popover);
    }

    setCommentOpen(id, open, recordEvent = true) {
      const timer = this.commentCloseTimers.get(id);
      if (timer) {
        window.clearTimeout(timer);
        this.commentCloseTimers.delete(id);
      }
      const wasOpen = this.manualOpenComments.has(id);
      if (open) this.manualOpenComments.add(id);
      else this.manualOpenComments.delete(id);
      this.updateCommentPopovers();
      if (recordEvent && wasOpen !== open && this.options.onPopupChange) {
        this.options.onPopupChange(id, open);
      }
    }

    scheduleCommentClose(id) {
      if (this.lockedComments.has(id)) return;
      const oldTimer = this.commentCloseTimers.get(id);
      if (oldTimer) window.clearTimeout(oldTimer);
      this.commentCloseTimers.set(id, window.setTimeout(() => {
        this.commentCloseTimers.delete(id);
        if (!this.lockedComments.has(id)) this.setCommentOpen(id, false);
      }, 140));
    }

    applyPopupTimeline(events, time) {
      const state = new Map();
      (events || []).forEach(event => {
        if (event.t <= time) state.set(event.id, !!event.open);
      });
      this.timelineOpenComments = new Set(
        Array.from(state.entries()).filter(([, open]) => open).map(([id]) => id)
      );
      this.updateCommentPopovers();
    }

    updateCommentPopovers() {
      this.commentPopovers.forEach((popover, id) => {
        popover.classList.toggle("show", this.manualOpenComments.has(id) || this.timelineOpenComments.has(id));
      });
    }

    addStickerAnchor(rect, annotation, tool, renderer) {
      const variant = (tool.variants || []).find(item => item.id === annotation.icon)
        || (tool.variants || [])[0];
      const wrap = document.createElement("div");
      wrap.className = "annotation-item";
      wrap.style.left = rect.left + rect.width + "px";
      wrap.style.top = rect.top + rect.height / 2 + "px";
      const anchor = document.createElement("div");
      anchor.className = "annotation-anchor " + (renderer.anchorClass || "");
      if (variant) anchor.innerHTML = variant.svg;
      anchor.title = variant ? variant.label : "标记";
      wrap.appendChild(anchor);
      wrap.appendChild(this.createDeleteChip(annotation));
      this.layer.appendChild(wrap);
    }

    markdownRects(annotation) {
      if (!annotation.offsets || annotation.offsets.length < 2) return [];
      const range = this.options.offsetsToRange(annotation.offsets[0], annotation.offsets[1]);
      if (!range) return [];
      const docRect = this.doc.getBoundingClientRect();
      return Array.from(range.getClientRects())
        .filter(rect => rect.width > 0 && rect.height > 0)
        .map(rect => ({
          left: rect.left - docRect.left,
          top: rect.top - docRect.top,
          width: rect.width,
          height: rect.height
        }));
    }

    pdfRects(annotation) {
      if (!Array.isArray(annotation.coords)) return [];
      const docRect = this.doc.getBoundingClientRect();
      const pages = Array.from(this.doc.querySelectorAll(".pdf-page"));
      return annotation.coords.map(coord => {
        const page = pages[coord.page];
        if (!page) return null;
        const pageRect = page.getBoundingClientRect();
        return {
          left: pageRect.left - docRect.left + coord.x * pageRect.width,
          top: pageRect.top - docRect.top + coord.y * pageRect.height,
          width: coord.w * pageRect.width,
          height: coord.h * pageRect.height
        };
      }).filter(Boolean);
    }

    htmlRects(annotation) {
      if (!Array.isArray(annotation.coords)) return [];
      const frame = this.doc.querySelector("iframe.html-presentation");
      if (!frame) return [];
      const docRect = this.doc.getBoundingClientRect();
      const frameRect = frame.getBoundingClientRect();
      const currentSlide = this.options.getHtmlSlide ? this.options.getHtmlSlide() : -1;
      const state = this.options.getHtmlState ? this.options.getHtmlState() : null;
      return annotation.coords.filter(coord => coord.slide == null || coord.slide === currentSlide).map(coord => {
        const currentX = Number(state?.x) || 0, currentY = Number(state?.y) || 0;
        const currentW = Number(state?.vw) || Number(coord.vw) || frameRect.width;
        const currentH = Number(state?.vh) || Number(coord.vh) || frameRect.height;
        const recordedX = Number(coord.sx) || 0, recordedY = Number(coord.sy) || 0;
        const recordedW = Number(coord.vw) || currentW, recordedH = Number(coord.vh) || currentH;
        const x = Number.isFinite(coord.sx) ? (coord.x * recordedW + recordedX - currentX) / currentW : coord.x;
        const y = Number.isFinite(coord.sy) ? (coord.y * recordedH + recordedY - currentY) / currentH : coord.y;
        return {
          left: frameRect.left - docRect.left + x * frameRect.width,
          top: frameRect.top - docRect.top + y * frameRect.height,
          width: coord.w * recordedW / currentW * frameRect.width,
          height: coord.h * recordedH / currentH * frameRect.height
        };
      });
    }

    // 悬停标注才出现的删除小按钮（×），替代原来"点击即删"的隐式逻辑
    createDeleteChip(annotation) {
      const tool = window.LectureLiteToolRegistry.get(annotation.type);
      const label = (tool && tool.label) || "标注";
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "annotation-del";
      chip.textContent = "×";
      chip.title = "删除" + label;
      chip.setAttribute("aria-label", "删除" + label);
      chip.addEventListener("pointerdown", event => event.stopPropagation());
      chip.addEventListener("click", event => {
        event.stopPropagation();
        this.removeAnnotation(annotation.id);
      });
      return chip;
    }

    addRect(rect, className, annotation) {
      const wrap = document.createElement("div");
      wrap.className = "annotation-item";
      wrap.style.left = rect.left + "px";
      wrap.style.top = rect.top + "px";
      wrap.style.width = rect.width + "px";
      wrap.style.height = rect.height + "px";
      const mark = document.createElement("div");
      mark.className = "annotation-mark " + className;
      wrap.appendChild(mark);
      wrap.appendChild(this.createDeleteChip(annotation));
      this.layer.appendChild(wrap);
    }

    addAnchor(rect, text, className, annotation) {
      const wrap = document.createElement("div");
      wrap.className = "annotation-item";
      wrap.style.left = rect.left + rect.width + "px";
      wrap.style.top = rect.top + rect.height / 2 + "px";
      const anchor = document.createElement("div");
      anchor.className = "annotation-anchor " + className;
      anchor.textContent = text;
      anchor.title = annotation.quote || "";
      wrap.appendChild(anchor);
      wrap.appendChild(this.createDeleteChip(annotation));
      this.layer.appendChild(wrap);
    }
  }

  window.LectureLiteAnnotationTools = {
    create(options) {
      return new AnnotationTools(options);
    }
  };
})();
