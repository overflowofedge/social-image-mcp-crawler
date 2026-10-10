const $ = id => document.getElementById(id);
const form = $("form");
const button = $("submit");
const status = $("status");
const itemsBox = $("items");
const worksBox = $("works");
const mediaType = $("media_type");
const imageLimit = $("image_limit");
const videoLimit = $("video_limit");
const perPostLimit = $("per_post_limit");
const maxPosts = $("max_posts");

const platformLabels = {
  douyin: "抖音", weibo: "微博", x: "X", xhs: "小红书",
  bilibili: "B站", instagram: "Instagram", other: "自定义网页"
};

const accountCards = [...document.querySelectorAll(".account[data-platform]")];
const loginStates = new Map();

function renderLogin(data) {
  const card = accountCards.find(row => row.dataset.platform === data.platform);
  if (!card) return;
  loginStates.set(data.platform, data);
  card.dataset.state = data.state;
  card.querySelector(".account-status").textContent = data.message || "请点击登录。";
  const start = card.querySelector(".start-login");
  start.disabled = Boolean(data.active);
  start.textContent = data.active ? "等待登录…" : data.session_available ? "重新登录" : data.login_label || "登录";
  const cancel = card.querySelector(".cancel-login");
  cancel.hidden = !data.active;
  cancel.disabled = data.state === "cancelling" || data.state === "saving";
}

async function refreshLogins() {
  try {
    const response = await fetch("/api/login", {cache: "no-store"});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    data.platforms.forEach(renderLogin);
  } catch (_) {
    accountCards.forEach(card => {
      card.querySelector(".account-status").textContent = "暂时无法读取登录状态，请确认应用正在运行。";
      card.querySelector(".start-login").disabled = false;
    });
  }
}

async function requestLogin(card, cancel = false) {
  const platform = card.dataset.platform;
  card.querySelector(cancel ? ".cancel-login" : ".start-login").disabled = true;
  try {
    const response = await fetch(cancel ? "/api/login/cancel" : "/api/login", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({platform, force: Boolean(loginStates.get(platform)?.session_available)})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error?.message || `HTTP ${response.status}`);
    renderLogin(data);
  } catch (error) {
    card.querySelector(".account-status").textContent = `无法操作登录：${error.message || error}`;
    card.querySelector(".start-login").disabled = false;
    card.querySelector(".cancel-login").disabled = false;
  }
}

accountCards.forEach(card => {
  card.querySelector(".start-login").addEventListener("click", () => requestLogin(card));
  card.querySelector(".cancel-login").addEventListener("click", () => requestLogin(card, true));
});

async function pollLogins() {
  await refreshLogins();
  setTimeout(pollLogins, [...loginStates.values()].some(value => value.active) ? 1000 : 5000);
}
pollLogins();

function renderLogEntries(entries) {
  status.innerHTML = "";
  const labels = {info: "信息", success: "成功", warning: "提醒", error: "错误"};
  const safeEntries = Array.isArray(entries) && entries.length ? entries : [
    {level: "error", message: "任务没有返回可读取的运行日志。", action: "重试一次；仍然失败时请重启桌面版。"}
  ];
  safeEntries.forEach(entry => {
    const level = labels[entry.level] ? entry.level : "info";
    const row = document.createElement("div");
    row.className = `log-line log-${level}`;
    const badge = document.createElement("span");
    badge.className = "log-level";
    badge.textContent = labels[level];
    const content = document.createElement("div");
    const message = document.createElement("div");
    message.textContent = entry.message || "";
    content.appendChild(message);
    if (entry.action) {
      const action = document.createElement("div");
      action.className = "log-action";
      action.textContent = `处理建议：${entry.action}`;
      content.appendChild(action);
    }
    row.append(badge, content);
    status.appendChild(row);
  });
}

function renderTaskReport(report, fallbackData = {}) {
  status.classList.remove("task-active");
  if (report && Array.isArray(report.logs)) {
    renderLogEntries(report.logs);
    return;
  }
  const rawError = fallbackData && fallbackData.error;
  const message = typeof rawError === "string" ? rawError : rawError && rawError.message;
  renderLogEntries([{
    level: "error",
    message: message ? `任务失败：${message}` : "任务失败，服务器没有返回详细原因。",
    action: "检查账号或网址、平台登录状态和网络连接后重试。"
  }]);
}

