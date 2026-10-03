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

mediaType.addEventListener("change", syncMediaLimits);
[imageLimit, videoLimit, perPostLimit, maxPosts].forEach(input => input.addEventListener("input", updateDownloadSummary));
syncMediaLimits();

form.addEventListener("submit", async event => {
  event.preventDefault();
  if (!form.reportValidity()) return;
  button.disabled = true;
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
    renderTaskReport(data.task_report, data);
    renderWorks(data.works);
    (data.items || []).forEach(item => {
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
  } catch (error) {
    renderLogEntries([{
      level: "error",
      message: `无法连接到桌面采集服务：${error.message || error}`,
      action: "确认桌面版仍在运行，并检查网络连接后重试。"
    }]);
  } finally {
    button.disabled = false;
  }
});
