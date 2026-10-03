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

function updateDownloadSummary() {
  const count = input => input.value || "—";
  const limits = [];
  if (mediaType.value !== "videos") {
    limits.push(`最多 ${count(imageLimit)} 张图片（每个作品最多 ${count(perPostLimit)} 张）`);
  }
  if (mediaType.value !== "images") {
    limits.push(`最多 ${count(videoLimit)} 个视频`);
  }
  $("download_summary").textContent = `本次下载：${limits.join("，")}。实际数量以找到的可下载内容为准。`;
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
[imageLimit, videoLimit, perPostLimit].forEach(input => input.addEventListener("input", updateDownloadSummary));
syncMediaLimits();

form.addEventListener("submit", async event => {
  event.preventDefault();
  if (!form.reportValidity()) return;
  button.disabled = true;
  itemsBox.innerHTML = "";
  renderWorks([]);
  status.textContent = "正在采集并下载，请稍候…";
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
  try {
    const response = await fetch("/api/search", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body)
    });
    const data = await response.json();
    status.textContent = JSON.stringify(data, null, 2);
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
    status.textContent = "请求失败：" + error;
  } finally {
    button.disabled = false;
  }
});