const stageLabels = {
  queued: "等待启动", preparing: "准备任务", resolving: "确认账号",
  retrieving: "检索并翻页", filtering: "筛选内容", downloading: "下载文件",
  finalizing: "整理结果", completed: "已完成", failed: "失败"
};

const stopReasonLabels = {
  target_reached: "候选内容已达到目标，已停止翻页。",
  post_limit_reached: "已达到最多检索作品数，分页结束。",
  source_exhausted: "平台未返回下一页游标，已翻到当前可读取的末页。",
  cursor_stalled: "下一页游标重复，为避免循环已停止翻页。",
  pagination_error: "后续分页读取失败，已保留已经取得的内容。"
};

function renderTaskProgress(task) {
  status.classList.add("task-active");
  const elapsed = Number(task.elapsed_seconds || 0).toFixed(1);
  const entries = [{
    level: task.state === "failed" ? "error" : "info",
    message: `${stageLabels[task.stage] || "处理中"} · 已运行 ${elapsed} 秒 · ${task.message || "后台任务正在运行。"}`
  }];
  entries.push({
    level: "info",
    message: `本次进度：已读取 ${task.pages_fetched || 0} 页，检查 ${task.posts_fetched || 0} 个作品，找到 ${task.images_found || 0} 张图片、${task.videos_found || 0} 个视频。`
  });
  if (task.stop_reason && stopReasonLabels[task.stop_reason]) {
    entries.push({
      level: ["cursor_stalled", "pagination_error"].includes(task.stop_reason) ? "warning" : "info",
      message: stopReasonLabels[task.stop_reason]
    });
  }
  if (task.stage === "downloading" || task.download_total || task.download_completed) {
    entries.push({
      level: "info",
      message: `下载进度：已处理 ${task.download_completed || 0} 个候选（候选池最多 ${task.download_total || 0} 个），新下载 ${task.downloaded || 0}，已存在 ${task.existing || 0}，重复 ${task.duplicate || 0}，失败 ${task.failed || 0}。`
    });
  }
  const activity = Array.isArray(task.activity) ? task.activity.slice(-7) : [];
  activity.forEach(entry => entries.push({
    level: entry.level || "info",
    message: `[${Number(entry.elapsed_seconds || 0).toFixed(1)} 秒] ${entry.message}`
  }));
  renderLogEntries(entries);
  status.classList.add("task-active");
}

const wait = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

async function waitForTask(taskId) {
  while (true) {
    await wait(850);
    const response = await fetch(`/api/tasks/${encodeURIComponent(taskId)}`, {cache: "no-store"});
    if (!response.ok) throw new Error(`无法读取任务进度（HTTP ${response.status}）`);
    const task = await response.json();
    renderTaskProgress(task);
    if (task.state === "completed" || task.state === "failed") return task.result || {};
  }
}

function updateDownloadSummary() {
  const count = input => input.value || "—";
  const limits = [];
  if (mediaType.value !== "videos") {
    limits.push(`最多 ${count(imageLimit)} 张图片（每个作品最多 ${count(perPostLimit)} 张）`);
  }
  if (mediaType.value !== "images") {
    limits.push(`最多 ${count(videoLimit)} 个视频`);
  }
  const notes = [`本次目标：${limits.join("，")}。数量是目标上限，完成日志会说明实际数量和不足原因。`];
  const imageTarget = Number(imageLimit.value);
  const capacity = Number(maxPosts.value) * Number(perPostLimit.value);
  if (mediaType.value !== "videos" && imageTarget > 0 && capacity > 0 && imageTarget > capacity) {
    notes.push(`当前设置理论最多取得 ${capacity} 张图片；请增加“最多检索作品数”或“每个作品最多张数”。`);
  }
  $("download_summary").textContent = notes.join(" ");
}

function syncMediaLimits() {
  const imagesSelected = mediaType.value !== "videos";
  const videosSelected = mediaType.value !== "images";
  $("image_settings").disabled = !imagesSelected;
  $("video_settings").disabled = !videosSelected;
  imageLimit.required = imagesSelected;
  perPostLimit.required = imagesSelected;
  videoLimit.required = videosSelected;
  updateDownloadSummary();
}

