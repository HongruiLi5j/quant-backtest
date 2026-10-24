"use strict";
// 翻译页只持有当前页面内存；所有服务端文本均作为普通文本节点展示。
(() => {
  const i18n = window.AI_I18N;
  const t = (key, values) => i18n.text(key, values);
  const state = {
    status: null, statusError: null, idea: "", busy: null, result: null,
    confirmed: null, ack: false, feedbackOpen: false, feedbackNote: "",
    feedbackSent: false, error: null, copyState: null
  };
  const page = document.createElement("section");
  page.id = "p-translator";
  page.className = "page ai-translator";
  document.querySelector("main").appendChild(page);
  const nav = document.createElement("a");
  nav.href = "#/translator";
  nav.dataset.p = "translator";
  nav.className = "ai-translator-nav";
  nav.hidden = true;
  document.getElementById("tabs").appendChild(nav);

  function el(tag, text, className, parent) {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    if (className) node.className = className;
    if (parent) parent.appendChild(node);
    return node;
  }
  function button(key, parent, action, primary = false) {
    const node = el("button", t(key), primary ? "btn primary" : "btn plain", parent);
    node.type = "button";
    node.disabled = Boolean(state.busy);
    node.addEventListener("click", action);
    return node;
  }
  function errorText(code) {
    const key = `ai.err.${code}`;
    return i18n.has(key) ? t(key) : t("ai.err.TRANSLATOR_UNAVAILABLE");
  }
  async function request(action, body) {
    const response = await fetch(`/api/ai/${action}`, {
      method: body === undefined ? "GET" : "POST",
      credentials: "same-origin",
      headers: { "X-Requested-With": "dashboard", "Content-Type": "application/json" },
      ...(body === undefined ? {} : { body: JSON.stringify(body) })
    });
    // 404 和认证失败可能是纯文本，不能强制按 JSON 解码。
    if (response.status !== 200) {
      let data = {};
      try { data = await response.json(); } catch {}
      throw new Error(data.error || `HTTP_${response.status}`);
    }
    return response.json();
  }
  function clearResult() {
    state.result = null;
    state.confirmed = null;
    state.ack = false;
    state.feedbackOpen = false;
    state.feedbackNote = "";
    state.feedbackSent = false;
    state.error = null;
    state.copyState = null;
  }
  async function run(kind, action) {
    if (state.busy) return;
    state.busy = kind;
    state.error = null;
    render();
    try { await action(); }
    catch (error) {
      state.error = error.message;
      if (error.message === "TRANSLATOR_DISABLED") {
        state.status.enabled = false;
        nav.hidden = true;
        clearResult();
      }
    } finally {
      state.busy = null;
      render();
    }
  }
  async function translate() {
    if (state.busy || !state.idea.trim()) return;
    const idea = state.idea;
    clearResult();
    await run("translate", async () => {
      state.result = await request("translate", { idea });
    });
  }
  async function confirm() {
    const result = state.result;
    if (!result || result.can_confirm !== true || (result.needs_ack && !state.ack)) return;
    await run("confirm", async () => {
      const body = { request_id: result.request_id };
      if (result.needs_ack) body.ack_flags = state.ack;
      const reply = await request("confirm", body);
      if (reply.ok !== true) throw new Error("TRANSLATOR_BACKEND_FAILED");
      state.confirmed = reply.config;
    });
  }
  async function feedback() {
    if (!state.result || Array.from(state.feedbackNote).length > 200) return;
    await run("feedback", async () => {
      const reply = await request("feedback", {
        request_id: state.result.request_id, verdict: "wrong", note: state.feedbackNote
      });
      if (reply.ok !== true) throw new Error("TRANSLATOR_BACKEND_FAILED");
      state.feedbackSent = true;
      state.feedbackOpen = false;
    });
  }
  async function copyConfig() {
    try {
      await navigator.clipboard.writeText(JSON.stringify(state.confirmed, null, 2));
      state.copyState = "ai.copy.ok";
    } catch { state.copyState = "ai.copy.failed"; }
    render();
  }
  function renderFlags(result, parent) {
    if (!result.flags?.length) return;
    const list = el("ul", null, "ai-flags", parent);
    for (const flag of result.flags) {
      const item = el("li", null, flag.level === "block" ? "ai-mismatch" : "ai-warning", list);
      el("strong", [flag.rule_id, flag.field].filter(Boolean).join(" · "), null, item);
      const keys = ["ai.flag.mismatch", "ai.flag.block", "ai.flag.conflict", "ai.flag.missing"];
      const key = keys.includes(flag.message_key) ? flag.message_key : "ai.flag.block";
      el("p", t(key, { idea: flag.idea_says, config: flag.config_has, field: flag.field }), null, item);
    }
  }
  function renderResult(parent) {
    const result = state.result;
    if (!result) return;
    const output = el("div", null, "ai-result", parent);
    renderFlags(result, output);
    if (result.status === "clarify" || result.clarify) {
      el("h2", t("ai.clarify.title"), null, output);
      el("p", result.clarify?.message, "ai-clarify-message", output);
      const examples = el("div", null, "toolbar", output);
      // 标签与填入的想法都来自固定文案，模型只能提供上面的普通文本消息。
      for (const key of ["ai.example.equal_weight", "ai.example.momentum"]) {
        button(key, examples, () => { state.idea = t(key); clearResult(); render(); });
      }
    } else if (result.status === "rejected" || result.refusal) {
      const refusal = result.refusal || {};
      el("h2", refusal.code, "ai-refusal-code", output);
      const key = `ai.refusal.${refusal.code}`;
      const fallback = i18n.has(key) ? key : "ai.refusal.UNSUPPORTED_REQUEST";
      if (refusal.message) el("p", refusal.message, "ai-refusal-message", output);
      el("p", refusal.message_zh || i18n.text(fallback, {}, "zh"), null, output).lang = "zh-CN";
      el("p", refusal.message_en || i18n.text(fallback, {}, "en"), null, output).lang = "en";
      if (refusal.fields?.length) el("p", t("ai.refusal.fields", { fields: refusal.fields.join(", ") }), null, output);
    } else {
      const wrap = el("div", null, "tbl", output);
      const table = el("table", null, "ai-fields", wrap);
      const head = el("tr", null, null, el("thead", null, null, table));
      for (const key of ["ai.col.field", "ai.col.value", "ai.col.source"]) el("th", t(key), null, head).scope = "col";
      const body = el("tbody", null, null, table);
      for (const field of result.view || []) {
        const sourceKeys = { idea: "ai.src.idea", default: "ai.src.default", mismatch: "ai.src.mismatch", unchecked: "ai.src.unchecked" };
        const source = Object.hasOwn(sourceKeys, field.source) ? field.source : "unchecked";
        const row = el("tr", null, `ai-${source}${field.important ? " ai-important" : ""}`, body);
        const label = i18n.language === "en" ? field.label_en : field.label_zh;
        const cell = el("td", label || field.field, null, row);
        el("small", field.field, "ai-field-path", cell);
        el("td", field.value_text, null, row);
        el("td", t(sourceKeys[source]), "ai-source", row);
      }
      if (state.confirmed) {
        el("p", t("ai.confirmed.note"), "note", output);
        button("ai.btn.copy", output, copyConfig);
        if (state.copyState) el("p", t(state.copyState), "dim", output);
        // 剪贴板不可用时仍可手动选择确认后的 JSON。
        el("pre", JSON.stringify(state.confirmed, null, 2), "ai-config", output);
      } else if (result.can_confirm === true) {
        let checkbox;
        if (result.needs_ack) {
          const label = el("label", null, "ai-ack", output);
          checkbox = el("input", null, null, label);
          checkbox.type = "checkbox";
          checkbox.checked = state.ack;
          checkbox.disabled = Boolean(state.busy);
          el("span", t("ai.ack"), null, label);
        }
        const confirmButton = button("ai.btn.confirm", output, confirm, true);
        confirmButton.disabled = Boolean(state.busy) || (result.needs_ack && !state.ack);
        if (checkbox) checkbox.addEventListener("change", () => {
          state.ack = checkbox.checked;
          confirmButton.disabled = Boolean(state.busy) || !state.ack;
        });
      }
    }
    const actions = el("div", null, "toolbar", output);
    button("ai.btn.cancel", actions, () => { clearResult(); render(); });
    if (!state.feedbackSent) button("ai.btn.wrong", actions, () => { state.feedbackOpen = true; render(); });
    if (state.feedbackSent) el("p", t("ai.feedback.sent"), "dim", output);
    if (state.feedbackOpen) {
      const label = el("label", t("ai.feedback.note"), "ai-input-label", output);
      const note = el("textarea", null, null, label);
      note.maxLength = 200;
      note.rows = 3;
      note.value = state.feedbackNote;
      note.disabled = Boolean(state.busy);
      note.addEventListener("input", () => { state.feedbackNote = note.value; });
      button("ai.btn.feedback", output, feedback);
    }
  }
  function render() {
    nav.textContent = t("ai.title");
    nav.classList.toggle("on", location.hash.split("/")[1] === "translator");
    page.replaceChildren();
    el("h1", t("ai.title"), null, el("div", null, "title", page));
    const banner = el("div", t("ai.notice"), "note ai-banner", page);
    if (state.status?.model?.grade === "experimental") {
      el("span", `${t("ai.badge.exp")} ${state.status.backend === "api_llm" ? "api_llm " : ""}${state.status.model.version || ""}`, "ai-badge", banner);
    }
    // 此限制说明在关闭、加载、拒绝、确认及错误状态下始终可见。
    el("p", t("ai.note.limits"), "ai-limits", page);
    el("p", t("ai.note.coverage"), "dim", page);
    const card = el("div", null, "card", page);
    if (!state.status) {
      el("p", state.statusError ? errorText(state.statusError) : t("ai.loading"), null, card);
      return;
    }
    if (state.status.enabled !== true || ["disabled", "unavailable"].includes(state.status.state)) {
      const key = state.status.problem === "CONFIG_INVALID" ? "ai.cfg.bad" :
        state.status.enabled === true ? "ai.err.TRANSLATOR_UNAVAILABLE" : "ai.off";
      el("p", t(key), null, card);
      return;
    }
    const label = el("label", t("ai.input.label"), "ai-input-label", card);
    const input = el("textarea", null, null, label);
    input.placeholder = t("ai.input.ph");
    input.maxLength = state.status.limits.max_input_chars;
    input.rows = 5;
    input.value = state.idea;
    input.disabled = Boolean(state.busy);
    const actions = el("div", null, "toolbar", card);
    const translateButton = button(state.busy === "translate" ? "ai.busy" : "ai.btn.translate", actions, translate, true);
    translateButton.disabled = Boolean(state.busy) || !state.idea.trim();
    input.addEventListener("input", () => {
      state.idea = input.value;
      // 编辑想法后废弃旧结果，避免对着新输入确认上一份配置。
      if (state.result) {
        clearResult();
        card.querySelector(".ai-result")?.remove();
        messages.replaceChildren();
      }
      translateButton.disabled = Boolean(state.busy) || !state.idea.trim();
    });
    const messages = el("div", null, null, card);
    messages.setAttribute("role", "status");
    if (state.busy && state.busy !== "translate") el("p", t("ai.saving"), "dim", messages);
    if (state.error) el("p", `${errorText(state.error)} (${state.error})`, "ai-error", messages);
    renderResult(card);
  }

  // 所有路由进入都复用同一个 Promise，页面生命周期内只探测一次状态。
  const statusReady = request("status").then((status) => {
    state.status = status;
    nav.hidden = status.enabled !== true;
  }).catch((error) => { state.statusError = error.message; }).finally(render);
  window.AI_TRANSLATOR = { async load() { await statusReady; render(); } };
  window.addEventListener("dashboard-language-change", render);
  if (location.hash.split("/")[1] === "translator") page.classList.add("on");
  render();
})();
