# 桌面版安装与使用

桌面版是一个本机运行的 Windows 网页应用，默认只监听 `127.0.0.1:8765`。它不需要打开 Codex、配置 MCP 客户端或手动输入 JSON，下载的文件默认保存到项目根目录的 `downloads` 文件夹。

## 安装

1. 安装 Python 3.10 或更高版本，并在安装时勾选 **Add Python to PATH**。
2. 下载 GitHub 的 `desktop` 分支 ZIP 并解压，或使用 Git 获取桌面版：

   ```powershell
   git clone -b desktop https://github.com/wozhendemeiyou/social-image-mcp-crawler.git
   cd social-image-mcp-crawler
   ```

3. 双击 `安装桌面版.bat`。它会自动创建 `.venv`、安装依赖和 Playwright，并生成 `.env`；如果已有 `third_party\dy-cli`，也会安装该来源的依赖。也可以执行：

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\scripts\install_desktop.ps1
   ```

4. 只使用“其他平台”时可以直接启动。需要抖音时，先安装 `third_party\dy-cli`，尚未配置浏览器时执行 `.\.venv\Scripts\python.exe -m playwright install chromium`，然后运行 `scripts\douyin_login.ps1`；需要微博或小红书时，运行 `powershell -ExecutionPolicy Bypass -File .\scripts\install_sources.ps1 -UseGit`，首次搜索时按浏览器提示登录。

## 启动

双击 `启动应用.bat`。启动前会自动检查 Python 依赖、Playwright 浏览器、来源桥接、抖音 Cookie，并执行一次轻量抖音链路检查；缺少 Chromium 会尝试安装，Cookie 缺失或过期会打开扫码登录流程。详细结果保存在 `.cache\preflight-latest.json`。检查通过后浏览器打开 `http://127.0.0.1:8765/` 即可使用。关闭启动它的 PowerShell 窗口即可停止应用。

如果旧版启动时出现 `init_import_site` / `UnicodeDecodeError`，更新后直接双击启动即可。程序会在 Python 加载依赖前修复中文安装路径的编码，并备份原路径文件；不需要删除 `.venv`、`.env`、登录缓存或已下载的文件。安装脚本也会执行同样的修复。

如果能启动但采集提示 `No module named 'playwright'`，更新后重新运行 `安装桌面版.bat`，补齐当前 `.venv` 的浏览器组件。安装到系统 Python 的组件不会自动用于桌面版虚拟环境。

## 使用

- 直接填写关键词、昵称、账号或完整网址，程序自动识别入口。
- 选择“其他平台”并粘贴主页地址时，会提取正文图片和直接视频文件，并按“最大作品数”访问同站内容卡片的详情页（只检索一层）。支持懒加载、原图链接、响应式图片和结构化媒体；自动过滤导航、Logo、图标以及宽高不足 160 像素的小图。
- 图片和视频可同时采集，分别计数；每篇图片上限不影响视频。动态内容会尝试浏览器加载，需已安装 Chromium 或配置 `WEBPAGE_BROWSER_PATH`。HLS/DASH 分段视频和只有嵌入式播放器的页面目前会明确提示限制。
- 图片模式不要求填写视频数量；只有视频或全部模式才填写视频数量。
- 点击开始采集后，图片会下载到 `downloads`，页面会显示采集数量、下载状态和缩略图。微博图片通过本地预览代理加载，避免浏览器跨域或 Referer 导致破图。

桌面版与 MCP 使用同一套采集、筛选、下载和断点逻辑，但桌面版日常使用不要求安装或注册 MCP 客户端。
