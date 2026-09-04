const bridge = window.AstrBotPluginPage;
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
let state = { config: {}, presets: [], references: [], personas: [], capabilities: {} };
let editingPreset = null;

function toast(message, kind = "success") {
  const el = $("#toast"); el.textContent = message; el.className = "toast show " + kind;
  window.clearTimeout(toast.timer); toast.timer = window.setTimeout(() => { el.className = "toast"; }, 3200);
}
function markDirty() { $("#dirty").textContent = "有未保存修改"; }
function setConfigField(key, value) {
  const el = document.querySelector('[data-config="' + key + '"]');
  if (!el) return;
  if (el.type === "checkbox") el.checked = !!value;
  else if (Array.isArray(value)) el.value = value.join("\n");
  else if (value !== undefined && value !== null) el.value = value;
}
function getConfig() {
  const config = { ...state.config };
  $$('[data-config]').forEach(el => {
    const key = el.dataset.config;
    if (el.type === "checkbox") config[key] = el.checked;
    else if (key === "allowed_users" || key === "allowed_groups") config[key] = el.value.split(/[,\n]+/).map(x => x.trim()).filter(Boolean);
    else if (el.type === "number") config[key] = Number(el.value);
    else config[key] = el.value;
  });
  return config;
}
function renderStatus() {
  const official = state.config.provider !== "openai_compatible";
  $("#provider-status").textContent = official ? "NovelAI 官方" : "OpenAI / NewAPI";
  $("#provider-dot").classList.toggle("ok", !!state.config.provider);
  const modes = { disabled:"全部禁用", command_only:"仅固定命令", llm_tool_only:"仅 LLM 工具", both:"命令 + LLM 工具" };
  $("#mode-status").textContent = modes[state.config.invoke_mode] || "未设置";
  $("#guard-status").textContent = "单请求 · " + (state.config.dedupe_window_seconds || 30) + " 秒去重";
  document.body.classList.toggle("compatible", !official);
  const caps = state.capabilities || {};
  $("#capability").innerHTML = Object.entries({ text_to_image:"文生图", img2img:"图生图", precise_reference:"Precise Reference", vibe_transfer:"Vibe Transfer" }).map(([key,label]) => '<span class="' + (caps[key] ? "" : "off") + '">' + (caps[key] ? "已支持" : "未启用") + " · " + label + '</span>').join("");
}
function fillConfig() { Object.entries(state.config).forEach(([key,value]) => setConfigField(key,value)); renderStatus(); renderPersonaMappings(); }
function renderPresets() {
  const select = $("#default-preset");
  if (select) { select.innerHTML = '<option value="">不使用默认预设</option>' + state.presets.map(p => '<option value="' + p.id + '">' + esc(p.name || p.id) + '</option>').join(""); select.value = state.config.default_preset_id || ""; }
  const box = $("#preset-list");
  if (!state.presets.length) { box.innerHTML = '<div class="empty">还没有预设。点击“新建预设”创建人物或画风锚定配置。</div>'; return; }
  box.innerHTML = state.presets.map(p => '<article class="preset-card"><div class="preset-meta"><strong>' + esc(p.name || p.id) + '</strong><p>' + esc(p.description || p.character_prompt || p.style_prompt || "未填写说明") + '</p></div><div class="card-actions"><button class="small-btn" data-edit="' + p.id + '">编辑</button><button class="small-btn danger" data-delete="' + p.id + '">删除</button></div></article>').join("");
  $$('[data-edit]').forEach(b => b.addEventListener("click", () => openPreset(b.dataset.edit)));
  $$('[data-delete]').forEach(b => b.addEventListener("click", () => deletePreset(b.dataset.delete)));
}
function renderPersonaMappings() {
  const box = $("#persona-mappings");
  if (!box) return;
  if (!state.personas.length) { box.innerHTML = '<div class="empty">未读取到 AstrBot 人设。仍可在预设中手动绑定人设 ID。</div>'; return; }
  const mapping = state.config.persona_preset_map || {};
  box.innerHTML = state.personas.map(persona => '<label class="field"><span>' + esc(persona.name || persona.id) + ' <small>(' + esc(persona.id) + ')</small></span><select data-persona="' + esc(persona.id) + '"><option value="">不自动应用</option>' + state.presets.map(p => '<option value="' + p.id + '">' + esc(p.name || p.id) + '</option>').join('') + '</select></label>').join('');
  $('[data-persona]').forEach(el => { el.value = mapping[el.dataset.persona] || ''; el.addEventListener('change', () => { state.config.persona_preset_map = state.config.persona_preset_map || {}; if (el.value) state.config.persona_preset_map[el.dataset.persona] = el.value; else delete state.config.persona_preset_map[el.dataset.persona]; markDirty(); }); });
}
function renderReferences() {
  const box = $("#reference-list");
  if (!state.references.length) { box.innerHTML = '<div class="empty">暂无参考图。上传后可在预设中绑定。</div>'; return; }
  box.innerHTML = state.references.map(r => '<article class="reference-card"><div class="reference-meta"><strong>' + esc(r.name || r.id) + '</strong><p>' + esc(r.type || "character") + ' · ' + new Date((r.created_at || 0) * 1000).toLocaleString() + '</p></div><div class="card-actions"><button class="small-btn danger" data-ref-delete="' + r.id + '">删除</button></div></article>').join("");
  $$('[data-ref-delete]').forEach(b => b.addEventListener("click", () => deleteReference(b.dataset.refDelete)));
}
function renderJobs() {
  bridge.apiGet("jobs").then(data => {
    const jobs = data.jobs || []; const box = $("#job-list");
    if (!jobs.length) { box.innerHTML = '<div class="empty">暂无运行记录。</div>'; return; }
    box.innerHTML = jobs.slice().reverse().map(j => '<article class="job-card"><div><div class="' + (j.ok ? "job-ok" : "job-fail") + '">' + (j.ok ? "成功" : "失败") + ' · ' + esc(j.operation || "generate") + '</div><small>Job ' + esc(j.job_id) + ' · ' + esc(j.provider) + ' · ' + new Date((j.created_at || 0) * 1000).toLocaleString() + '</small></div><div><small>' + esc(j.message || j.error_code || "") + '</small></div></article>').join("");
  }).catch(err => toast(err.message, "error"));
}
function esc(value) { return String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c])); }
function openPreset(id = "") {
  editingPreset = state.presets.find(p => p.id === id) || null;
  $("#preset-editor").classList.remove("hidden"); $("#preset-editor-title").textContent = editingPreset ? "编辑预设" : "新建预设";
  $("#delete-preset").style.display = editingPreset ? "inline-flex" : "none";
  const p = editingPreset || {};
  $("#preset-id").value = p.id || ""; $("#preset-name").value = p.name || ""; $("#preset-persona").value = p.persona_id || ""; $("#preset-reference").value = p.reference_id || ""; $("#preset-reference-type").value = p.reference_type || "character"; $("#preset-description").value = p.description || ""; $("#preset-style").value = p.style_prompt || ""; $("#preset-character").value = p.character_prompt || ""; $("#preset-negative").value = p.negative_prompt || "";
  $("#preset-name").focus();
}
function closePreset() { editingPreset = null; $("#preset-editor").classList.add("hidden"); }
async function savePreset() {
  const payload = { name: $("#preset-name").value.trim(), persona_id: $("#preset-persona").value.trim(), reference_id: $("#preset-reference").value.trim(), reference_type: $("#preset-reference-type").value, description: $("#preset-description").value.trim(), style_prompt: $("#preset-style").value.trim(), character_prompt: $("#preset-character").value.trim(), negative_prompt: $("#preset-negative").value.trim(), enabled: true };
  if (!payload.name) { toast("预设名称不能为空", "error"); return; }
  try { const data = editingPreset ? await bridge.apiPost("presets/manage", { action: "update", id: editingPreset.id, ...payload }) : await bridge.apiPost("presets/manage", { action: "create", ...payload }); if (data.preset) { if (editingPreset) state.presets = state.presets.map(p => p.id === data.preset.id ? data.preset : p); else state.presets.push(data.preset); } renderPresets(); renderPersonaMappings(); closePreset(); toast(data.message || "预设已保存"); } catch (err) { toast(err.message, "error"); }
}
async function deletePreset(id) { if (!confirm("确定删除这个预设吗？")) return; try { const data = await bridge.apiPost("presets/manage", { action: "delete", id }); state.presets = state.presets.filter(p => p.id !== id); renderPresets(); renderPersonaMappings(); toast(data.message || "预设已删除"); } catch (err) { toast(err.message, "error"); } }
async function deleteReference(id) { if (!confirm("确定删除这张参考图吗？")) return; try { const data = await bridge.apiPost("references/manage", { action: "delete", id }); state.references = state.references.filter(r => r.id !== id); renderReferences(); toast(data.message || "参考图已删除"); } catch (err) { toast(err.message, "error"); } }
async function saveConfig() {
  const button = $("#save"); button.disabled = true; button.textContent = "保存中…";
  try { const data = await bridge.apiPost("config", getConfig()); state.config = data.config || getConfig(); fillConfig(); $("#dirty").textContent = "已保存"; toast(data.message || "配置已保存"); } catch (err) { toast(err.message || "保存失败", "error"); } finally { button.disabled = false; button.innerHTML = '<span class="icon">✓</span>保存配置'; }
}
async function testProvider() { const b = $("#test-provider"); b.disabled = true; b.textContent = "测试中…"; try { const c = getConfig(); const data = await bridge.apiPost("test-provider", { provider:c.provider, base_url:c.provider === "openai_compatible" ? c.openai_base_url : c.base_url, api_key:c.api_key, api_token:c.api_token }); toast(data.message || "测试完成", data.ok ? "success" : "error"); } catch (err) { toast(err.message, "error"); } finally { b.disabled = false; b.textContent = "测试连接"; } }
async function uploadReference() { const input = $("#reference-file"); if (!input.files[0]) { toast("请先选择图片", "error"); return; } const b = $("#upload-reference"); b.disabled = true; b.textContent = "上传中…"; try { const data = await bridge.upload("references/upload", input.files[0]); state.references.push(data.reference); renderReferences(); input.value = ""; toast(data.message || "参考图已上传"); } catch (err) { toast(err.message, "error"); } finally { b.disabled = false; b.textContent = "上传参考图"; } }

