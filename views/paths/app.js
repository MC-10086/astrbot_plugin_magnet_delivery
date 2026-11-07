const bridge = window.AstrBotPluginView;
const fields = [
  ["download_paths.directory", "统一下载目录 · 所有下载器默认共用"],
  ["aria2_auto.download_dir", "aria2 自动管理 · 独立目录（选填）"],
  ["qb.download_dir", "qBittorrent · 独立目录（选填）"],
  ["aria2.download_dir", "aria2 手动连接 · 独立目录（选填）"],
  ["xunlei_cli.download_dir", "迅雷官方 CLI · 独立目录（选填）"],
  ["aria2_auto.executable", "aria2 自动管理 · 程序文件", "aria2c.exe"],
  ["xunlei_cli.executable", "迅雷官方 CLI · 程序文件", "xunlei-cli.exe"],
];
const inputs = new Map();
const sections = new Map();
const dialog = document.querySelector("#picker");
const status = document.querySelector("#status");
const browseStatus = document.querySelector("#browse-status");
const entries = document.querySelector("#entries");
const pagination = document.querySelector("#pagination");
const previousPage = document.querySelector("#previous-page");
const nextPage = document.querySelector("#next-page");
const startLocation = document.querySelector("#start-location");
const separate = document.querySelector("#separate");
const individual = fields.filter(field => field[0].endsWith(".download_dir")).map(field => field[0]);
const individualFields = document.querySelector("#individual-fields");
const directoryOptions = document.querySelector(".options");
let selectedFields = [];
let active, listing, detectedCli, busy = false;
function message(node, text, error = false) { node.textContent = text; node.classList.toggle("error", error); }
function button(label, handler) { const node = document.createElement("button"); node.type = "button"; node.textContent = label; node.onclick = handler; return node; }
function select(path) { if (busy) return; inputs.get(active[0]).value = path; dialog.close(); message(status, "已选择，点击“保存路径”写入配置。"); }
async function browse(path = "", page = 1) {
  if (busy) return;
  busy = true; document.querySelector("#choose").disabled = true;
  document.querySelector("#location").value = path;
  entries.replaceChildren();
  pagination.hidden = true;
  startLocation.hidden = true;
  message(browseStatus, "正在读取目录…");
  try {
    const result = await bridge.apiGet("paths/browse", {path, page: String(page), kind: active[2] ? active[0] : "directory"});
    listing = result; document.querySelector("#location").value = result.path;
    for (const item of result.directories) {
      const label = item.linked ? item.name + "（链接，请选择实际目录）" : "▸ " + item.name;
      const entry = button(label, () => browse(item.path));
      entry.disabled = Boolean(item.linked);
      entries.append(entry);
    }
    for (const item of result.files) entries.append(button("选择程序：" + item.name, () => select(item.path)));
    entries.scrollTop = 0;
    const drives = document.querySelector("#drives"); drives.replaceChildren();
    for (const drive of result.drives) drives.append(button(drive, () => browse(drive)));
    pagination.hidden = result.pages <= 1;
    document.querySelector("#page-info").textContent = `第 ${result.page} / ${result.pages} 页 · 共 ${result.total} 个文件夹`;
    previousPage.disabled = result.page <= 1;
    nextPage.disabled = result.page >= result.pages;
    message(browseStatus, result.directories.length || result.files.length ? "点击文件夹进入，也可以粘贴完整路径后前往。" : "此目录没有可显示的项目。");
    document.querySelector("#choose").disabled = Boolean(active[2]);
  } catch (error) { listing = null; startLocation.hidden = false; message(browseStatus, error.message, true); }
  finally { busy = false; }
}
for (const field of fields) {
  const section = document.createElement("section"), label = document.createElement("label"), row = document.createElement("div"), input = document.createElement("input");
  input.id = field[0]; input.readOnly = true; input.placeholder = field[2] ? `选择 ${field[2]}，已自动找到时可留空` : individual.includes(field[0]) ? "留空跟随统一下载目录" : "留空自动创建统一目录";
  label.htmlFor = input.id; label.textContent = field[1]; row.className = "row"; inputs.set(field[0], input);
  const browseButton = button("浏览…", async () => {
    if (busy) return;
    active = field;
    document.querySelector("#picker-title").textContent = field[2] ? `选择 ${field[2]}` : "选择下载目录";
    dialog.showModal();
    const initial = individual.includes(field[0]) ? inputs.get("download_paths.directory").value : "";
    await browse(input.value || (field[2] === "xunlei-cli.exe" ? detectedCli : initial) || "");
  });
  row.append(input, browseButton, button("清空", () => { input.value = ""; message(status, "已清空，保存后使用默认设置。"); }));
  section.hidden = field[0] !== "download_paths.directory";
  sections.set(field[0], section);
  section.append(label, row); document.querySelector(field[2] ? "#program-fields" : individual.includes(field[0]) ? "#individual-fields" : "#shared-fields").append(section);
}
const cliTest = button("测试迅雷 CLI 连接", async () => {
  cliTest.disabled = true;
  message(status, "正在测试迅雷 CLI…");
  try {
    const result = await bridge.apiGet("paths/test-xunlei-cli");
    message(status, result.message, !result.message.includes("连接正常"));
  } catch (error) { message(status, error.message, true); }
  finally { cliTest.disabled = false; }
});
cliTest.disabled = true;
cliTest.hidden = true;
document.querySelector("#program-fields").append(cliTest);
separate.onchange = () => { individualFields.hidden = !separate.checked; message(status, separate.checked ? "请选择当前下载器的独立目录。" : "当前下载器将使用统一目录。"); };
document.querySelector("#up").onclick = () => listing && browse(listing.parent);
previousPage.onclick = () => listing && browse(listing.path, listing.page - 1);
nextPage.onclick = () => listing && browse(listing.path, listing.page + 1);
document.querySelector("#go").onclick = () => browse(document.querySelector("#location").value);
startLocation.onclick = () => browse();
document.querySelector("#location").onkeydown = (event) => { if (event.key === "Enter") { event.preventDefault(); browse(event.target.value); } };
document.querySelector("#choose").onclick = () => listing && !active[2] && select(listing.path);
document.querySelector("#cancel").onclick = () => dialog.close();
const save = document.querySelector("#save");
save.onclick = async () => {
  save.disabled = true;
  try {
    const paths = Object.fromEntries(selectedFields.map(key => [key, !separate.checked && individual.includes(key) ? "" : inputs.get(key).value]));
    const result = await bridge.apiPost("paths/save", {paths});
    for (const [key, value] of Object.entries(paths)) inputs.get(key).value = value;
    message(status, result.message);
  }
  catch (error) { message(status, error.message, true); }
  finally { save.disabled = false; }
};
try {
  if (!bridge) throw new Error("请从 AstrBot 插件详情打开“目录设置”页面。");
  const context = await bridge.ready(); document.documentElement.classList.toggle("dark", context.isDark);
  bridge.onContext((context) => document.documentElement.classList.toggle("dark", context.isDark));
  const result = await bridge.apiGet("paths"); for (const [key, value] of Object.entries(result.paths)) if (inputs.has(key)) inputs.get(key).value = value;
  detectedCli = result.detected_xunlei_cli;
  if (detectedCli) inputs.get("xunlei_cli.executable").placeholder = `已自动找到：${detectedCli}`;
  const group = result.download_backend;
  selectedFields = [...inputs.keys()].filter(key => key === "download_paths.directory" || key.startsWith(`${group}.`));
  for (const key of selectedFields) sections.get(key).hidden = false;
  const directory = selectedFields.find(key => individual.includes(key));
  directoryOptions.hidden = !directory;
  separate.checked = Boolean(directory && inputs.get(directory).value.trim());
  individualFields.hidden = !separate.checked;
  cliTest.hidden = group !== "xunlei_cli";
  inputs.get("download_paths.directory").placeholder = `默认：${result.default_directory}`;
  message(status, result.message); save.disabled = false; cliTest.disabled = false;
} catch (error) { message(status, error.message, true); }
