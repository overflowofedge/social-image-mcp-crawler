# 社交媒体图片采集器｜桌面版

这是一个面向 Windows 的本地社交媒体图片和视频采集应用，支持抖音、小红书、微博、B 站、X、Instagram 以及自定义网页。它启动一个本机网页界面，不需要安装 Codex、配置 MCP 客户端，也不需要手动输入 JSON。程序只监听本机 `127.0.0.1`，下载文件默认放在项目目录的 `downloads` 文件夹。

## 核心能力

- 输入关键词、账号昵称、UID、主页链接或作品链接进行采集；抖音、微博和 B 站账号会先确认身份，避免同名账号混入。
- 图片、视频可单独或同时下载。单类媒体最多设置 1000 个，图片和视频合计最多 2000 个，单次最多检索 500 个作品；默认仍为 20，实际数量取决于平台返回、内容筛选、重复文件和下载状态。
- 按图片、视频和混合作品生成作品清单；媒体按平台和类型分目录保存，文件名采用 `时间戳_作品号_作品名`。
- 同一账号再次采集时按作品和文件指纹增量去重，已经处理的旧作品不会重复下载。
- 完成日志分别显示目标、找到、新下载、已存在、重复、规则拒绝和失败数量。数量不足或任务失败时，会用中文说明原因并给出处理建议。
- 启动前自动检查 Python、浏览器组件、来源 CLI、Cookie 和平台链路；可修复的本地依赖问题会自动处理。

默认的 `master` 分支支持 MCP 服务和本地应用，`desktop` 分支面向桌面应用。两个分支使用相同的平台 CLI、安装修复和下载逻辑。普通用户按下面的桌面安装流程即可使用；MCP 客户端接入见文末。

## 安装

