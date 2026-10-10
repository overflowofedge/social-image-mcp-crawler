import importlib.util
import asyncio
import json
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
import threading
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import pytest


_SPEC = importlib.util.spec_from_file_location(
    "app_server",
    Path(__file__).resolve().parents[1] / "scripts" / "app_server.py",
)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
_preview_referer = _MODULE._preview_referer
_creator_hint = _MODULE._creator_hint
_error_guidance = _MODULE._error_guidance
_task_report = _MODULE._task_report
_request_timeout = _MODULE._request_timeout


@contextmanager
def running_server(handler):
    with _MODULE.LocalHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join(timeout=2)


def test_desktop_port_cannot_be_shared_with_another_app_instance():
    with _MODULE.LocalHTTPServer(("127.0.0.1", 0), _MODULE.Handler) as server:
        with pytest.raises(OSError):
            with ThreadingHTTPServer(server.server_address, _MODULE.Handler):
                pass


@pytest.mark.parametrize("no_browser", [False, True])
def test_repeated_launch_reuses_running_desktop_without_starting_workers(monkeypatch, capsys, no_browser):
    opened = []
    monkeypatch.setattr(_MODULE.webbrowser, "open", opened.append)
    monkeypatch.setattr(_MODULE, "_Loop", lambda: pytest.fail("Duplicate launch started workers"))
    with running_server(_MODULE.Handler) as server:
        port = server.server_port
        arguments = ["app_server.py", "--port", str(port)]
        if no_browser:
            arguments.append("--no-browser")
        monkeypatch.setattr(_MODULE.sys, "argv", arguments)
        _MODULE.main()
        _MODULE.main()
        url = f"http://127.0.0.1:{port}/"
        assert opened == ([] if no_browser else [url, url])
        assert _MODULE._is_running_app(url)
    assert capsys.readouterr().out.count("应用已在运行") == 2


@pytest.mark.parametrize("identity", [
    {"app": "another-program", "root": str(_MODULE.ROOT)},
    {"app": _MODULE.APP_ID, "root": str(_MODULE.ROOT / "another-installation")},
])
def test_occupied_port_never_opens_another_program_or_installation(monkeypatch, identity):
    class OtherHandler(_MODULE.Handler):
        def do_GET(self):
            self._send(200, identity)

    monkeypatch.setattr(_MODULE.webbrowser, "open", lambda *_: pytest.fail("Opened another app"))
    monkeypatch.setattr(_MODULE, "_Loop", lambda: pytest.fail("Started workers on occupied port"))
    with running_server(OtherHandler) as server:
        monkeypatch.setattr(_MODULE.sys, "argv", ["app_server.py", "--port", str(server.server_port)])
        with pytest.raises(SystemExit, match="端口 .* 不可用"):
            _MODULE.main()


def test_preview_referer_uses_weibo_page_for_sina_image_hosts():
    assert _preview_referer("https://wx2.sinaimg.cn/mw2000/a.jpg") == "https://m.weibo.cn/"


def test_preview_referer_preserves_item_permalink():
    assert _preview_referer(
        "https://wx2.sinaimg.cn/mw2000/a.jpg",
        "https://m.weibo.cn/detail/123",
    ) == "https://m.weibo.cn/detail/123"


def test_desktop_form_treats_bilibili_nickname_as_creator_name():
    assert _creator_hint("机械人读书笔记", "bilibili", None) == ("机械人读书笔记", None)
    assert _creator_hint("Tech World", "bilibili", None) == ("Tech World", None)
    assert _creator_hint("288159073", "bilibili", None) == (None, "288159073")


def test_implicit_creator_failure_always_retries_as_keyword(monkeypatch):
    calls = []

    async def fake_search(query, **kwargs):
        calls.append((kwargs.get("creator_name"), kwargs.get("creator_id")))
        if kwargs.get("creator_name") or kwargs.get("creator_id"):
            return {"items": [], "error": {"code": "adapter_error", "message": "verify_check"}}
        return {"items": [{"id": "keyword-result", "media_type": "image"}]}

    class Runner:
        @staticmethod
        def call(awaitable, timeout=None):
            return asyncio.run(awaitable)

    activity = []
    monkeypatch.setattr(_MODULE, "search_images", fake_search)
    result = _MODULE._execute_search({
        "query": "Vinan", "platforms": ["douyin"], "max_results": 1,
        "media_type": "images", "image_limit": 1, "max_posts": 1,
        "download": False,
    }, Runner(), activity.append)

    assert calls == [(None, "Vinan"), (None, None)]
    assert result["items"][0]["id"] == "keyword-result"
    assert any("关键词检索" in event["message"] for event in activity)


