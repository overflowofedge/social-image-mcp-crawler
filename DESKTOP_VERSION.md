# 桌面版安装与使用

桌面版是一个本机运行的 Windows 网页应用，支持抖音、小红书、微博、B 站、X、Instagram 和自定义网页，默认只监听 `127.0.0.1:8765`。它不需要打开 Codex、配置 MCP 客户端或手动输入 JSON，下载的文件默认保存到项目根目录的 `downloads` 文件夹。

## 功能与数量

- 输入关键词、昵称、UID、主页链接或作品链接；抖音、微博和 B 站支持账号身份确认。
- 单类图片或视频最多设置 1000 个，两类合计最多 2000 个，最多检索 500 个作品。默认数量仍是 20，大批量任务通过分页和增量续传执行。
- 图片和视频分别计数，每个作品的图片上限不会占用视频配额。
- 登录成功后复用本机持久 Cookie；仅在 Cookie 缺失、损坏、缺少认证字段或已过期时重新登录，普通空结果和临时接口故障不会清除登录态。
- 视频默认按 3 个一批、单并发顺序下载；一批完成后才开始下一批。失败、重复或拒绝项由后续候选补足，直到达到视频上限或遍历完最大作品数。
- 文件按 `平台/<images|videos>` 分类，名称由时间戳、作品号和作品名组成；再次采集相同账号时跳过已经处理的作品与重复文件。
- 运行日志显示预设数量、实际找到数量和每种保存状态；没有达到目标或发生错误时，会显示中文原因与可操作的处理建议。

数量是目标上限，并不保证平台一定返回足量内容。CLI 仍然使用平台接口、登录 Cookie 和当前网络，可能遇到验证码、限流或登录失效；程序会保留已完成文件，用户可按日志处理后继续采集。

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

4. 只使用“其他平台”时可以直接启动。需要抖音时，先安装 `third_party\dy-cli`，尚未配置浏览器时执行 `.\.venv\Scripts\python.exe -m playwright install chromium`，然后运行 `scripts\douyin_login.ps1`；需要抖音、小红书、微博或 B 站时，运行 `powershell -ExecutionPolicy Bypass -File .\scripts\install_desktop.ps1 -InstallSources`，安装来源及桌面桥接依赖。四个平台的检索和账号采集都通过独立 CLI 子进程运行，首次采集时按浏览器提示完成登录。

5. X 与 Instagram 使用各自独立的 gallery-dl 浏览器会话。按需要分别运行以下命令，并在打开的对应平台窗口完成官方登录：

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\scripts\setup_account.ps1 -Platform x
   powershell -ExecutionPolicy Bypass -File .\scripts\setup_account.ps1 -Platform instagram
   ```

   两个平台的 Cookie、浏览器配置和失败冷却分别保存；一个平台登录失效或限流不会覆盖、阻塞另一个平台。

## 启动

双击 `启动应用.bat`。启动前会自动检查 Python 依赖、Playwright 浏览器、来源桥接和本机持久 Cookie。默认不额外请求抖音接口，避免每次启动增加风控频率；即使手动启用动态检查且平台临时返回验证，也只会标记抖音状态，不会阻止桌面版和其它平台启动。详细结果保存在 `.cache\preflight-latest.json`。检查通过后浏览器打开 `http://127.0.0.1:8765/` 即可使用。

如果旧版启动时出现 `init_import_site` / `UnicodeDecodeError`，更新后直接双击启动即可。程序会在 Python 加载依赖前修复中文安装路径的编码，并备份原路径文件；不需要删除 `.venv`、`.env`、登录缓存或已下载的文件。安装脚本也会执行同样的修复。

如果能启动但采集提示 `No module named 'playwright'`，更新后重新运行 `安装桌面版.bat`，补齐当前 `.venv` 的浏览器组件。安装到系统 Python 的组件不会自动用于桌面版虚拟环境。

## 使用

- 直接填写关键词、昵称、账号或完整网址，程序自动识别入口。
- 选择“其他平台”并粘贴主页地址时，会提取正文图片和直接视频文件，并按“最大作品数”访问同站内容卡片的详情页（只检索一层）。支持懒加载、原图链接、响应式图片和结构化媒体；自动过滤导航、Logo、图标以及宽高不足 160 像素的小图。
- 图片和视频可同时采集，分别计数；每篇图片上限不影响视频。动态内容会尝试浏览器加载，需已安装 Chromium 或配置 `WEBPAGE_BROWSER_PATH`。HLS/DASH 分段视频和只有嵌入式播放器的页面目前会明确提示限制。
- 图片模式不要求填写视频数量；只有视频或全部模式才填写视频数量。
- 点击开始采集后，页面会按作品类型列出图片、视频和混合作品清单；所有下载统一保存到 `downloads/<platform>/<account-or-nickname>/<images|videos>`，并在账号目录生成 `<account-or-nickname>.md` 清单。清单使用“日期 + 作品标题 + 文件名”的一行一文件格式，视频清单可按文件名中的 BV 号或作品 ID核验；下载校验和断点续传使用同目录的 `manifest.jsonl`。命名为 `时间戳_作品号_作品名`；重复采集会读取作品清单和 `manifest.jsonl`，只下载新增作品，已校验的文件显示为已存在。微博图片通过本地预览代理加载，避免浏览器跨域或 Referer 导致破图。
- 页面中的数量为目标上限。任务完成后，日志会分别列出找到、新下载、已存在、重复、拒绝和失败数量，并针对理论容量不足、Cookie 失效、平台验证、超时或内容筛选给出处理建议。

桌面版与 MCP 使用同一套采集、筛选、下载和断点逻辑，但桌面版日常使用不要求安装或注册 MCP 客户端。
