# 社交媒体图片采集器｜桌面版

这是一个面向 Windows 的本地图片采集应用。它启动一个本机网页界面，不需要安装 Codex、配置 MCP 客户端，也不需要手动输入 JSON。程序只监听本机 `127.0.0.1`，下载文件默认放在项目目录的 `downloads` 文件夹。

## 安装

1. 安装 [Python 3.10+](https://www.python.org/downloads/)，安装时勾选 **Add Python to PATH**。
2. 从 GitHub 下载本仓库的 `desktop` 分支 ZIP，解压到本地目录。也可以执行：

   ```powershell
   git clone -b desktop https://github.com/wozhendemeiyou/social-image-mcp-crawler.git
   ```

3. 双击项目根目录的 **安装桌面版.bat**。它会自动创建 `.venv`、安装桌面版依赖和 Playwright，并生成本地 `.env`；若已有 `third_party\dy-cli`，也会安装该来源的依赖。不需要执行 MCP 注册命令。尚未配置浏览器时，按下面的抖音步骤安装 Chromium。

只使用“其他平台”网页图片提取，到这里即可开始使用。抖音、微博、小红书和 B 站等平台通过独立 CLI 来源运行，按下面对应平台配置登录态和来源项目即可。

### 抖音

```powershell
git clone https://github.com/Youhai020616/douyin.git third_party\dy-cli
.\.venv\Scripts\python.exe -m pip install -e .\third_party\dy-cli
.\.venv\Scripts\python.exe -m playwright install chromium
.\scripts\douyin_login.ps1 -Python .\.venv\Scripts\python.exe
```

### 微博和小红书

在项目目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_desktop.ps1 -InstallSources
```

安装脚本会同时安装 MediaCrawler 的桌面桥接依赖。抖音、小红书、微博和 B 站的检索、账号采集都通过独立 CLI 子进程运行；MediaCrawler 桥接按 `--platform` 隔离小红书/微博，抖音使用独立 dy-cli，B 站使用 `bilibili_cli_bridge.py`。首次采集时，程序会打开来源项目的浏览器窗口，请按提示完成官方登录。登录信息只保存在本机 `.cache`，不会提交到 Git。若微博昵称查询遇到 HTTP 432，可以输入完整主页链接重试；账号会由备用浏览器来源核验。配置 `BILIBILI_COOKIE` 可提高 B 站访问稳定性；X 和 Instagram 继续使用独立的 gallery-dl CLI 会话。

## 启动

双击 **启动应用.bat**。启动时会先检查 Python 依赖、Playwright 浏览器、来源桥接、抖音 Cookie，以及抖音和微博接口状态；缺少 MediaCrawler 桥接依赖会尝试修复。检查结果保存在 `.cache\preflight-latest.json`，微博浏览器登录状态需在首次采集时核验。通过后浏览器打开 `http://127.0.0.1:8765/`。关闭启动窗口即可停止应用。

## 使用

- 直接输入关键词、昵称、账号或完整网址，程序自动识别类型。
- 选择抖音、微博、X，或选择“其他平台”并粘贴主页地址。其他平台会优先提取正文、相册和内容卡片中的图片，并访问同站内容详情页寻找原图与视频；“最大作品数”限制访问的详情页数量，仅向下检索一层。
- 网页图片会过滤导航、Logo、头像、图标和统计像素；同一图片优先选择原图或最大的响应式版本，并校验真实尺寸（宽高至少 160 像素）。图片、视频分别按设置数量下载，每篇图片上限不会挤掉同篇视频。
- 网页视频支持 `<video>`、`<source>`、直接视频链接和结构化数据中的 MP4/WebM 等文件。动态页面会尝试浏览器加载；HLS/DASH 分段流、嵌入式播放器或无法访问的媒体会显示限制或错误，不会把封面作为视频下载。动态读取需要 Playwright 的 Chromium，或设置 `WEBPAGE_BROWSER_PATH` 为已安装浏览器的路径。
- 图片模式不需要填写视频数量；只有视频或全部模式才填写视频数量。
- 点击开始采集后，页面按图片作品、视频作品和混合作品列出清单；媒体文件按平台和类型保存到 `downloads/<platform>/<images|videos>`。文件名使用 `时间戳_作品号_作品名`，同一账号再次采集时只会加入新的作品，已存在且校验通过的文件会标记为 `existing`。

## 常见问题

- **提示找不到 Python**：重新安装 Python，并确认勾选了 **Add Python to PATH**。
- **启动提示 `init_import_site` 或 `UnicodeDecodeError`**：旧版在中文目录安装时可能写入了 GBK 路径，导致 UTF-8 启动失败。更新桌面版后双击 `启动应用.bat` 会自动修复，原路径文件会备份；无需删除 `.venv`、登录信息或下载文件。重新运行 `安装桌面版.bat` 也会先修复再安装。
- **采集提示 `No module named 'playwright'`**：旧版虚拟环境缺少浏览器采集组件。更新后重新运行 `安装桌面版.bat`，再重试采集；仅复制 `third_party` 文件夹不会把依赖安装进 `.venv`。抖音接口返回 403 时也需要这些组件才能尝试已登录浏览器采集。
- **页面打不开**：确认启动窗口仍在运行，或换一个端口执行 `powershell -ExecutionPolicy Bypass -File .\scripts\start_app.ps1 -Port 8766`。
- **平台显示未配置**：先运行 `install_sources.ps1 -UseGit`，再按平台完成登录；“其他平台”不依赖这些来源项目。

桌面版日常使用不需要 MCP 客户端。完整的桌面版说明见 `DESKTOP_VERSION.md`。