def test_task_report_explains_when_image_settings_cannot_reach_target():
    report = _task_report(
        {
            "platforms": ["douyin"], "media_type": "images", "image_limit": 20,
            "per_post_limit": 3, "max_posts": 2, "download": False,
        },
        {"items": [{"id": str(index), "media_type": "image"} for index in range(6)]},
        1.25,
    )

    assert report["requested"]["images"] == 20
    assert report["found"]["images"] == 6
    assert report["shortfalls"]["images"] == 14
    assert any("理论最多只能取得 6 张图片" in issue["reason"] for issue in report["issues"])
    assert any("最多检索作品数" in issue["action"] for issue in report["issues"])


def test_bilibili_wbi_failure_has_plain_language_guidance():
    reason, action = _error_guidance("WBI API error -352", "bilibili")

    assert "安全验证" in reason
    assert "平台登录" in action and "直接重试" in action


def test_missing_query_guidance_tells_user_what_to_enter():
    reason, action = _error_guidance("请输入搜索提示词或链接", "douyin")

    assert reason == "没有填写要采集的账号或网址。"
    assert "账号昵称" in action
    assert "主页链接" in action


def test_douyin_short_link_parse_failure_is_not_reported_as_expired_login():
    reason, action = _error_guidance(
        "dy-cli 抖音请求失败: 无法从链接提取视频 ID: https://v.douyin.com/-cHq0KkO5uE/",
        "douyin",
    )

    assert "短链接" in reason
    assert "登录状态" not in reason
    assert "douyin.com/user" in action


def test_task_report_separates_each_download_outcome():
    statuses = ["downloaded", "existing", "duplicate", "rejected", "failed"]
    result = {
        "items": [{"id": status, "media_type": "image"} for status in statuses],
        "downloads": [
            {
                "candidate_id": status, "media_type": "image", "status": status,
                **({"error": "network connection failed"} if status == "failed" else {}),
            }
            for status in statuses
        ],
    }
    report = _task_report(
        {"platforms": ["weibo"], "media_type": "images", "image_limit": 5, "download": True},
        result,
        0.5,
    )

    assert {name: report["downloads"][name]["images"] for name in statuses} == {
        name: 1 for name in statuses
    }
    assert report["successful"]["images"] == 2
    assert any("新下载 1 个，已存在 1 个，重复跳过 1 个" in log["message"] for log in report["logs"])
    assert any("网络连接失败" in issue["reason"] for issue in report["issues"])


def test_task_report_explains_incremental_run_with_no_new_files():
    report = _task_report(
        {
            "platforms": ["douyin"], "media_type": "videos", "video_limit": 3,
            "max_posts": 10, "download": True,
        },
        {"items": [], "downloads": [], "refresh_count": 2, "post_ids": ["old-post"]},
        0.2,
    )

    assert any("之前已经处理过" in issue["reason"] for issue in report["issues"])
    assert any("等待账号发布新作品" in issue["action"] for issue in report["issues"])


def test_task_report_uses_current_run_counts_and_explains_pagination_end():
    report = _task_report(
        {
            "platforms": ["douyin"], "media_type": "images", "image_limit": 5,
            "max_posts": 20, "download": False,
        },
        {
            "items": [], "posts_fetched": 90, "pages_fetched": 9,
            "current_posts_fetched": 12, "current_pages_fetched": 2,
            "current_pagination_stop_reason": "source_exhausted",
        },
        0.2,
    )

    assert any("本次检索：读取 2 页，检查 12 个作品" in log["message"] for log in report["logs"])
    assert any("平台未返回下一页游标" in log["message"] for log in report["logs"])


def test_large_task_report_explains_cli_rate_limit_risk():
    report = _task_report(
        {
            "platforms": ["bilibili"], "media_type": "videos", "video_limit": 500,
            "max_posts": 500, "download": False,
        },
        {"items": []},
        0.1,
    )

    warning = next(log for log in report["logs"] if "大批量任务" in log["message"])
    assert warning["level"] == "warning"
    assert "CLI 仍受平台访问频率" in warning["action"]


def test_large_video_job_gets_a_dynamic_desktop_timeout():
    timeout = _request_timeout({
        "media_type": "videos", "video_limit": 50, "max_posts": 50,
    })

    assert timeout > 300
    assert timeout <= 7200


def _read_task(server, task_id):
    with urlopen(f"http://127.0.0.1:{server.server_port}/api/tasks/{task_id}", timeout=3) as response:
        return json.loads(response.read())


