const bridge = window.AstrBotPluginPage;
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const EMPTY_STATE = {
  config: {},
  presets: [],
  references: [],
  personas: [],
  capabilities: {},
};

let state = { ...EMPTY_STATE };
let editingPreset = null;
let confirmResolver = null;
let confirmPreviousFocus = null;

function closeConfirmation(accepted = false) {
  const dialog = $("#confirm-dialog");
  if (!dialog || !confirmResolver) return;
  const resolve = confirmResolver;
  confirmResolver = null;
  dialog.classList.add("hidden");
  dialog.setAttribute("aria-hidden", "true");
  document.body.classList.remove("dialog-open");
  if (confirmPreviousFocus?.isConnected) confirmPreviousFocus.focus();
  confirmPreviousFocus = null;
  resolve(Boolean(accepted));
}

function askConfirmation({ title, message, confirmText = "确认" }) {
  const dialog = $("#confirm-dialog");
  if (!dialog) return Promise.resolve(false);
  if (confirmResolver) closeConfirmation(false);
  confirmPreviousFocus = document.activeElement;
  $("#confirm-title").textContent = title;
  $("#confirm-message").textContent = message;
  $("#confirm-accept").textContent = confirmText;
  dialog.classList.remove("hidden");
  dialog.setAttribute("aria-hidden", "false");
  document.body.classList.add("dialog-open");
  return new Promise((resolve) => {
    confirmResolver = resolve;
    window.requestAnimationFrame(() => $("#confirm-cancel")?.focus());
  });
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[char]);
}

function readableError(error) {
  const raw = error?.message || error?.error || error;
  const text = typeof raw === "string" ? raw : JSON.stringify(raw);
  return !text || /�/.test(text) ? "操作失败，请检查配置和 AstrBot 日志。" : text;
}

function ensureOk(data) {
  if (data && data.ok === false) {
    throw new Error(readableError(data.error || data.message));
  }
  return data || {};
}

function withTimeout(promise, milliseconds, message) {
  let timer;
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      timer = window.setTimeout(() => reject(new Error(message)), milliseconds);
    }),
  ]).finally(() => window.clearTimeout(timer));
}

function toast(message, kind = "success", duration = 3600) {
  const element = $("#toast");
  if (!element) return;
  element.textContent = message;
  element.className = `toast show ${kind}`;
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => {
    element.className = "toast";
  }, duration);
}

function markDirty() {
  const indicator = $("#dirty");
  if (indicator) indicator.textContent = "有未保存修改";
}

function setSaveBusy(busy) {
  for (const button of [$("#save"), $("#save-bottom")].filter(Boolean)) {
    button.disabled = busy;
    if (button.id === "save") {
      button.innerHTML = busy
        ? '<span class="icon">…</span>保存中'
        : '<span class="icon">✓</span>保存配置';
    } else {
      button.textContent = busy ? "保存中…" : "保存配置";
    }
  }
}

function normalizeState(payload) {
  const next = ensureOk(payload);
  return {
    config: next.config && typeof next.config === "object" ? next.config : {},
    presets: Array.isArray(next.presets) ? next.presets : [],
    references: Array.isArray(next.references) ? next.references : [],
    personas: Array.isArray(next.personas) ? next.personas : [],
    capabilities: next.capabilities && typeof next.capabilities === "object"
      ? next.capabilities
      : {},
  };
}

function setConfigField(key, value) {
  const element = $(`[data-config="${CSS.escape(key)}"]`);
  if (!element) return;
  if (element.type === "checkbox") {
    element.checked = Boolean(value);
  } else if (Array.isArray(value)) {
    element.value = value.join("\n");
  } else if (value !== undefined && value !== null) {
    element.value = value;
  }
}

function getConfig() {
  const config = { ...state.config };
  $$('[data-config]').forEach((element) => {
    const key = element.dataset.config;
    if (element.type === "checkbox") {
      config[key] = element.checked;
    } else if (key === "allowed_users" || key === "allowed_groups") {
      config[key] = element.value
        .split(/[,\n]+/)
        .map((item) => item.trim())
        .filter(Boolean);
    } else if (element.type === "number") {
      config[key] = Number(element.value);
    } else {
      config[key] = element.value;
    }
  });
  config.default_preset_id = $("#default-preset")?.value || "";
  return config;
}