1. 安装 [Python 3.10+](https://www.python.org/downloads/)，安装时勾选 **Add Python to PATH**。
2. 从 GitHub 下载本仓库的 `desktop` 分支 ZIP，解压到本地目录。也可以执行：

   ```powershell
   git clone -b desktop https://github.com/overflowofedge/social-image-mcp-crawler.git
   ```

3. 双击项目根目录的 **安装桌面版.bat**。默认会创建 `.venv`，下载锁定版本的 dy-cli、MediaCrawler、XHS-Downloader，安装各平台依赖和 gallery-dl，并验证浏览器与实际导入。ZIP 下载用户不需要安装 Git。只有本地组件验证通过才显示安装完成；已有 `.env`、登录缓存、来源修改和下载文件会保留。仅使用普通网页时可运行 `powershell -ExecutionPolicy Bypass -File .\scripts\install_desktop.ps1 -WebOnly`。

安装完成后即可启动界面。安装检查不要求用户拥有任何平台账号，也不会自动打开抖音登录窗口。需要登录的平台按下面步骤分别配置；某个平台未登录不影响其它平台。登录态有效时会持续复用。

### 抖音

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup_account.ps1 -Platform douyin
```

### 微博和小红书

抖音也可以双击 **登录抖音.bat**。微博可以双击 **登录微博.bat**，或在项目目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup_account.ps1 -Platform weibo
```

各平台通过自己的 CLI 子进程运行；MediaCrawler 按 `--platform` 隔离微博和小红书，抖音使用 dy-cli，B 站使用 `bilibili_cli_bridge.py`。微博登录脚本会明确打开登录窗口，请完成官方登录。小红书首次使用需要自己的账号与平台登录；没有账号时可以跳过。登录信息保存在本机，不会提交到 Git。配置 `BILIBILI_COOKIE` 可提高 B 站访问稳定性。

### X 和 Instagram

X 与 Instagram 必须分别建立 gallery-dl 会话，不共用 Cookie、浏览器配置或失败冷却。在项目目录依次运行需要的平台命令，并在各自打开的独立浏览器窗口完成官方登录：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup_account.ps1 -Platform x
powershell -ExecutionPolicy Bypass -File .\scripts\setup_account.ps1 -Platform instagram
```

脚本会把 X 与 Instagram 的会话分别保存到 `.cache`，并写入各自的 `X_GALLERY_DL_*` / `INSTAGRAM_GALLERY_DL_*` 配置。一个平台登录过期、限流或采集失败时，不会改写或冷却另一个平台。

## 启动

双击 **启动应用.bat**。旧安装尚未补齐组件时会执行默认安装；之后启动自检默认只做本地检查，不请求平台接口、不要求登录。报告区分组件缺失、依赖异常、需要登录和组件就绪，保存在 `.cache\preflight-latest.json`。一个平台未登录或验证失败不阻止其它平台和界面启动。浏览器打开 `http://127.0.0.1:8765/` 后即可使用。

## 使用

- 直接输入关键词、昵称、账号或完整网址，程序自动识别类型。
- 选择抖音、小红书、微博、B 站、X、Instagram，或选择“其他平台”并粘贴主页地址。其他平台会优先提取正文、相册和内容卡片中的图片，并访问同站内容详情页寻找原图与视频；“最多检索作品数”限制访问的详情页数量，仅向下检索一层。
- 网页图片会过滤导航、Logo、头像、图标和统计像素；同一图片优先选择原图或最大的响应式版本，并校验真实尺寸（宽高至少 160 像素）。图片、视频分别按设置数量下载，每篇图片上限不会挤掉同篇视频。
- 网页视频支持 `<video>`、`<source>`、直接视频链接和结构化数据中的 MP4/WebM 等文件。动态页面会尝试浏览器加载；HLS/DASH 分段流、嵌入式播放器或无法访问的媒体会显示限制或错误，不会把封面作为视频下载。动态读取需要 Playwright 的 Chromium，或设置 `WEBPAGE_BROWSER_PATH` 为已安装浏览器的路径。
- 图片模式不需要填写视频数量；只有视频或全部模式才填写视频数量。
- 点击开始采集后，页面按图片作品、视频作品和混合作品列出清单；所有下载统一保存到 `downloads/<platform>/<account-or-nickname>/<images|videos>`。每个账号目录同时生成 `<account-or-nickname>.md` 清单，采用“日期 + 作品标题 + 文件名”的一行一文件格式，打开即可阅读和核对；视频清单会提示按文件名中的 BV 号或作品 ID 核验。下载校验和断点续传使用同目录的 `manifest.jsonl`，同一账号再次采集时只会加入新的作品，已存在且校验通过的文件会标记为 `existing`。
- 登录成功后会复用本机持久 Cookie；只有 Cookie 文件缺失、损坏、缺少认证字段或确已过期时才要求重新登录。关键词没有结果、接口暂时失败或触发平台验证不会自动清除登录态。
- 视频默认每批下载 3 个、并发 1 个；上一批完全结束后才开始下一批。下载失败、重复或被拒绝的候选不占成功数量，程序会继续从后续作品补足，直到达到视频上限或“最多检索作品数”已遍历完。可通过 `.env` 中的 `VIDEO_DOWNLOAD_BATCH_SIZE` 和 `VIDEO_DOWNLOAD_CONCURRENCY` 调整。
- 数量输入是目标上限，不是平台保证值。页面会提前提示“作品数 × 每作品图片数”的理论容量，任务结束后再说明平台实际返回、重复跳过、规则拒绝和下载失败造成的差额。

## 常见问题

- **提示找不到 Python**：重新安装 Python，并确认勾选了 **Add Python to PATH**。
- **启动提示 `init_import_site` 或 `UnicodeDecodeError`**：旧版在中文目录安装时可能写入了 GBK 路径，导致 UTF-8 启动失败。更新桌面版后双击 `启动应用.bat` 会自动修复，原路径文件会备份；无需删除 `.venv`、登录信息或下载文件。重新运行 `安装桌面版.bat` 也会先修复再安装。
- **采集提示 `No module named 'playwright'`**：旧版虚拟环境缺少浏览器采集组件。更新后重新运行 `安装桌面版.bat`，再重试采集；仅复制 `third_party` 文件夹不会把依赖安装进 `.venv`。抖音接口返回 403 时也需要这些组件才能尝试已登录浏览器采集。
- **页面打不开**：确认启动窗口仍在运行，或换一个端口执行 `powershell -ExecutionPolicy Bypass -File .\scripts\start_app.ps1 -Port 8766`。
- **抖音/微博来源未安装**：重新双击 `安装桌面版.bat`，默认会补齐来源与依赖；网络失败的详细原因见 `.cache/source-install-latest.json`。显示“需要登录”时再运行对应平台的登录脚本。
- **设置了数量但没有下载满**：查看“运行状态”中的找到数量和保存结果。按提示增加“最多检索作品数”或“每个作品最多张数”；若候选仍不足，说明平台当前可访问内容、筛选结果或去重后的文件少于目标。
- **使用 CLI 是否不会触发风控**：不会。CLI 用来隔离平台实现并稳定传递结构化结果，平台仍能识别账号、Cookie、IP 和访问频率。大批量任务会分页、限速并支持续传；出现验证码、403、412、429 或 Cookie 失效时，按日志建议重新登录或稍后重试。

桌面版日常使用不需要 MCP 客户端。完整的桌面版说明见 `DESKTOP_VERSION.md`。

## MCP 客户端接入

先完成上面的安装及需要的平台登录。客户端必须使用当前项目 `.venv` 中的 Python；来源命令为空时会自动定位当前项目的桥接脚本，无需手动修改示例路径。

支持 stdio MCP 的客户端可以使用以下配置，将 `<项目绝对路径>` 替换为解压目录：

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

可以先在项目目录检查工具发现与来源配置：

```powershell
.\.venv\Scripts\python.exe -m social_image_mcp.server --check
```

`--check` 显示配置状态，平台访问是否成功需要实际采集验证。stdio 服务由客户端启动，无需在终端输入 JSON。代码或登录配置更新后，重新连接客户端以加载最新版本。

提供的工具包括 `search_images`、`fetch_creator_images`、`inspect_item`、`download_images`、`list_platforms`、`list_sources` 和 `submit_feedback`。例如按账号采集图片和视频：

```json
{
  "platform": "douyin",
  "creator_id": "你的抖音号",
  "media_type": "all",
  "max_posts": 20,
  "max_images": 30,
  "max_videos": 10,
  "download": true,
  "resume": true
}
```

也可提供精确 `creator_name` 或完整 `profile_url`。账号采集默认只确认账号、分页、按质量下载与去重；提供 `content_query` 或 `content_spec` 才启用内容筛选。工具返回身份、作品计数、保存结果和错误原因；数量是目标上限，受实际可访问作品数和平台限制影响。