def _wait_for_terminal_task(server, task_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        task = _read_task(server, task_id)
        if task["state"] in {"completed", "failed"}:
            return task
        time.sleep(0.01)
    raise AssertionError("desktop task did not finish")


def test_search_http_returns_task_immediately_and_exposes_running_progress():
    class StubRunner:
        started = threading.Event()
        release = threading.Event()

        @classmethod
        def call(cls, awaitable, timeout=None):
            awaitable.close()
            cls.started.set()
            assert cls.release.wait(timeout=2)
            return {"items": [], "downloads": []}

    previous_runner = getattr(_MODULE.Handler, "runner", None)
    previous_tasks = _MODULE.Handler.tasks
    _MODULE.Handler.runner = StubRunner()
    _MODULE.Handler.tasks = _MODULE.TaskRegistry()
    try:
        with running_server(_MODULE.Handler) as server:
            request = Request(
                f"http://127.0.0.1:{server.server_port}/api/search",
                data=json.dumps({
                    "query": "测试账号", "platforms": ["douyin"],
                    "media_type": "images", "image_limit": 3,
                    "per_post_limit": 3, "max_posts": 1, "download": True,
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=3) as response:
                assert response.status == 202
                payload = json.loads(response.read())
            assert payload["task_id"]
            assert StubRunner.started.wait(timeout=1)
            running = _read_task(server, payload["task_id"])
            assert running["state"] == "running"
            assert running["stage"] == "preparing"
            StubRunner.release.set()
            task = _wait_for_terminal_task(server, payload["task_id"])
    finally:
        StubRunner.release.set()
        _MODULE.Handler.tasks = previous_tasks
        if previous_runner is None:
            delattr(_MODULE.Handler, "runner")
        else:
            _MODULE.Handler.runner = previous_runner

    assert task["state"] == "completed"
    assert task["result"]["task_report"]["status"] == "partial"
    assert task["result"]["task_report"]["requested"]["images"] == 3
    assert any("实际可用数量比目标少 3 张图片" in log["message"] for log in task["result"]["task_report"]["logs"])


def test_search_task_failure_is_visible_through_progress_endpoint():
    class FailingRunner:
        @staticmethod
        def call(awaitable, timeout=None):
            awaitable.close()
            raise RuntimeError("source unavailable")

    previous_runner = getattr(_MODULE.Handler, "runner", None)
    previous_tasks = _MODULE.Handler.tasks
    _MODULE.Handler.runner = FailingRunner()
    _MODULE.Handler.tasks = _MODULE.TaskRegistry()
    try:
        with running_server(_MODULE.Handler) as server:
            request = Request(
                f"http://127.0.0.1:{server.server_port}/api/search",
                data=json.dumps({"query": "测试账号", "platforms": ["douyin"]}).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urlopen(request, timeout=3) as response:
                payload = json.loads(response.read())
            task = _wait_for_terminal_task(server, payload["task_id"])
    finally:
        _MODULE.Handler.tasks = previous_tasks
        if previous_runner is None:
            delattr(_MODULE.Handler, "runner")
        else:
            _MODULE.Handler.runner = previous_runner

    assert task["state"] == "failed"
    assert task["result"]["error"]["message"] == "source unavailable"
    assert task["activity"][-1]["level"] == "error"


@pytest.mark.parametrize("platform", ["douyin", "weibo", "xhs", "bilibili", "x", "instagram"])
def test_login_http_is_async_deduplicated_and_can_be_cancelled(monkeypatch, tmp_path, platform):
    from scripts.account_login import LoginManager
    monkeypatch.setenv(f"{platform.upper()}_BROWSER_STORAGE_STATE", str(tmp_path / "session.json"))
    monkeypatch.delenv(f"{platform.upper()}_GALLERY_DL_COOKIES_FILE", raising=False)
    waiting = threading.Event()

    async def login(platform, root, cancel, update):
        update("waiting", "等待手机确认")
        waiting.set()
        while not cancel.is_set():
            await asyncio.sleep(0.01)
        raise asyncio.CancelledError()

    manager = LoginManager(tmp_path, login)
    monkeypatch.setattr(_MODULE.Handler, "logins", manager)
    with running_server(_MODULE.Handler) as server:
        base = f"http://127.0.0.1:{server.server_port}"

        def post(path, data, origin=None):
            headers = {"Content-Type": "application/json"}
            if origin:
                headers["Origin"] = origin
            request = Request(base + path, data=json.dumps(data).encode(), headers=headers, method="POST")
            with urlopen(request, timeout=2) as response:
                return response.status, json.loads(response.read())

        status, first = post("/api/login", {"platform": platform}, base)
        assert status == 202 and waiting.wait(1)
        _, repeated = post("/api/login", {"platform": platform})
        assert first["task_id"] == repeated["task_id"]
        with urlopen(base + "/api/login") as response:
            platforms = json.loads(response.read())["platforms"]
        assert {row["platform"] for row in platforms} == {"douyin", "weibo", "xhs", "bilibili", "x", "instagram"}
        assert next(row for row in platforms if row["platform"] == platform)["state"] == "waiting"
        with pytest.raises(HTTPError) as error:
            post("/api/login", {"platform": platform}, "https://untrusted.test")
        assert error.value.code == 403
        with pytest.raises(HTTPError) as error:
            post("/api/login", {"platform": "unsupported"})
        assert error.value.code == 400
        post("/api/login/cancel", {"platform": platform})
    manager.close()
    assert manager.status(platform)["state"] == "cancelled"
