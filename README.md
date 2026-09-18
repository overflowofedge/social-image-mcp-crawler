# Social Image MCP Crawler

一个面向抖音、小红书、微博、X、Instagram 的图片检索与下载 MCP 服务。它把平台适配、用户意图解析、结果排序、质量过滤、去重和下载拆开，避免“只会把网页上的图全部下载下来”。

## 新手安装与使用（Windows）

下面按顺序复制命令即可完成安装。第一次使用只需要安装 Python 和 Git；不会写代码也可以使用。

### 不使用 Codex：双击启动本地应用

安装依赖并完成平台登录后，直接双击项目根目录的 `启动应用.bat`（英文文件名为 `start_app.bat` 也可以）。程序会启动本机网页界面并自动打开浏览器；在页面中输入提示词或平台链接，选择抖音/微博/X、图片或视频以及是否下载即可。文件默认保存到项目下的 `downloads` 文件夹。

应用只监听本机 `127.0.0.1:8765`，关闭黑色 PowerShell 窗口即可停止服务。它使用与 MCP 相同的采集、相关性筛选和下载代码，不需要打开 Codex，也不会消耗对话 token。

### 第 1 步：安装 Python

从 [python.org](https://www.python.org/downloads/) 安装 Python 3.10 或更高版本。安装界面第一页一定勾选 **Add Python to PATH**。安装完成后打开 PowerShell，输入：

```powershell
python --version
```

能看到 `Python 3.10`、`Python 3.11` 或更高版本就可以继续。如果提示找不到 `python`，重新安装并勾选上面的选项。

### 第 2 步：下载项目

安装 Git 后，在 PowerShell 中逐行执行：

```powershell
git clone https://github.com/wozhendemeiyou/social-image-mcp-crawler.git
cd social-image-mcp-crawler
```

### 第 3 步：安装项目依赖

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
Copy-Item .env.example .env
social-image-mcp --check
```

如果 PowerShell 阻止虚拟环境脚本，只需先执行一次：

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

然后重新执行 `.\.venv\Scripts\Activate.ps1`。每次重新打开 PowerShell 使用项目时，都先运行这一句激活环境：

```powershell
cd social-image-mcp-crawler
.\.venv\Scripts\Activate.ps1
```

### 第 4 步：配置抖音登录（抓取抖音必须）

抖音的搜索和博主主页需要登录态。项目使用开源的 [dy-cli](https://github.com/Youhai020616/douyin) 负责抖音登录和数据读取。先在项目根目录执行下面三行，把 dy-cli 放到项目约定的位置：

```powershell
git clone https://github.com/Youhai020616/douyin.git third_party\dy-cli
python -m pip install -e .\third_party\dy-cli
playwright install chromium
```

如果提示 `third_party\dy-cli` 已存在，说明已经安装过，直接继续下一步即可。然后运行登录脚本：

```powershell
.\scripts\douyin_login.ps1
```

浏览器弹出后，用手机抖音扫码登录。看到 `login completed` 后关闭窗口即可。登录信息只保存在本机的 `.cache` 目录，不会上传 GitHub。

如果你只想先测试 MCP 工具发现，可以跳过登录；真正抓取抖音图片或视频时再登录即可。小红书、微博、X、Instagram 也需要各自的平台登录或 API 配置，未配置的平台会显示 `unavailable`，不会影响其他平台。

### 第 5 步：连接 MCP 客户端

以 Codex CLI 为例，在项目根目录执行：

```powershell
codex mcp add social-image --env PYTHONUTF8=1 --env PYTHONIOENCODING=utf-8 -- python .\scripts\run_mcp.py
codex mcp list
```

如果客户端不是从项目目录启动，请使用绝对路径。先在项目目录执行 `Get-Location` 查看路径，再把下面的 `<项目绝对路径>` 替换掉：

```powershell
codex mcp add social-image --env PYTHONUTF8=1 --env PYTHONIOENCODING=utf-8 -- python "<项目绝对路径>\scripts\run_mcp.py"
```

添加完成后重新打开一个 Codex 任务。在对话中直接说“搜索抖音上的咖啡店图片并下载”，客户端就会调用这个 MCP。不要在 PowerShell 窗口里手动输入 JSON；MCP 服务只接受客户端发送的标准协议消息。

如果你使用 Claude Desktop、Cursor 等支持 JSON 配置的客户端，把下面配置加入它们的 MCP 配置文件，并将两个 `<项目绝对路径>` 替换成实际路径。Windows 路径中的反斜杠要写成两个反斜杠：

```json
{
  "mcpServers": {
    "social-image": {
      "command": "<项目绝对路径>\\.venv\\Scripts\\python.exe",
      "args": ["<项目绝对路径>\\scripts\\run_mcp.py"],
      "env": {
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8"
      }
    }
  }
}
```

例如项目放在 `D:\\social-image-mcp-crawler` 时，`command` 就是 `D:\\social-image-mcp-crawler\\.venv\\Scripts\\python.exe`，`args` 就是 `D:\\social-image-mcp-crawler\\scripts\\run_mcp.py`。保存配置后重启客户端。

### 第 6 步：第一次调用示例

在 MCP 客户端中，可以直接这样说：

```text
用 social-image 抓取抖音号 Gracebb0722 最近 20 个作品的图片，下载到本地。
```

也可以明确要求视频：

```text
用 social-image 抓取抖音昵称“放学小野猪”的公开视频，最多 10 个，下载原视频。
```

程序默认把文件保存到项目下的 `downloads` 文件夹，并用 `.cache` 保存断点和缓存。再次执行相同任务会自动续传。

### 常见问题

- **提示 `No recommended source configured`**：还没有配置来源项目，按“来源项目接入”完成安装，并检查 `.env` 中的命令路径。
- **抖音号或昵称搜不到**：确认抖音号没有多余空格；改用 `creator_name` 填昵称；仍然找不到时，复制博主完整主页链接，改用 `profile_url`。主页链接通常最稳定。
- **提示登录、验证码或 403**：重新运行 `scripts\\douyin_login.ps1` 登录，并确认当前网络可以打开抖音。程序不会绕过验证码或平台限制。
- **下载目录在哪里**：默认是项目目录下的 `downloads`；可以在调用时传入 `output_dir` 指定其他目录。

### X 平台为什么以前下载不了，如何修复

X 的公开搜索接口通常只给图片缩略图；视频还必须从 `media.variants` 里取 MP4 地址。现在项目已自动请求图片原图（`pbs.twimg.com` 的 `name=orig`），并选择 X 返回的最高码率 MP4；下载请求也带浏览器 User-Agent 和 Referer。

要使用 X 下载，请完成下面配置：

1. 在 [X Developer Portal](https://developer.x.com/en/portal/dashboard) 创建项目和应用，生成 **Bearer Token**。
2. 打开项目根目录的 `.env`，填写：

   ```dotenv
   X_BEARER_TOKEN=粘贴你的BearerToken
   ```

3. 如果 API 返回 `401`、`403` 或 `429`，用项目脚本让 gallery-dl 读取浏览器登录态：

   ```powershell
   python -m pip install gallery-dl
   powershell -ExecutionPolicy Bypass -File .\scripts\setup_account.ps1 -Platform x -Browser edge
   ```

   在弹出的 Edge 窗口登录 X，脚本会用公开的 NASA 媒体页验证会话，然后把会话保存到本机 `.cache`。这个文件已被 `.gitignore` 忽略。验证页只用于确认登录和媒体读取，不代表你要下载 NASA 的内容。

4. 修改 `.env` 后，关闭旧的 MCP 任务并重新打开一个任务，再调用：

   ```text
   搜索 X 上的 coffee shop 图片和视频，下载到本地。
   ```

只有 `url` 或 `media_url` 指向 `pbs.twimg.com`、`video.twimg.com` 的真实媒体时才会下载；X 返回的网页链接、转发页面和受保护账号不会被当成媒体文件。Bearer Token 只能访问 API 允许的公开内容，不能绕过私密账号、付费内容或平台限流。

按 X 博主抓取时可以直接使用 `@用户名`、`from:用户名`、`x-user:用户名`，或调用 `fetch_creator_images` 时填写 `platform: "x"` 和 `creator_id`。例如：

```text
抓取 X 博主 @jwj180 最近 20 条含媒体的帖子并下载图片。
```

`from:jwj180` 也会被识别为博主时间线。X 的普通关键词搜索接口近期经常返回 404，因此不要把博主名当作普通关键词检索。

该账号路径由 gallery-dl 读取 X 登录态；如果只配置 Bearer Token 而没有 gallery-dl Cookie，搜索 API 可以工作，但博主时间线下载可能因 X 权限限制返回空结果。

调用 X 视频时，在 MCP 工具参数中设置 `media_type: "videos"`；要同时获取图片和视频则设置 `media_type: "all"`。普通图片搜索默认 `media_type: "images"`。

## 能力

- `search_images`：默认走“推荐采集项目召回 + 本地语义重排”，支持抖音、小红书、微博、B 站、X 和 Instagram 的关键词、平台内容 ID、帖子/笔记/视频 URL；多平台并发检索后统一排序，可通过 `download=true` 直接下载。配置本地 CLIP 后会追加视觉重排；`retrieval_mode=hybrid` 才会额外叠加公共索引或旧平台适配器。
- 其他网页图片：在本地应用选择“其他平台”并粘贴完整的 `http(s)` 网页地址，或在 `search_images` 中传入 `platforms: ["other"]`，服务会提取网页主图、`img`、懒加载图片和 `srcset` 图片并交给统一下载器。普通平台链接仍按对应平台处理；其他平台目前只提取图片，不下载视频。
- `fetch_creator_images`：按抖音、微博或 B 站博主账号抓取主页作品图片或视频。抖音支持 `creator_id`、`creator_name`（昵称）和完整 `profile_url`；微博与 B 站支持数字 UID 或完整主页链接。`media_type=images|videos|all` 分别获取图片、原视频或两者；分页、日期范围、作品/媒体上限和断点续传保持一致。
- 账号内容筛选：只有提供 `content_query` 才启用。对象要求会先按人物、穿搭、风景、场景、建筑、食物、饮品、包和身体部位等类别做粗粒度视觉分类，再按 include/exclude/required 规则过滤；没有可用模型时 `optional` 回退账号结果，`required` 失败关闭。
- `inspect_item`：按平台和 ID 获取单条内容的图片候选。
- `download_images`：按候选结果下载原图，自动重试、校验图片尺寸、按内容哈希去重并写入 `manifest.jsonl`。
- `list_platforms`：查看平台能力、凭据和配置状态。
- 轻量语义理解：中英文分词、同义词扩展、否定词、ID 精确匹配、方向偏好和质量加权。
- 可选视觉语义重排：对召回图的缩略图/原图运行 CLIP 图文相似度，减少“文字相关但画面不对”的结果；普通关键词请求按候选短名单执行，账号请求一旦提供 `content_query` 即会进行视觉相关性校验（即使 `quality_mode=fast`），避免抖音/微博只凭文案误下载。
- 推荐来源项目：dy-cli（抖音关键词/图集/详情）、MediaCrawler（小红书/微博）、gallery-dl（X/Instagram/微博）、XHS-Downloader（小红书高分辨率）。来源项目只做候选召回，排序和下载由本项目统一完成。
- SQLite 查询缓存，降低重复检索延迟。
- 可选本地语义重排：设置 `SEMANTIC_MODEL` 后启用 `sentence-transformers`，默认不下载模型、不增加启动成本。

对象分类会复用本地 `VISION_MODEL`（默认 `openai/clip-vit-base-patch32`），无需安装 YOLO。若配置了本地检测或分割权重，可通过 `OBJECT_MODEL` 启用；`OBJECT_LABEL_MAP` 用来把自定义标签映射到标准类别，例如 `{"person":"person","shirt":"clothing","dress":"clothing","mountain":"landscape"}`。CLIP 适合人物、穿搭、风景、场景等粗粒度判断；像素级 mask 或精确身体部位裁剪需要专门的人体解析/服装分割权重。

## 运行

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
Set-Location "<project-path>"
pip install -e .
Copy-Item .env.example .env
social-image-mcp --check
```

`--check` 是终端诊断命令。确认无误后，把 `social-image-mcp` 配置给 MCP 客户端；不要在 PowerShell 里直接运行后手动按回车，stdio 服务会把空行当作 JSON-RPC 输入。

现在如果检测到真实终端，服务会直接显示提示并退出，不会进入 JSON-RPC 解析循环。

MCP 客户端使用 stdio 连接：

```powershell
python -m social_image_mcp.server
```

如果你已经在别的目录安装过旧版本，先确认命令指向当前环境：

```powershell
Get-Command social-image-mcp
python -m pip install -e "<project-path>"
social-image-mcp --check
```

桌面 MCP 客户端配置示例：

```json
{
  "mcpServers": {
    "social-image": {
      "command": "social-image-mcp",
      "env": {
        "X_BEARER_TOKEN": "",
        "INSTAGRAM_ACCESS_TOKEN": "",
        "DOUYIN_COOKIE": ""
      }
    }
  }
}
```

需要按账号内容筛选时再提供内容要求；例如只保留人物穿搭和生活照：

```json
{
  "platform": "douyin",
  "creator_id": "Gracebb0722",
  "media_type": "images",
  "content_query": "只要人物穿搭和日常生活，排除风景、建筑、街景和纯场景图",
  "filter_mode": "optional",
  "quality_mode": "balanced",
  "max_images": 20,
  "download": true
}
```

也可以按抖音昵称精确查找（昵称必须只命中一个账号）：

```json
{
  "platform": "douyin",
  "creator_name": "放学小野猪",
  "media_type": "videos",
  "max_posts": 10,
  "max_images": 10,
  "download": true
}
```

### 抖音账号输入提示

抖音博主可以用以下任一种方式填写：

1. **抖音号**：放在 `creator_id`，例如 `Gracebb0722`。
2. **昵称**：放在 `creator_name`，例如 `放学小野猪`。昵称必须精确命中一个账号。
3. **主页链接**：如果用抖音号或昵称搜索不到，改用完整的抖音主页链接放在 `profile_url`；链接通常最稳定。

抖音号：

```json
{"platform":"douyin","creator_id":"你的抖音号","download":true}
```

昵称：

```json
{"platform":"douyin","creator_name":"博主昵称","download":true}
```

搜索不到时改用主页链接（把 `<sec_uid>` 换成链接中的实际值）：

```json
{"platform":"douyin","profile_url":"https://www.douyin.com/user/<sec_uid>","download":true}
```

`search_images` 中直接输入一串抖音号可能会被当作作品 ID 解析。需要按账号抓取时，优先调用 `fetch_creator_images`，或使用带前缀的查询：`douyin-user:<抖音号>`、`douyin-name:<昵称>`。如果仍然找不到，请回退到上面的完整主页链接。

视频下载与图片下载共用账号身份校验、分页和断点续传，但使用独立的视频地址和 MIME 校验，结果中的 `media_type` 为 `video`。

B 站关键词和视频 ID 使用公开 API 返回视频封面；账号抓取支持 B 站 UID/空间主页。原视频仅在平台返回完整渐进式播放地址时下载，受限接口会在 `warnings` 或 `error` 中说明。

Codex 推荐使用项目自带的绝对路径启动器，避免客户端工作目录变化导致缓存和下载目录漂移：

```powershell
codex mcp add social-image --env PYTHONUTF8=1 --env PYTHONIOENCODING=utf-8 -- "python" "C:\\path\\to\\project\\scripts\\run_mcp.py"
codex mcp list
```

添加后重新打开一个 Codex 任务，让客户端重新发现 `search_images`、`download_images` 等工具。不要在 PowerShell 中手动向 stdio 进程输入 JSON。

也可以从任意目录运行项目验收脚本：

```powershell
python "C:\\path\\to\\project\\scripts\\verify_mcp.py"
python "C:\\path\\to\\project\\scripts\\verify_mcp.py" --query "咖啡店室内" --platform douyin --max-results 4
python "C:\\path\\to\\project\\scripts\\verify_mcp.py" --query "douyin:作品ID" --platform douyin --max-results 1 --download
```

脚本会通过同一个绝对路径 launcher 连接 stdio，不会把 PowerShell 的当前目录、手动 JSON 输入或旧的全局安装命令混入验收结果。

## 来源项目接入

默认 `retrieval_mode=sources`，不会因为本地未配置 Cookie/API 就偷偷改走旧网页抓取。来源子进程默认最多运行 45 秒；登录、验证码或平台请求没有完成时会返回明确错误，不会长期占住 MCP。先安装推荐项目及其依赖：

```powershell
.\scripts\install_sources.ps1
python -m pip install aiomysql motor xhshow jieba wordcloud parsel pyhumps asyncmy aiosqlite curl-cffi textual pywebview emoji
python -m pip install --no-deps --force-reinstall fastmcp==3.4.7 fastmcp-slim==3.4.7
playwright install chromium
```

不要在本 MCP 环境中直接安装两个仓库的完整 `requirements.txt`：MediaCrawler 当前声明的 `matplotlib>=3.11.0` 不支持 Python 3.10，而 XHS-Downloader 最新依赖会把 `mcp` 升级到 2.x。上面的命令是当前 Python 3.10 环境验证过的桥接依赖组合。

然后在 `.env` 中配置随项目提供的桥接器（把路径改成当前仓库的绝对路径）：

```dotenv
MEDIA_CRAWLER_COMMAND=python "C:/path/to/project/scripts/media_crawler_bridge.py" --platform {platform} --query "{query}" --item-id "{item_id}" --url "{url}" --limit {limit}
XHS_DOWNLOADER_COMMAND=python "C:/path/to/project/scripts/xhs_downloader_bridge.py" --query "{query}" --item-id "{item_id}" --url "{url}" --limit {limit}
GALLERY_DL_BINARY=gallery-dl
```

`media_crawler_bridge.py` 使用 MediaCrawler 的浏览器会话做小红书/微博关键词与 ID 召回；抖音由 `dy-cli` 独立负责，避免 MediaCrawler 的浏览器登录流程嵌入 stdio 后卡住；`xhs_downloader_bridge.py` 只在小红书单条 ID/URL 请求时补充高清图集详情，不参与关键词搜索。三者都只输出 JSON 元数据和图片 URL，MCP 自己负责图片下载和校验。首次运行来源项目可能需要扫码登录；这不是把 Cookie/API 当作检索算法，而是把平台项目当作候选召回器，最终相关性由本项目的意图、语义、CLIP、质量和去重层决定。

命令必须把候选记录打印到 stdout。可以输出单个 JSON、JSON 数组或 JSON Lines；常见字段包括 `id`、`image_url`/`images`、`title`、`author`、`width`、`height`、`permalink`。`gallery-dl` 使用 `--dump-json` 输出元数据，MCP 自己负责图片下载和校验。

推荐的桥接脚本只负责把对应项目的结果转换成上述 JSON 合约，不负责下载：

```json
{"id":"n1","image_url":"https://cdn.example/a.jpg","title":"咖啡店室内","width":1600,"height":900,"permalink":"https://..."}
```

检查看到来源状态：

```text
list_sources()
```

## 平台兼容模式

X 使用 `X_BEARER_TOKEN` 调用 API v2 的搜索/媒体接口。Instagram 使用 Graph API，需要 `INSTAGRAM_ACCESS_TOKEN`，按标签搜索还需要 `INSTAGRAM_USER_ID`。如果需要旧版平台适配器，可显式设置 `retrieval_mode=platform`；`hybrid` 会把来源项目、公共索引和旧适配器合并后再统一排序。Cookie/API 不再是默认检索路径。

启用浏览器回退：

```powershell
pip install -e ".[browser]"
playwright install chromium
```

启用语义重排（可选）：

```powershell
pip install -e ".[semantic]"
$env:SEMANTIC_MODEL="BAAI/bge-small-zh-v1.5"
```

## 阶段记录

### 阶段 1：采集底座重划

目标：让平台项目只做候选召回，避免平台接口直接决定最终图片。

结论：已完成 `SourceHub` 和多个真实桥接器，把 dy-cli、MediaCrawler、XHS-Downloader、gallery-dl 统一成可替换来源；抖音已从 MediaCrawler 的后台扫码链路拆出，关键词/图集/详情由 dy-cli 负责；桥接器只做平台召回/高清详情，不负责最终下载；旧 Cookie/API 适配器改为显式兼容模式。

建议：先运行 `social-image-mcp --check` 或 MCP 的 `list_sources()`。首次使用抖音前，在项目 PowerShell 中运行 `scripts\\douyin_login.ps1` 完成一次可见扫码登录；如果来源显示 `configured=false`，检查 `.env` 中的绝对路径和命令模板。`configured=true` 只表示来源命令存在，`verified=true` 才表示最近一次真实请求成功；以 `platforms.<平台>.count`、`error`、`verified` 和实际图片下载记录为准。

抖音 ID/URL 说明：`douyin:<作品ID>` 或抖音作品 URL 会先请求作品详情；若当前网络被抖音限制，服务会明确返回“需要可访问抖音的网络代理”，不会用相似搜索结果替代目标作品。关键词成功召回过的作品会写入 `.cache/dy-cli-results.json`，同一作品 ID 在 6 小时内可从本地精确复用；缓存过期后仍需详情接口可访问。代理按 dy-cli 的配置设置，例如：

```powershell
python -m dy_cli.main config set api.proxy http://127.0.0.1:7897
```

### 阶段 2：精准度算法

目标：让图片是否符合提示由统一 AI 层判断。

结论：已完成结构化意图解析、文本语义排序、CLIP 图文重排、分辨率/缩略图惩罚、感知哈希去重和本地反馈偏置。

建议：安装 `.[vision]` 并准备本地模型。服务默认只读本地模型缓存（`VISION_LOCAL_ONLY=true`），MCP 启动时会后台预加载，单次请求只给视觉增强 8 秒预算；视觉层超时会自动返回平台召回结果，不会阻塞 MCP，模型完成后后续请求自动启用视觉排序。需要允许联网下载时显式设置 `VISION_LOCAL_ONLY=false`。

### 阶段 3：下载与反馈闭环

目标：只下载 Top-K 合格图片，并让用户确认结果能够影响后续排序。

结论：已完成自动下载、图片校验、重试、manifest、近似重复过滤和 `submit_feedback` 工具。

建议：先用 `download=false` 检查候选，再对确认结果调用 `download_images` 或 `download=true`。

### 阶段 4：验证

目标：证明代码和 MCP 入口可运行。

结论：当前离线测试覆盖来源解析、桥接器规范化、服务路由、意图、排序、视觉回退、下载和反馈，共 58 项；MCP stdio 工具可被客户端发现；pytest 已限制只收集本项目 `tests/`，不会误跑第三方仓库测试。已实测抖音 MCP stdio 关键词检索约 6 秒返回候选，并成功下载 4 张 JPEG（尺寸 1674×2232 至 2160×3240）；连续请求触发 `verify_check` 时会在数秒内返回明确错误，不再等待 300 秒。

建议：真实平台验证必须使用你有权访问的账号/来源项目，并逐平台记录命中率后再调整模型和权重。

### 阶段 5：抖音 stdio 现场验收

目标：验证 Codex/MCP 客户端到抖音来源、精确排序和图片下载的完整链路，而不是只验证配置文件。

结论：已通过同一 stdio 客户端实测关键词 `咖啡店室内`，约 6 秒返回 4 个不同作品的候选；下载校验全部通过，生成 4 张 JPEG，尺寸为 1674×2232 至 2160×3240。中文元数据已修复为 UTF-8。`douyin:<作品ID>` 会严格匹配目标作品；已召回作品可在 6 小时内从本地精确索引复用。连续请求触发抖音 `verify_check` 时现在会在数秒内返回明确错误，不再空等 300 秒。视觉 CLIP 的生产启用和进程隔离见阶段 13。

边界：抖音作品详情接口在当前网络返回“需要国内 IP/代理”，这属于平台访问条件，不是 MCP 连接故障；X、Instagram 未登录时 `gallery-dl` 会返回认证错误，微博搜索 URL 在 `gallery-dl` 中不支持，服务会明确报告这些情况。服务不会用相似作品冒充 ID 目标，也不会绕过验证码或风控。`configured=true` 只表示来源命令存在，真实成功必须同时看到候选 `count > 0`、无 `error`，以及下载记录 `status=downloaded`。

建议：日常先用关键词检索；要按 ID/URL 精确获取，配置 dy-cli 可访问抖音的网络代理，或先成功召回该作品让本地精确索引复用。下一阶段再逐平台用你有权访问的微博、X、Instagram 账号做同样的现场验收。

### 阶段 6：多平台失败透明度

目标：验证 X、Instagram、微博在没有可用账号或来源支持时不会假装成功，并确保 `hybrid` 模式仍能保留其他召回通道的结果。

结论：已现场验证 X（约 1.6 秒）返回 `AuthRequired`，Instagram（约 1.4 秒）返回登录页错误，微博搜索 URL 被 `gallery-dl` 明确判定为不支持；这些错误现在会出现在对应平台的 `error` 字段。来源状态新增 `verified`：抖音精确缓存请求实测 `verified=true`，X/Instagram 认证失败保持 `verified=false`。`hybrid` 已改为通道隔离，来源失败不会丢弃公共索引或已配置 API 的候选。X/Instagram 的公共 Bing 索引在本次关键词下没有返回合规平台页面，因此没有下载记录，未将其记为成功。

建议：下一步只需为确实要用的平台配置合法凭据或可用来源：X 可使用 Bearer Token 或 gallery-dl 的认证 Cookie，Instagram 可使用 Graph API Token，微博应使用 MediaCrawler 登录态或兼容 JSON 网关。配置完成后必须重新做“候选 count、无 error、下载 status”三项验收。

### 阶段 7：Codex 启动与超时修复

目标：解决 Codex 中旧 MCP 进程、工作目录漂移、Windows 编码和来源卡死导致的“等待 300 秒/返回空结果”。

结论：已完成以下修复并用真实 stdio 客户端验收：

- `scripts/run_mcp.py` 将工作目录、源码路径、缓存和下载目录固定到本项目绝对路径；Codex 的 `social-image` 已注册为该 launcher，而不是依赖旧的全局安装命令。
- `SEARCH_TIMEOUT_SECONDS=55` 为整个 `search_images` 增加服务端硬截止；来源子进程超时会回收进程树并返回 `search_timeout`/`adapter_error`，客户端不会再无期限等待。
- `scripts/douyin_login.ps1` 强制 UTF-8，修复 Windows PowerShell 读取脚本时中文错误信息导致的解析失败。
- 浏览器回退在导航、DOM 提取或登录失败时也会关闭自己创建的页面/上下文；CDP 用户浏览器不会被关闭。
- 同一 Codex launcher 下，关键词 `咖啡店室内` 实测约 6.8 秒返回 4 个候选；作品 ID `7639200056281136754` 实测约 22.7 秒返回精确图并下载成功，文件为 1674×2232 JPEG，视觉检查确认为咖啡店室内。

建议：修改 MCP 配置后重新打开一个 Codex 任务，让客户端重新发现工具；用 `list_platforms` 看能力状态时，以 `verified=true`、候选 `count>0`、下载 `status=downloaded` 三项作为真实可用标准。`configured=true` 只表示命令或程序存在，不代表平台登录已成功。

### 阶段 8：多平台隔离超时

目标：默认请求多个平台时，某个平台登录或来源卡住不能拖累已经成功的其他平台。

结论：每个平台现在有独立的 `PLATFORM_TIMEOUT_SECONDS`（默认 50 秒），超时只写入该平台的 `platform_timeout`，其他平台候选继续参与排序；全局 `SEARCH_TIMEOUT_SECONDS`（默认 55 秒）仍作为最后保险。真实混合请求 `douyin + x + instagram` 约 6.6 秒返回 4 张抖音候选，X/Instagram 的认证错误分别保留在各自平台字段中。

建议：日常调用可以不指定单个平台，服务会返回能访问的平台结果和其他平台的明确错误。需要补齐 X/Instagram/微博时，只配置对应合法登录态，然后重新运行 `scripts\\verify_mcp.py` 验收，不需要修改核心排序或下载代码。

### 阶段 9：平台路由与失败熔断

目标：避免明确平台的 ID/URL 被送去所有平台，并避免未登录来源在每次请求时重复启动浏览器或 CLI。

结论：当查询包含 `douyin:`、`x:`、`instagram:` 等明确平台 ID，或能识别平台的 URL，且调用未显式传入 `platforms` 时，服务只请求目标平台。来源失败后按平台熔断 120 秒，其他平台不受影响；真实验收中抖音 ID 自动路由约 0.5 秒返回，X 首次认证失败约 1.1 秒，第二次相同会话约 0.02 秒返回原始原因。

建议：ID/URL 优先使用带平台前缀或完整链接；关键词可以显式指定平台，也可以多平台并行。账号状态变化后可等待短暂熔断到期或重启 MCP 服务进行重新验证。

### 阶段 10：持久验证状态

目标：MCP 重启或图片候选命中缓存后，仍能区分“安装过”“最近成功过”和“当前可用”，避免旧状态造成误判。

实际证据：真实抖音请求成功后，`.cache/source-verification.json` 只记录来源、平台、成功/失败时间与错误，不保存账号或 Cookie；启动新进程执行 `scripts\\run_mcp.py --check`，抖音仍显示 `verified=true`、`ready=true` 和 `last_verified_at=2026-09-05T02:20:16.068706Z`。随后真实请求触发 `verify_check`，新进程能够显示最近失败，`verified=true` 保留最近 24 小时成功证据，但 `ready=false` 准确表示最后一次请求失败。缓存命中也会重新读取这些实时状态。

结论：`configured` 仅表示工具存在；`verified` 表示 24 小时内至少有一次真实成功；`ready` 表示最近一次真实请求成功。验证账本即使因权限或文件锁无法写入，也不会把已经成功的平台搜索改判成失败。

建议：判断平台是否真正可用时，以本次候选 `count > 0`、平台无 `error`、下载记录 `status=downloaded` 为最终验收；`verified/ready` 用于快速诊断，不替代本次请求结果。X、Instagram、微博仍需完成各自合法账号会话后再做同样验收，小红书因当前没有账号暂不列为优先项。

### 阶段 11：一次性账号会话入口

目标：让账号访问条件与图片检索算法彻底分开，并避免手工复制 Cookie、手改 JSON 或手动启动 stdio 服务。

实际证据：新增 `scripts\\setup_account.ps1`。抖音调用 dy-cli 官方登录流程；微博调用 MediaCrawler 的可见浏览器登录并立即做一次真实关键词候选验证；X/Instagram 打开平台官网，登录后由 gallery-dl 从本机已登录浏览器读取会话，导出到项目 `.cache` 下的受忽略 Netscape 文件，再验证真实媒体元数据。脚本不会读取或显示密码，也不会绕过验证码或登录保护。

结论：登录会话只解决“平台允许访问”，不会参与相关性判断。用户意图仍由结构化意图解析、文本语义/CLIP 排序、分辨率与水印过滤、去重和反馈偏置处理。当前抖音已完成真实下载验收；微博后续已完成登录和关键词验收（见阶段 14）；X、Instagram 在用户完成官方登录前仍保持 `verified=false`，不宣称完成。

建议：关闭正在运行的 `social-image` MCP 后，在项目目录仅运行对应平台的一条命令：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup_account.ps1 -Platform x -Browser edge
powershell -ExecutionPolicy Bypass -File .\scripts\setup_account.ps1 -Platform instagram -Browser edge
powershell -ExecutionPolicy Bypass -File .\scripts\setup_account.ps1 -Platform weibo
```

每个平台脚本成功后，新建一个 Codex 任务使 MCP 读取新的本地会话文件，再运行 `search_images` 的候选、错误和下载三项验收。不要在 PowerShell 中手动输入 MCP JSON。会话文件只存在于 `.cache`，不要复制给他人。

### 阶段 12：Codex 300 秒现场故障定位

目标：判断截图中的 300 秒超时究竟来自搜索代码、平台风控，还是 Codex 持有旧 MCP 连接。

实际证据：截图对应任务仍连接到 09:49 启动的旧 `scripts\\run_mcp.py` 进程，该进程早于本轮超时和状态修复。使用同一 launcher 新建 stdio 会话后，抖音关键词请求在 6.43 秒返回明确的 `verify_check`，没有等待 300 秒；结束旧进程后，当前已打开任务返回 `Transport closed`，证明 MCP 通道不会在任务中自动热重连。随后再从新 stdio 会话请求作品 ID `7639200056281136754`，1.45 秒返回精确候选并下载为 `1674x2232` JPEG，SHA-256 为 `71ccdf2830b34f8ff11efd376ef3d293288d1b0d3aa2a73487edf2f9048bf1c5`。

结论：300 秒不是当前搜索逻辑的执行时间，而是旧 Codex 任务中的过期 MCP 进程/连接。新代码的硬截止有效；关键词当前仍可能被抖音风控拒绝，但会快速、明确返回。ID 链路、图片下载和校验均已真实通过。

建议：修改 MCP 代码或配置后必须新建 Codex 任务，让客户端重新启动服务。新任务先调用 `list_platforms`，再用已知作品 ID 做一次 `download=true` 验收；关键词若出现 `verify_check`，不要反复高频重试，稍后再试或先使用作品 URL/ID。当前已打开任务的 `Transport closed` 无法靠继续调用自行恢复。

### 阶段 13：视觉模型与 300 秒超时修复

目标：让 CLIP 视觉重排真正参与生产请求，同时保证模型冷启动、CPU 推理或模型异常都不能阻塞 MCP stdio。

实际证据：`.env` 已启用本地缓存模型 `openai/clip-vit-base-patch32`，并保持 `VISION_LOCAL_ONLY=true`、视觉增强预算 8 秒。CLIP 现在运行在独立的 `scripts/vision_worker.py` 子进程中，MCP 主进程只通过 JSON 管道发送缩略图和接收分数；服务关闭时会主动终止该子进程。独立视觉验证返回 `vision_similarity=0.21038225293159485`。标准 stdio 双请求验收结果：冷请求 8.31 秒返回精确候选 `7639200056281136754:1`，视觉仍在加载时明确降级；等待 25 秒后热请求 0.92 秒返回同一候选，`vision.enabled=true`、`vision_similarity=0.19396883249282837`。同时 `verify_mcp.py` 实测下载成功，图片为 JPEG、`1674×2232`，`ok=true`。

结论：抖音 `search_images` 的 300 秒超时根因已修复并通过同一 stdio 链路验收。问题不是客户端输入 JSON，也不是图片下载本身，而是 Torch/CLIP 在 MCP 进程内的冷启动和 CPU 计算阻塞；现在最坏情况只会在 8 秒后返回来源排序，主服务保持可用。视觉分数已成为可观测证据，不再只是配置项。

建议：重启 Codex 中的 `social-image` MCP 任务后再使用新代码；首次请求允许视觉模型后台预热，随后请求会启用视觉排序。平台验收仍需逐个平台满足 `count > 0`、`error=null` 和下载记录 `status=downloaded`，不能仅凭 `configured=true` 或 `vision.enabled=true` 判定成功。下一阶段完成 X、Instagram 登录并做同样的严格现场验收，同时补测微博精确 ID/URL 路由。

### 阶段 14：微博关键词与对比视觉排序

目标：修复微博候选召回，并验证“咖啡店室内”不会仅靠帖子文字把咖啡杯、饮品特写排到室内空间图之前。

实际证据：MediaCrawler 已通过原生 CDP 模式连接隔离的 Edge 用户目录，用户完成微博官方扫码登录后，关键词 `咖啡店室内` 返回 6 个候选，平台 `error=null`、`verified=true`、`ready=true`。初次结果中有一张饮品特写误入 Top 3；加入正向空间提示与饮品/户外/人物等互斥负向提示后，CLIP 对三张室内图给出正向 margin `0.05306`、`0.04483`、`0.03518`，饮品特写候选 `5336862822502143:6` 的 margin 为负并被移出 Top 3。严格复验耗时 19.81 秒，三张 Top 3 图片均下载成功，尺寸均约 `2000x2667`。人工检查确认前两张是明确的室内空间，第三张也是室内空间但更接近居住/休闲场景，与“咖啡店”的相关性为中等。微博 ID `5336862822502143` 的独立复验在 18.77 秒返回 16 个候选，Top 4 全部保留同一作品 ID 并下载成功；人工检查确认餐食、咖啡和室内图均属于指定作品图集。测试结果为 61 项全部通过。

结论：微博关键词召回、精确 ID/URL 路由、下载和视觉排除干扰图均已通过验收；对比视觉排序确实纠正了饮品特写误排。关键词结果当前是 2 张强相关、1 张中等相关，不能描述为三张都高度精准；ID 请求会保留指定作品的完整图集语义，不因单张图片与关键词不相似而误删。

建议：微博关键词结果可先使用 Top 2；后续将场所属性加入负向对比或提高视觉 margin 门槛，再决定是否保留第三张。按 ID/URL 下载时应按作品图集整体验收，不套用关键词 Top-N 的删图规则。

### 阶段 15：Codex 旧 MCP 连接复验

目标：解释最新截图中的 300 秒超时，并确认当前代码、Codex 配置和任务内 MCP 连接三者各自状态。

实际证据：`C:\\Users\\wangn\\.codex\\config.toml` 正确指向项目的绝对路径启动器。截图对应的任务仍持有 11:56 启动的 `run_mcp.py` 进程；该任务调用 `list_platforms` 时读到的抖音最后成功时间仍是 `03:43:01Z`，而磁盘验证账本已更新到 `04:54:45Z`，证明它没有加载新代码/新状态。使用同一启动器新建标准 MCP stdio 会话后，抖音 ID `7639200056281136754` 在 8.8 秒返回 1 个精确候选，平台 `error=null`，并下载 `1674x2232` JPEG，SHA-256 为 `71ccdf2830b34f8ff11efd376ef3d293288d1b0d3aa2a73487edf2f9048bf1c5`。结束旧进程后，原任务立即返回 `Transport closed`，没有自动重连；61 项测试全部通过。

结论：截图中的 300 秒是旧 Codex 任务持有过期/阻塞的 MCP 进程，不是当前抖音来源、下载器或视觉算法仍需 300 秒。代码内 55 秒全局截止和 8 秒视觉降级在新进程中有效；已经打开的任务无法热替换 MCP 进程。

建议：现在只需在 Codex 新建一个任务，不要重启 PowerShell、不要手动输入 JSON、不要重新复制配置。新任务中直接说“调用 social-image，下载抖音作品 `7639200056281136754` 的图片”；这会启动当前代码。该请求通过后，再分别完成 X 和 Instagram 的官方登录与验收。

### 阶段 16：第一阶段用户试用冻结

目标：先只把抖音、微博两条线路交给用户试用，暂不让 X、Instagram 的未完成状态影响阶段结论。

实际证据：抖音精确 ID `7639200056281136754` 实测约 9.69 秒返回 1 个候选，平台 `error=null`，下载 JPEG 尺寸 `1674x2232`，状态为 `downloaded`。抖音关键词 `咖啡店室内` 实测约 15.39 秒返回 6 个候选，Top 3 均下载成功，尺寸从 `1674x2232` 到 `1910x2546`；人工检查确认三张都是明确的咖啡店室内图。微博关键词 `咖啡店室内` 实测约 25.22 秒返回 6 个候选，CLIP 对比视觉排序返回 Top 3，三个候选的 margin 为 `0.05306`、`0.04483`、`0.03518`，三张均以 `2000x2667` JPEG 下载成功，平台 `error=null`。微博 ID `5336862822502143` 实测约 18.77 秒返回 16 个候选，Top 4 全部属于指定作品并下载成功。当前 Python 测试为 61/61 通过。

结论：第一阶段的抖音、微博两平台均已覆盖 ID/URL 与关键词两种入口，并达到候选非空、平台无错误、实际下载成功和人工检查四项试用标准。微博关键词 Top 3 中前两张是强相关室内空间，第三张是中等相关室内空间；不要把关键词结果理解成指定场所的绝对保证。X、Instagram 仍未纳入本阶段验收。

建议：新建一个 Codex 任务后，直接用自然语言调用 `social-image`，先试抖音作品 ID，再试微博关键词。试用期间若发现错图，保留返回的候选 ID 和图片路径，下一阶段用 `submit_feedback` 做针对性纠偏；不要手动向 PowerShell 输入 MCP JSON，也不要在旧任务里继续调用已经关闭的连接。

### 阶段 17：博主账号续传与失败透明度

目标：让 `fetch_creator_images` 在真实平台受限时快速回退，并让断点续传的累计统计、来源状态和验收结果保持一致。

实际证据：新增 `scripts\\verify_creator.py`，通过同一 stdio launcher 检查工具发现、账号身份、作品数量、下载状态以及 `semantic_applied=false`/`vision_applied=false`。creator 来源现在遵循平台级熔断和失败账本：抖音 dy-cli 超时或 HTTP 403 后会记录失败并尝试 MediaCrawler；来源返回非法 JSON、身份信封缺失或编码错误也会记录到 `list_sources()`，不会无限重启同一坏来源。SQLite 断点新增累计 `posts_fetched`、`post_ids`、`pages_fetched`、`rejected_posts` 和 warnings，旧断点仍可继续使用。Windows UTF-8/GB18030 输出均可读，自动化测试现为 73 项全部通过。

结论：账号路径的算法规则没有改变：只做账号身份校验、原图下载、尺寸/哈希去重，不启动文本语义或 CLIP。最近一次抖音主页复验明确返回 dy-cli HTTP 403 和 MediaCrawler 超时，微博 UID `5336862822502143` 当前接口返回“这里还没有内容”；服务会报告这些平台访问状态，不把旧缓存或相似账号冒充成功。此前已完成的抖音与微博真实下载记录仍保留在 `downloads/`，可用于对照验收。

建议：试用账号时先运行 `verify_creator.py --no-download` 确认 `identity`、`item_count` 和 `error=null`，再加 `--download`。抖音优先使用完整主页 `sec_uid`；微博必须使用当前可访问且有公开内容的数字 UID 或主页链接。若出现 `creator_source_error`，先看 `failure_reasons` 和 `list_sources()` 的最后错误，不要重复提交同一个被平台拒绝的账号请求。

### 阶段 18：账号内容对象筛选

目标：账号主页默认快速下载；用户提出内容要求时，再按人物、穿搭、风景、场景、建筑、物品和身体部位做筛选。

结论：已完成结构化 `content_spec`、include/exclude/required 规则、主体面积阈值、可选/严格失败策略和对象决策明细。对象后端支持本地检测/分割模型，也支持复用本地 CLIP 做粗粒度类别对比；当前本机 CLIP 缓存已验证可以启动。默认账号请求不会触发任何视觉或对象判断。

边界：CLIP 分类能区分“人物穿搭”和“风景/纯场景”等大类，但不是像素级实例分割。要精确判断衣服区域、腿部、鞋子或主体占比，应配置专门的人体解析、服装检测或分割权重，并用 `OBJECT_LABEL_MAP` 映射标签。没有模型时，`filter_mode=optional` 会保留账号结果并标记 warning，`required` 会返回 `content_filter_required`，避免把未验证图片当成合格结果。

### 阶段 19：抖音号/昵称与视频媒体

目标：让用户可以直接提供抖音号、数字 UID 或昵称，不必先复制主页链接；同时支持从同一账号分页获取原视频。

结论：`creator_id` 支持抖音号、UID、sec_uid；中文昵称既可放在 `creator_name`，也可直接放在 `creator_id` 自动识别。昵称会经过精确匹配，命中多个账号时返回明确错误，不会猜测。`media_type=images` 获取图片，`media_type=videos` 获取原视频，`media_type=all` 获取两者；视频使用封面参与筛选，原地址交给视频下载校验，断点续传会区分图片和视频。

边界：视频地址是否可用取决于抖音当前登录态和平台返回的播放权限；服务不会把视频封面冒充原视频。视频与图片共用账号身份校验、分页和内容筛选，但下载校验规则不同。

### 阶段 20：代码审计与抓取性能优化

目标：清理重复逻辑，缩短账号和关键词请求的等待，避免单个来源或视觉模型拖住整个 MCP 进程。

已完成：

- 抖音账号抓取默认只走 dy-cli；MediaCrawler 回退改为显式开关 `DOUYIN_MEDIA_CRAWLER_FALLBACK=true`。dy-cli 自带浏览器回退时，不会再无条件串行等待第二套浏览器链路。
- 抖音作品分页等待默认降至 `0.25s`，浏览器轮询间隔统一为可配置的 `DOUYIN_BROWSER_POLL_MS=400`；MediaCrawler 分页等待同样降至 `0.25s`。
- 视觉输入增加 64 张、5 分钟本地图片缓存，并限制图片请求并发为 8；视频候选只请求封面，不再误下载 MP4 进行图片解析。
- 账号内容对象筛选增加独立超时 `OBJECT_FILTER_TIMEOUT_SECONDS=4`。超时会快速返回可解释 warning；`required` 模式失败关闭，`optional` 模式保留账号结果。
- 关键词请求仍会后台预热 CLIP，但冷模型未就绪时直接返回规则/文本排序，不再同步等待视觉模型冷启动；视觉就绪后的后续请求才启用视觉重排，并在 `retrieval.vision_deferred` 标记本次是否延期。
- 可选语义模型同样受 `SEMANTIC_TIMEOUT_SECONDS=4` 预算保护；冷启动降级后，缓存命中会在模型已就绪时补做排序，避免重复访问平台或长期复用旧顺序。
- 删除未使用 import、未使用数据类和变量，修正未启动服务时来源状态遗漏抖音回退配置的问题。

验证：当前自动化测试为 98 项全部通过；`pyflakes src scripts tests`、全量 Python 编译和 `git diff --check` 均通过。上述是代码级和本机模拟验证，不等同于平台实时抓取成功；抖音/微博是否能返回内容仍取决于当前登录态、账号可见性和平台风控。

## 典型调用

```json
{
  "query": "极简风咖啡店室内，横图，不要水印",
  "platforms": ["xhs", "instagram", "x"],
  "max_results": 24,
  "safe_mode": true,
  "min_width": 1200,
  "download": true,
  "retrieval_mode": "sources"
}
```

`search_images` 返回的 `items` 可以原样传给 `download_images`。下载结果包含本地路径、最终 URL、尺寸、哈希和失败原因，便于后续工作流继续处理。

博主账号调用 `fetch_creator_images`，例如：

```json
{
  "platform": "douyin",
  "creator_id": "Gracebb0722",
  "max_posts": 20,
  "max_images": 50,
  "download": true,
  "resume": true
}
```

抖音也接受完整主页链接 `https://www.douyin.com/user/<sec_uid>`，主页链接比抖音号更稳定；微博使用数字 UID 或 `https://weibo.com/u/<uid>`。返回的 `identity.canonical_id` 是平台确认后的账号 ID，图片记录都带有 `creator_id`、`post_id` 和 `media_index`。需要继续分页时，使用相同参数和 `resume=true` 即可；续传返回的 `posts_fetched`、`post_ids`、`pages_fetched` 是该断点任务的累计值。

可以用项目自带脚本先验收完整 stdio 链路。脚本会检查账号身份、作品数量、下载状态，并明确显示 `semantic_applied=false` 和 `vision_applied=false`：

```powershell
# 抖音号：账号搜索偶尔会被 verify_check，主页 sec_uid 更稳定
python .\scripts\verify_creator.py `
  --platform douyin `
  --profile-url "https://www.douyin.com/user/MS4wLjABAAAAknTG_PwzN_7afuDqrpAumVE6CfRFrL0NUTccuyUcxSI" `
  --max-posts 5 --max-images 10 --download

# 也可以直接使用抖音号，但需要 dy-cli 登录态
python .\scripts\verify_creator.py --platform douyin --creator-id Gracebb0722 --max-posts 5 --max-images 10 --download

# 直接使用昵称抓取原视频
python .\scripts\verify_creator.py --platform douyin --creator-name "放学小野猪" --media-type videos --max-posts 5 --max-images 5 --download

# 微博：把 1234567890 换成你要访问的数字 UID
python .\scripts\verify_creator.py --platform weibo --creator-id 1234567890 --max-posts 5 --max-images 10 --download
```

脚本退出码为 `0` 才表示本次候选和下载均成功；退出码为 `2` 时，查看输出中的 `error`、`warnings` 和 `failure_reasons`。这是一次真实访问验收，不会把旧缓存或相似账号当成成功。

## 合规与稳定性

### 并发抓取和冷却时间

多个 `fetch_creator_images` 请求可以并发执行。服务按“平台 + 账号标识”维护账号任务锁：同一个账号的重复请求会排队，避免同时写入同一个断点文件；不同账号会同时运行，互不等待。下载阶段仍受 `max_concurrency` 控制，防止单个账号一次打开过多连接。

冷却时间由 `SOURCE_FAILURE_COOLDOWN_SECONDS` 控制，默认 120 秒。某个来源刚刚遇到限流、验证码、登录失败、超时或无效响应时，系统会在这段时间内跳过同一来源的重试并立即返回上次错误。账号抓取的冷却键包含账号标识，因此一个账号失败不会阻止同平台的其他账号；关键词搜索仍按平台来源单独冷却。设置为 `0` 可关闭失败冷却，但高频重试可能再次触发平台风控。

只接入你有权访问的数据和账号，遵守各平台条款、robots/速率限制与版权要求。服务不会绕过验证码、登录保护或风控；没有凭据或网关时，对应平台会返回清晰的 `unavailable` 状态，而不会静默返回低质量网页结果。
