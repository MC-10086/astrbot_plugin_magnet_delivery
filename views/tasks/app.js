const bridge = window.AstrBotPluginView;
const $ = (selector) => document.querySelector(selector);
let data = null, loading = false, timer = null, selection = null, mutating = false;
let mutationRevision = 0;
const pending = new Set();
const status = (text, error = false) => { $("#status").textContent = text; $("#status").classList.toggle("error", error); };
const element = (tag, text, className) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (className) node.className = className; return node; };
const size = (bytes) => bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(2)} GiB` : `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
const chatLabel = (group) => group.private_user ? `私聊 ${group.private_user}` : `群 ${group.group}`;
function choices(select, values, fallback) {
  const previous = select.value;
  select.replaceChildren(new Option(fallback, ""), ...values.map(([value, label]) => new Option(label, value)));
  if (values.some(([value]) => value === previous)) select.value = previous;
}
function render() {
  if (!data) return;
  const groups = data.groups;
  choices($("#platform"), [...new Set(groups.map(group => group.platform))].map(value => [value, value]), "全部实例");
  const platform = $("#platform").value;
  choices($("#group"), groups.filter(group => !platform || group.platform === platform).map(group => [group.chat, `${chatLabel(group)} · ${group.platform}`]), "全部会话");
  $("#active").textContent = data.summary.active;
  $("#speed").textContent = `${size(data.summary.speed)}/s`;
  $("#attention").textContent = data.summary.attention;
  const container = $("#groups"); container.replaceChildren();
  let visible = 0;
  for (const group of groups) {
    if ((platform && group.platform !== platform) || ($("#group").value && group.chat !== $("#group").value)) continue;
    const tasks = group.tasks.filter(task => $("#scope").value === "all" || ($("#scope").value === "attention" ? task.attention : task.live));
    if (!tasks.length) continue;
    visible += tasks.length;
    const section = element("section", undefined, "group"), heading = element("div", undefined, "group-heading"), title = element("h2", chatLabel(group));
    title.append(element("small", `机器人实例：${group.platform}`)); heading.append(title, element("span", `${tasks.length} 个任务`, "count")); section.append(heading);
    const wrap = element("div", undefined, "table-wrap"), table = element("table"), head = element("thead"), header = element("tr");
    for (const title of ["资源 / 任务", "状态", "下载进度", "操作"]) header.append(element("th", title));
    head.append(header); table.append(head); const body = element("tbody");
    for (const task of tasks) {
      const row = element("tr"), name = element("td"), state = element("td"), progress = element("td"), action = element("td");
      name.append(element("div", task.name, "resource"), element("div", `${task.short_id} · ${task.backend} · 用户 ${task.owner}`, "detail"), element("div", task.delivery, "detail"));
      state.append(element("span", task.label, `badge${task.attention ? " attention" : task.cleaned || task.state === "completed" ? " finished" : ""}`));
      if (task.error) state.append(element("div", task.error, "error-detail"));
      if (task.cleaned) state.append(element("div", "本地文件已清理", "detail"));
      const bar = element("div", undefined, "progress"), fill = element("span"); fill.style.width = `${task.progress ?? 0}%`; bar.append(fill);
      progress.append(element("div", task.progress === null ? "等待元数据" : `${task.progress.toFixed(1)}% · ${size(task.done)} / ${size(task.total)}`), bar, element("div", `${size(task.speed)}/s · 已确认发送 ${task.sent_count} 个`, "detail"));
      const button = element("button", task.retry_cleanup || task.state === "cleanup_failed" ? "重试清理" : task.state === "cancel_failed" ? "重试取消" : "取消并清理", "danger");
      button.disabled = !(task.cancellable || task.retry_cleanup) || mutating;
      if (task.state === "uploading") { button.textContent = "上传中"; button.title = "文件正在交给QQ接口，无法可靠撤回"; }
      else if (["cancelling", "sent_cleaning"].includes(task.state)) button.textContent = "清理中…";
      else if (!task.cancellable && !task.retry_cleanup) button.textContent = task.cleaned ? "已清理" : "—";
      button.onclick = () => {
        selection = {id: task.id, chat: group.chat, action: task.retry_cleanup ? "cleanup" : "cancel"};
        $("#confirm-title").textContent = task.retry_cleanup ? "重试清理本地文件？" : "取消任务并删除文件？";
        $("#confirm-description").textContent = task.retry_cleanup ? "文件已发送成功。本次只重试移除下载器任务和清理本地文件，不会重新上传。删除操作不可恢复。" : "将停止下载器中的此任务，并永久删除它的下载内容、压缩包和临时文件。此操作不可恢复。";
        $("#confirm-cancel").textContent = task.retry_cleanup ? "重试清理" : "取消并删除文件";
        $("#confirm-name").textContent = `${chatLabel(group)} · ${task.short_id} · ${task.name}`; $("#confirm").showModal();
      };
      action.append(button);
      if (["ready", "upload_unknown", "upload_failed"].includes(task.state)) {
        const resend = task.state !== "ready";
        const send = element("button", resend ? "核对后重发" : "发送到原会话");
        send.disabled = mutating;
        send.onclick = () => {
          selection = {id: task.id, chat: group.chat, action: resend ? "resend" : "send"};
          $("#confirm-title").textContent = resend ? "重发未确认的文件？" : "发送文件到原会话？";
          $("#confirm-description").textContent = resend
            ? "请先查看 QQ 原会话，确认未收到待重发文件。插件会跳过已确认发送的文件。"
            : "任务文件已在本机就绪。点击发送后，文件将交付到提交任务的原会话。";
          $("#confirm-cancel").textContent = resend ? "已核对，重发" : "确认发送";
          $("#confirm-name").textContent = `${chatLabel(group)} · ${task.short_id} · ${task.name}`;
          $("#confirm").showModal();
        };
        action.append(send);
      }
      row.append(name, state, progress, action); body.append(row);
    }
    table.append(body); wrap.append(table); section.append(wrap); container.append(section);
  }
  $("#empty").hidden = visible > 0;
  $("#updated").textContent = `更新于 ${new Date(data.updated_at * 1000).toLocaleTimeString()} · 保留展示最近 ${data.history_limit} 条结束记录`;
}
async function refresh(manual = false) {
  if (loading || mutating) return;
  const revision = mutationRevision;
  loading = true; $("#refresh").disabled = true;
  try {
    const snapshot = await bridge.apiGet("tasks");
    if (revision !== mutationRevision) return;
    const firstLoad = !data;
    data = snapshot; render();
    let event = null;
    for (const group of data.groups) for (const task of group.tasks) if (pending.has(task.id) && (!task.live || task.attention)) {
      pending.delete(task.id);
      const message = task.cleaned
        ? (task.delivered ? "文件已发送成功，本地文件已清理。" : "已取消，文件已清理。")
        : (task.error || task.label);
      event = {message: `任务 ${task.short_id}：${message}`, error: task.attention || task.state === "failed"};
    }
    if (event) status(event.message, event.error);
    else if (manual) status("列表已更新。");
    else if (firstLoad || $("#status").textContent.startsWith("刷新失败：")) status("已连接 AstrBot。管理面板可管理群聊和私聊的插件任务。");
  } catch (error) {
    if (revision === mutationRevision) status(`刷新失败：${error.message}。当前显示上次成功获取的数据。`, true);
  } finally {
    loading = false; $("#refresh").disabled = false;
    if (revision !== mutationRevision && !mutating) void refresh();
  }
}
$("#refresh").onclick = () => refresh(true);
for (const selector of ["#platform", "#group", "#scope"]) $(selector).onchange = render;
$("#back").onclick = () => $("#confirm").close();
$("#confirm-cancel").onclick = async () => {
  if (!selection || mutating) return;
  const selected = {...selection}; mutating = true; $("#confirm-cancel").disabled = true;
  mutationRevision += 1;
  try {
    const result = await bridge.apiPost(`tasks/${selected.action}`, {
      id: selected.id, chat: selected.chat, confirmed: selected.action === "resend",
    });
    pending.add(selected.id);
    $("#confirm").close(); status(result.message);
  } catch (error) { $("#confirm").close(); status(`操作未成功：${error.message}`, true); }
  finally { mutating = false; $("#confirm-cancel").disabled = false; render(); await refresh(); }
};
try {
  if (!bridge) throw new Error("请从 AstrBot 插件详情打开“任务管理”视图");
  const context = await bridge.ready(); document.documentElement.classList.toggle("dark", context.isDark);
  bridge.onContext(context => document.documentElement.classList.toggle("dark", context.isDark));
  await refresh();
  timer = setInterval(() => { if ($("#auto").checked && !document.hidden && !$("#confirm").open) refresh(); }, 3000);
} catch (error) { status(error.message, true); }
window.addEventListener("pagehide", () => clearInterval(timer));