function renderStatus() {
  const official = state.config.provider !== "openai_compatible";
  $("#provider-status").textContent = official ? "NovelAI 官方" : "OpenAI / NewAPI";
  $("#provider-dot").classList.toggle("ok", Boolean(state.config.provider));

  const modes = {
    disabled: "全部禁用",
    command_only: "仅固定命令",
    llm_tool_only: "仅 LLM 工具",
    both: "命令 + LLM 工具",
  };
  $("#mode-status").textContent = modes[state.config.invoke_mode] || "未设置";
  const retryMode = state.config.retry_mode || "none";
  const retryAttempts = { none: 1, rate_limit_once: 2, rate_limit_twice: 3 }[retryMode] || 1;
  $("#guard-status").textContent = `单事件去重 · ${state.config.dedupe_window_seconds || 30} 秒`;
  $("#limit-status").textContent = `最多 1 张 · ${retryAttempts} 次 API 尝试`;
  document.body.classList.toggle("compatible", !official);
  document.body.classList.toggle("custom-model", state.config.model === "custom");
  $$('[data-retry-setting]').forEach((container) => {
    const control = $("input, select", container);
    if (control) control.disabled = retryMode === "none";
    container.classList.toggle("setting-disabled", retryMode === "none");
  });

  const keyLabel = $("#api-key-label");
  const authHelp = $("#auth-help");
  if (keyLabel) {
    keyLabel.textContent = official ? "NovelAI Persistent Token" : "兼容后端 API Key";
  }
  if (authHelp) {
    const header = state.config.openai_auth_header || "Authorization";
    const prefix = state.config.openai_auth_prefix || "Bearer";
    authHelp.textContent = official
      ? "请求头：Authorization: Bearer <Persistent Token>"
      : `请求头：${header}: ${`${prefix} <API Key>`.trim()}`;
  }

  const capabilities = state.capabilities || {};
  $("#capability").innerHTML = Object.entries({
    text_to_image: "文生图",
    img2img: "图生图",
    precise_reference: "Precise Reference",
    vibe_transfer: "Vibe Transfer",
  }).map(([key, label]) => (
    `<span class="${capabilities[key] ? "" : "off"}">${capabilities[key] ? "已支持" : "未启用"} · ${label}</span>`
  )).join("");
}

function fillConfig() {
  Object.entries(state.config).forEach(([key, value]) => setConfigField(key, value));
  renderStatus();
  renderPersonaOptions();
  renderPersonaMappings();
}

function renderPersonaOptions() {
  const list = $("#persona-options");
  if (!list) return;
  list.innerHTML = state.personas
    .map((persona) => `<option value="${esc(persona.id)}">${esc(persona.name || persona.id)}</option>`)
    .join("");
}

function renderActivePresets() {
  const box = $("#active-preset-list");
  if (!box) return;
  const selected = state.presets.find((preset) => preset.id === (state.config.default_preset_id || ""));
  const mapped = Object.entries(state.config.persona_preset_map || {})
    .map(([persona, id]) => {
      const preset = state.presets.find((item) => item.id === id);
      return preset ? {
        label: `${persona} → ${preset.name || preset.id}${preset.enabled === false ? "（已禁用）" : ""}`,
        active: preset.enabled !== false,
      } : null;
    })
    .filter(Boolean);
  const items = [];
  if (selected) items.push({
    label: `默认：${selected.name || selected.id}${selected.enabled === false ? "（已禁用）" : ""}`,
    active: selected.enabled !== false,
  });
  items.push(...mapped);
  box.innerHTML = items.length
    ? items.map((item) => `<article class="preset-card"><div class="preset-meta"><strong>${esc(item.label)}</strong><p>${item.active ? "当前生图会自动应用" : "映射已保留；重新启用角色卡后才会应用"}</p></div></article>`).join("")
    : '<div class="empty">当前没有应用角色卡。</div>';
}