function renderWorks(works) {
  worksBox.innerHTML = "";
  if (!Array.isArray(works) || works.length === 0) {
    const empty = document.createElement("p");
    empty.className = "hint";
    empty.textContent = "本次没有新的作品。已下载作品会在下一次刷新时自动跳过。";
    worksBox.appendChild(empty);
    return;
  }
  const labels = {image: "图片作品", video: "视频作品", mixed: "图文/视频混合作品"};
  works.forEach(work => {
    const row = document.createElement("div");
    row.className = "work-row";
    const title = document.createElement("span");
    title.className = "work-title";
    title.textContent = work.title || work.work_id || "未命名作品";
    title.title = work.title || work.work_id || "未命名作品";
    const meta = document.createElement("span");
    meta.className = "work-meta";
    const kind = labels[work.media_type] || "作品";
    meta.textContent = `${kind} · ${work.media_count || 0} 个媒体`;
    row.append(title, meta);
    worksBox.appendChild(row);
  });
}

function renderItems(items) {
  itemsBox.innerHTML = "";
  (items || []).forEach(item => {
    const card = document.createElement("div");
    card.className = "item";
    if (item.media_type === "image") {
      const image = document.createElement("img");
      const previewQuery = new URLSearchParams({url: item.image_url});
      if (item.permalink) previewQuery.set("referer", item.permalink);
      image.src = `/api/image?${previewQuery.toString()}`;
      image.loading = "lazy";
      image.alt = item.title || item.author || "图片预览";
      card.appendChild(image);
    } else {
      const video = document.createElement("p");
      video.textContent = "视频文件";
      card.appendChild(video);
    }
    const text = document.createElement("p");
    text.textContent = item.title || item.author || item.platform || "";
    const link = document.createElement("a");
    link.href = item.permalink || item.image_url;
    link.target = "_blank";
    link.textContent = "打开原帖";
    text.appendChild(document.createElement("br"));
    text.appendChild(link);
    card.appendChild(text);
    itemsBox.appendChild(card);
  });
}

mediaType.addEventListener("change", syncMediaLimits);
[imageLimit, videoLimit, perPostLimit, maxPosts].forEach(input => input.addEventListener("input", updateDownloadSummary));
syncMediaLimits();

form.addEventListener("submit", async event => {
  event.preventDefault();
  if (!form.reportValidity()) return;
  button.disabled = true;
  const originalButtonText = button.textContent;
  button.textContent = "正在采集…";
  itemsBox.innerHTML = "";
  renderWorks([]);
  const platform = document.querySelector('input[name="platform"]:checked').value;
  const mediaTypeValue = mediaType.value;
  const imageLimitValue = mediaTypeValue !== "videos" ? Number(imageLimit.value) : null;
  const videoLimitValue = mediaTypeValue !== "images" ? Number(videoLimit.value) : null;
  const maxResults = (imageLimitValue || 0) + (videoLimitValue || 0);
  const body = {
    query: $("query").value,
    platforms: [platform],
    max_results: maxResults,
    media_type: mediaTypeValue,
    image_limit: imageLimitValue,
    video_limit: videoLimitValue,
    per_post_limit: mediaTypeValue !== "videos" ? Number(perPostLimit.value) : null,
    max_posts: Number($("max_posts").value),
    download: true,
    content_query: $("content_query").value || null,
    filter_mode: $("filter_mode").value,
    quality_mode: $("quality_mode").value
  };
  const targets = [];
  if (imageLimitValue) targets.push(`${imageLimitValue} 张图片`);
  if (videoLimitValue) targets.push(`${videoLimitValue} 个视频`);
  renderLogEntries([
    {level: "info", message: `开始采集${platformLabels[platform] || platform}，目标为 ${targets.join("、")}。`},
    {level: "info", message: `本次最多检查 ${body.max_posts} 个作品，请稍候。`}
  ]);
  try {
    const response = await fetch("/api/search", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body)
    });
    let data;
    try {
      data = await response.json();
    } catch (_error) {
      throw new Error(`服务器返回内容无法读取（HTTP ${response.status}）`);
    }
    if (!response.ok) {
      const rawError = data && data.error;
      throw new Error((rawError && rawError.message) || rawError || `请求失败（HTTP ${response.status}）`);
    }
    if (response.status === 202 && data.task_id) {
      renderTaskProgress(data);
      data = await waitForTask(data.task_id);
    }
    renderTaskReport(data.task_report, data);
    renderWorks(data.works);
    renderItems(data.items);
  } catch (error) {
    renderLogEntries([{
      level: "error",
      message: `无法连接到桌面采集服务：${error.message || error}`,
      action: "确认桌面版仍在运行，并检查网络连接后重试。"
    }]);
  } finally {
    button.disabled = false;
    button.textContent = originalButtonText;
  }
});