$$('[data-tab]').forEach(tab => tab.addEventListener("click", () => { $$('[data-tab]').forEach(x => x.classList.toggle("active", x === tab)); $$('[data-panel]').forEach(p => p.classList.toggle("active", p.dataset.panel === tab.dataset.tab)); }));
$$('[data-config]').forEach(el => el.addEventListener("input", markDirty));
$("#default-preset").addEventListener("change", () => { state.config.default_preset_id = $("#default-preset").value; markDirty(); });
$("#save").addEventListener("click", saveConfig); $("#save-bottom").addEventListener("click", saveConfig); $("#test-provider").addEventListener("click", testProvider); $("#upload-reference").addEventListener("click", uploadReference); $("#add-preset").addEventListener("click", () => openPreset()); $("#close-editor").addEventListener("click", closePreset); $("#cancel-preset").addEventListener("click", closePreset); $("#save-preset").addEventListener("click", savePreset); $("#delete-preset").addEventListener("click", () => editingPreset && deletePreset(editingPreset.id).then(closePreset)); $("#refresh-jobs").addEventListener("click", renderJobs);

try { await bridge.ready(); state = await bridge.apiGet("settings"); fillConfig(); renderPresets(); renderReferences(); renderJobs(); } catch (err) { toast("页面初始化失败：" + err.message, "error"); }