function renderPresets() {
  const select = $("#default-preset");
  if (select) {
    select.innerHTML = '<option value="">不使用默认角色卡</option>' + state.presets
      .map((preset) => `<option value="${esc(preset.id)}">${esc(preset.name || preset.id)}${preset.enabled === false ? "（已禁用）" : ""}</option>`)
      .join("");
    select.value = state.config.default_preset_id || "";
  }

  const box = $("#preset-list");
  if (!state.presets.length) {
    box.innerHTML = '<div class="empty">还没有角色卡。点击“新建角色卡”填写正向与负面 Tag。</div>';
    renderActivePresets();
    return;
  }

  box.innerHTML = state.presets.map((preset) => {
    const cardLocked = preset.lock_positive !== false && Boolean(preset.positive_prompt);
    const qualityMode = preset.quality_override || (cardLocked ? "off" : "inherit");
    const badges = [
      preset.enabled === false ? "已禁用" : "已启用",
      cardLocked ? `角色卡 ${Number(preset.positive_strength || 1.35).toFixed(2)}×` : "角色卡未锁定",
      qualityMode === "off" ? "质量标签关闭" : qualityMode === "on" ? "质量标签开启" : "质量标签跟随全局",
    ];
    return `
    <article class="preset-card">
      <div class="preset-meta">
        <strong>${esc(preset.name || preset.id)}</strong>
        <p>${esc(preset.description || preset.positive_prompt || "未填写说明")}</p>
        <div class="preset-badges">${badges.map((badge) => `<span>${esc(badge)}</span>`).join("")}</div>
      </div>
      <div class="card-actions">
        <button class="small-btn" type="button" data-edit="${esc(preset.id)}">编辑</button>
        <button class="small-btn danger" type="button" data-delete="${esc(preset.id)}">删除</button>
      </div>
    </article>`;
  }).join("");

  $$('[data-edit]', box).forEach((button) => {
    button.addEventListener("click", () => openPreset(button.dataset.edit));
  });
  $$('[data-delete]', box).forEach((button) => {
    button.addEventListener("click", () => deletePreset(button.dataset.delete, button));
  });
  renderActivePresets();
}

function renderPersonaMappings() {
  const box = $("#persona-mappings");
  if (!box) return;
  if (!state.personas.length) {
    box.innerHTML = '<div class="empty">未读取到 AstrBot 人设。仍可在角色卡中手动绑定人设 ID。</div>';
    return;
  }

  const mapping = state.config.persona_preset_map || {};
  box.innerHTML = state.personas.map((persona) => `
    <label class="field">
      <span>${esc(persona.name || persona.id)} <small>(${esc(persona.id)})</small></span>
      <select data-persona="${esc(persona.id)}">
        <option value="">不自动应用</option>
        ${state.presets.map((preset) => `<option value="${esc(preset.id)}">${esc(preset.name || preset.id)}${preset.enabled === false ? "（已禁用）" : ""}</option>`).join("")}
      </select>
    </label>`).join("");

  $$('[data-persona]', box).forEach((element) => {
    element.value = mapping[element.dataset.persona] || "";
    element.addEventListener("change", () => {
      state.config.persona_preset_map = state.config.persona_preset_map || {};
      if (element.value) {
        state.config.persona_preset_map[element.dataset.persona] = element.value;
      } else {
        delete state.config.persona_preset_map[element.dataset.persona];
      }
      markDirty();
      renderActivePresets();
    });
  });
}

function renderReferenceOptions(selectedId = "") {
  const select = $("#preset-reference");
  if (!select) return;
  select.innerHTML = '<option value="">不使用参考图</option>' + state.references
    .map((reference) => `<option value="${esc(reference.id)}">${esc(reference.name || reference.id)} · ${esc(reference.type || "character")}</option>`)
    .join("");
  select.value = selectedId || "";
}

function renderReferences() {
  renderReferenceOptions(editingPreset?.reference_id || "");
  const box = $("#reference-list");
  if (!state.references.length) {
    box.innerHTML = '<div class="empty">还没有参考图。上传后可绑定到角色卡。</div>';
    return;
  }
  box.innerHTML = state.references.map((reference) => `
    <article class="reference-card">
      <div class="preset-meta">
        <strong>${esc(reference.name || reference.id)}</strong>
        <p>${esc(reference.type || "character")} · ${esc(reference.id)}</p>
      </div>
      <div class="card-actions">
        <button class="small-btn danger" type="button" data-ref-delete="${esc(reference.id)}">删除</button>
      </div>
    </article>`).join("");
  $$('[data-ref-delete]', box).forEach((button) => {
    button.addEventListener("click", () => deleteReference(button.dataset.refDelete, button));
  });
}

