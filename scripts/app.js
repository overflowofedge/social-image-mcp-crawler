const $ = id => document.getElementById(id);
const form = $("form");
const button = $("submit");
const status = $("status");
const itemsBox = $("items");
const mediaType = $("media_type");
const videoLimit = $("video_limit");

function syncMediaLimits() {
  const videosSelected = mediaType.value === "videos" || mediaType.value === "all";
  videoLimit.disabled = !videosSelected;
  videoLimit.required = videosSelected;
  if (!videosSelected) videoLimit.value = "";
}

mediaType.addEventListener("change", syncMediaLimits);
syncMediaLimits();

form.addEventListener("submit", async event => {
  event.preventDefault();
  button.disabled = true;
  itemsBox.innerHTML = "";
  status.textContent = "正在采集，请稍候…";
  const platform = document.querySelector('input[name="platform"]:checked').value;
  const imageLimit = Number($("image_limit").value);
  const videoLimitValue = videoLimit.value ? Number(videoLimit.value) : null;
  const mediaTypeValue = mediaType.value;
  const maxResults = mediaTypeValue === "all" ? imageLimit + (videoLimitValue || imageLimit) : (mediaTypeValue === "videos" ? (videoLimitValue || imageLimit) : imageLimit);
  const body = {
    query: $("query").value,
    platforms: [platform],
    max_results: maxResults,
    media_type: mediaTypeValue,
    image_limit: imageLimit,
    video_limit: videoLimitValue,
    per_post_limit: Number($("per_post_limit").value),
    max_posts: Number($("max_posts").value),
    download: $("download").checked,
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
