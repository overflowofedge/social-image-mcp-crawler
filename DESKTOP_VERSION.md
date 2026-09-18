# 桌面版安装与使用

桌面版是一个本机运行的 Windows 网页应用，默认只监听 `127.0.0.1:8765`。它不需要打开 Codex，下载的文件默认保存到项目根目录的 `downloads` 文件夹。

## 安装

1. 安装 Python 3.10 或更高版本，并在安装时勾选 **Add Python to PATH**。
2. 将项目目录复制到本机，或使用 Git 获取项目：

   ```powershell
   git clone https://github.com/wozhendemeiyou/social-image-mcp-crawler.git
   cd 抖音、小红书、微博社交媒体图片MCP爬取器
   ```

3. 安装项目依赖：

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -U pip
   pip install -e .
   ```

4. 按需配置平台来源和登录态。抖音可运行 `powershell -ExecutionPolicy Bypass -File .\scripts\setup_account.ps1 -Platform douyin`；微博可运行同一脚本并把平台改为 `weibo`。其他平台按 `README.md` 中的配置说明启用。

## 启动

双击项目根目录的 `启动应用.bat`，或在 PowerShell 中执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start_app.ps1
```

浏览器打开 `http://127.0.0.1:8765/` 后即可使用。停止时关闭启动它的 PowerShell 窗口。

## 使用

- 在输入框直接填写关键词、昵称、账号或完整平台链接，程序会自动识别入口。
- 选择抖音、微博、X 或“其他平台”。选择“其他平台”时粘贴网页地址，程序会提取网页主图和图片链接。
- 图片模式不要求填写视频数量；只有选择视频或全部模式时才填写视频数量。
- 点击开始采集后，图片会下载到 `downloads`，页面会显示数量、状态和缩略图。微博图片通过本地预览代理加载，避免浏览器跨域或 Referer 导致破图。

桌面版与 MCP 使用同一套采集、筛选、下载和断点逻辑。完整参数、平台登录和故障排查请参阅 `README.md`。