async function renderJobs({ reportErrors = true } = {}) {
  try {
    const data = ensureOk(await withTimeout(bridge.apiGet("jobs"), 15000, "读取任务记录超时"));
    const jobs = Array.isArray(data.jobs) ? data.jobs : [];
    const box = $("#job-list");
    const clearButton = $("#clear-jobs");
    if (clearButton) clearButton.disabled = jobs.length === 0;
    if (!jobs.length) {
      box.innerHTML = '<div class="empty">暂无运行记录。</div>';
      return;
    }
    box.innerHTML = jobs.slice().reverse().map((job) => `
      <article class="job-card">
        <div>
          <div class="${job.ok ? "job-ok" : "job-fail"}">${job.ok ? "成功" : "失败"} · ${esc(job.operation || "generate")}</div>
          <small>Job ${esc(job.job_id)} · ${esc(job.provider)} · 尝试 ${esc(job.attempts || 0)} 次 · ${new Date((job.created_at || 0) * 1000).toLocaleString()}</small>
        </div>
        <div class="job-meta"><small>${job.preset_id ? `角色卡 ${esc(job.preset_id)} · ` : "未使用角色卡 · "}${esc(job.message || job.error_code || "")}</small><div class="card-actions"><button class="small-btn danger" type="button" data-job-delete="${esc(job.job_id)}">删除记录</button></div></div>
      </article>`).join("");
    $$('[data-job-delete]', box).forEach((button) => {
      button.addEventListener("click", () => deleteJob(button.dataset.jobDelete, button));
    });
  } catch (error) {
    if (reportErrors) toast(readableError(error), "error");
  }
}

async function deleteJob(jobId, trigger = null) {
  if (!jobId) return false;
  const confirmed = await askConfirmation({
    title: "删除这条任务记录？",
    message: `将删除 Job ${jobId} 的本地运行记录，不会删除已经发送的图片。`,
    confirmText: "删除记录",
  });
  if (!confirmed) return false;
  if (trigger) trigger.disabled = true;
  try {
    const data = ensureOk(await withTimeout(
      bridge.apiPost("jobs/manage", { action: "delete", job_id: jobId }),
      15000,
      "删除任务记录超时",
    ));
    await renderJobs();
    toast(data.message || "任务记录已删除");
    return true;
  } catch (error) {
    toast(readableError(error), "error", 7000);
    return false;
  } finally {
    if (trigger?.isConnected) trigger.disabled = false;
  }
}

async function clearJobs() {
  const button = $("#clear-jobs");
  const confirmed = await askConfirmation({
    title: "清空全部任务记录？",
    message: "只会清空本次插件运行期间的记录，不会删除已经发送的图片。",
    confirmText: "清空记录",
  });
  if (!confirmed) return false;
  button.disabled = true;
  const previousText = button.textContent;
  button.textContent = "清空中…";
  try {
    const data = ensureOk(await withTimeout(
      bridge.apiPost("jobs/manage", { action: "clear" }),
      15000,
      "清空任务记录超时",
    ));
    await renderJobs();
    toast(data.message || "任务记录已清空");
    return true;
  } catch (error) {
    toast(readableError(error), "error", 7000);
    return false;
  } finally {
    button.textContent = previousText;
    if ($$(".job-card", $("#job-list")).length) button.disabled = false;
  }
}

function openPreset(id = "") {
  editingPreset = state.presets.find((preset) => preset.id === id) || null;
  $("#preset-editor").classList.remove("hidden");
  $("#preset-editor-title").textContent = editingPreset ? "编辑角色卡" : "新建角色卡";
  $("#delete-preset").style.display = editingPreset ? "inline-flex" : "none";
  const preset = editingPreset || {};
  $("#preset-id").value = preset.id || "";
  $("#preset-name").value = preset.name || "";
  $("#preset-persona").value = preset.persona_id || "";
  renderReferenceOptions(preset.reference_id || "");
  $("#preset-reference-type").value = preset.reference_type || "character";
  $("#preset-enabled").checked = preset.enabled !== false;
  $("#preset-lock-positive").checked = preset.lock_positive !== false;
  $("#preset-positive-strength").value = preset.positive_strength || 1.35;
  $("#preset-quality-override").value = preset.quality_override || "off";
  $("#preset-description").value = preset.description || "";
  $("#preset-positive").value = preset.positive_prompt || "";
  $("#preset-negative").value = preset.negative_prompt || "";
  $("#preset-name").focus();
}

function closePreset() {
  editingPreset = null;
  $("#preset-editor").classList.add("hidden");
}

async function reloadState() {
  state = normalizeState(await withTimeout(
    bridge.apiGet("settings"),
    15000,
    "读取插件配置超时，请确认 AstrBot 版本支持插件 Pages Web API",
  ));
  fillConfig();
  renderPresets();
  renderReferences();
}

async function savePreset() {
  const button = $("#save-preset");
  const payload = {
    name: $("#preset-name").value.trim(),
    persona_id: $("#preset-persona").value.trim(),
    reference_id: $("#preset-reference").value.trim(),
    reference_type: $("#preset-reference-type").value,
    lock_positive: $("#preset-lock-positive").checked,
    positive_strength: Number($("#preset-positive-strength").value || 1.35),
    quality_override: $("#preset-quality-override").value,
    description: $("#preset-description").value.trim(),
    positive_prompt: $("#preset-positive").value.trim(),
    negative_prompt: $("#preset-negative").value.trim(),
    enabled: $("#preset-enabled").checked,
  };
  if (!payload.name) {
    toast("角色卡名称不能为空", "error");
    $("#preset-name").focus();
    return;
  }

  button.disabled = true;
  button.textContent = "保存中…";
  try {
    const body = editingPreset
      ? { action: "update", id: editingPreset.id, ...payload }
      : { action: "create", ...payload };
    const data = ensureOk(await withTimeout(
      bridge.apiPost("presets/manage", body),
      15000,
      "保存角色卡超时",
    ));
    await reloadState();
    closePreset();
    toast(data.message || "角色卡已保存");
  } catch (error) {
    toast(readableError(error), "error", 7000);
  } finally {
    button.disabled = false;
    button.textContent = "保存角色卡";
  }
}

function cleanDeletedPresetFromClient(presetId) {
  state.presets = state.presets.filter((preset) => preset.id !== presetId);
  if (state.config.default_preset_id === presetId) {
    state.config.default_preset_id = "";
  }
  const mapping = state.config.persona_preset_map || {};
  state.config.persona_preset_map = Object.fromEntries(
    Object.entries(mapping).filter(([, mappedId]) => mappedId !== presetId),
  );
}

async function deletePreset(id, trigger = null) {
  if (!id) return false;
  const confirmed = await askConfirmation({
    title: "删除这个角色卡？",
    message: "删除后无法恢复；相关默认角色卡和 AstrBot 人设映射会一并清理。",
    confirmText: "删除角色卡",
  });
  if (!confirmed) return false;
  if (trigger) trigger.disabled = true;
  try {
    const data = ensureOk(await withTimeout(
      bridge.apiPost("presets/manage", { action: "delete", id }),
      15000,
      "删除角色卡超时",
    ));
    cleanDeletedPresetFromClient(id);
    if (Array.isArray(data.presets)) state.presets = data.presets;
    if (data.config && typeof data.config === "object") state.config = data.config;
    renderPresets();
    renderPersonaMappings();
    if (editingPreset?.id === id) closePreset();
    toast(data.message || "角色卡已删除");
    return true;
  } catch (error) {
    toast(readableError(error), "error", 7000);
    return false;
  } finally {
    if (trigger?.isConnected) trigger.disabled = false;
  }
}

async function deleteReference(id, trigger = null) {
  if (!id) return false;
  const confirmed = await askConfirmation({
    title: "删除这张参考图？",
    message: "删除后无法恢复；绑定该图片的角色卡会自动解除引用。",
    confirmText: "删除参考图",
  });
  if (!confirmed) return false;
  if (trigger) trigger.disabled = true;
  try {
    const data = ensureOk(await withTimeout(
      bridge.apiPost("references/manage", { action: "delete", id }),
      15000,
      "删除参考图超时",
    ));
    state.references = state.references.filter((reference) => reference.id !== id);
    state.presets = state.presets.map((preset) => (
      preset.reference_id === id ? { ...preset, reference_id: "" } : preset
    ));
    if (editingPreset?.reference_id === id) editingPreset.reference_id = "";
    renderReferences();
    renderPresets();
    toast(data.message || "参考图已删除");
    return true;
  } catch (error) {
    toast(readableError(error), "error", 7000);
    return false;
  } finally {
    if (trigger?.isConnected) trigger.disabled = false;
  }
}

async function saveConfig() {
  setSaveBusy(true);
  try {
    const data = ensureOk(await withTimeout(
      bridge.apiPost("config", getConfig()),
      15000,
      "保存配置超时",
    ));
    await reloadState();
    $("#dirty").textContent = "已保存";
    toast(data.message || "配置已保存");
  } catch (error) {
    toast(readableError(error) || "保存失败", "error", 7000);
  } finally {
    setSaveBusy(false);
  }
}

async function testProvider() {
  const button = $("#test-provider");
  button.disabled = true;
  button.textContent = "测试中…";
  try {
    const config = getConfig();
    const data = ensureOk(await withTimeout(bridge.apiPost("test-provider", {
      provider: config.provider,
      base_url: config.provider === "openai_compatible" ? config.openai_base_url : config.base_url,
      api_key: config.api_key === "********" ? undefined : config.api_key,
      api_token: config.api_token === "********" ? undefined : config.api_token,
      auth_header: config.openai_auth_header,
      auth_prefix: config.openai_auth_prefix,
    }), 20000, "连接测试超时"));
    toast(data.message || "测试完成", data.ok === false ? "error" : "success", 6000);
  } catch (error) {
    toast(readableError(error), "error", 7000);
  } finally {
    button.disabled = false;
    button.textContent = "测试连接";
  }
}

async function uploadReference() {
  const input = $("#reference-file");
  if (!input.files[0]) {
    toast("请先选择图片", "error");
    return;
  }
  const button = $("#upload-reference");
  button.disabled = true;
  button.textContent = "上传中…";
  try {
    const data = ensureOk(await withTimeout(
      bridge.upload("references/upload", input.files[0]),
      45000,
      "上传参考图超时",
    ));
    await reloadState();
    input.value = "";
    toast(data.message || "参考图已上传");
  } catch (error) {
    toast(readableError(error), "error", 7000);
  } finally {
    button.disabled = false;
    button.textContent = "上传参考图";
  }
}

$$('[data-tab]').forEach((tab) => {
  tab.addEventListener("click", () => {
    $$('[data-tab]').forEach((item) => item.classList.toggle("active", item === tab));
    $$('[data-panel]').forEach((panel) => {
      panel.classList.toggle("active", panel.dataset.panel === tab.dataset.tab);
    });
  });
});

$$('[data-config]').forEach((element) => {
  element.addEventListener("input", () => {
    state.config[element.dataset.config] = element.type === "checkbox"
      ? element.checked
      : element.type === "number"
        ? Number(element.value)
        : element.value;
    markDirty();
    if (["provider", "model", "openai_auth_header", "openai_auth_prefix", "invoke_mode", "dedupe_window_seconds", "retry_mode"].includes(element.dataset.config)) {
      renderStatus();
    }
  });
});

$("#default-preset").addEventListener("change", () => {
  state.config.default_preset_id = $("#default-preset").value;
  markDirty();
  renderActivePresets();
});
$("#save").addEventListener("click", saveConfig);
$("#save-bottom").addEventListener("click", saveConfig);
$("#test-provider").addEventListener("click", testProvider);
$("#upload-reference").addEventListener("click", uploadReference);
$("#add-preset").addEventListener("click", () => openPreset());
$("#close-editor").addEventListener("click", closePreset);
$("#cancel-preset").addEventListener("click", closePreset);
$("#save-preset").addEventListener("click", savePreset);
$("#delete-preset").addEventListener("click", () => {
  if (editingPreset) deletePreset(editingPreset.id, $("#delete-preset"));
});
$("#refresh-jobs").addEventListener("click", () => renderJobs());
$("#clear-jobs").addEventListener("click", clearJobs);
$("#confirm-cancel").addEventListener("click", () => closeConfirmation(false));
$("#confirm-accept").addEventListener("click", () => closeConfirmation(true));
$("#confirm-dialog").addEventListener("click", (event) => {
  if (event.target === $("#confirm-dialog")) closeConfirmation(false);
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && confirmResolver) closeConfirmation(false);
});

try {
  if (!bridge || typeof bridge.ready !== "function") {
    throw new Error("AstrBot 插件页面 Bridge 未加载，请升级 AstrBot 后重新打开页面");
  }
  await withTimeout(bridge.ready(), 10000, "AstrBot 插件页面 Bridge 初始化超时");
  await reloadState();
  await renderJobs({ reportErrors: false });
  $("#dirty").textContent = "配置已加载";
} catch (error) {
  $("#dirty").textContent = "初始化失败";
  toast(`页面初始化失败：${readableError(error)}`, "error", 12000);
  console.error("NovelAI Painter WebUI initialization failed:", error);
}
