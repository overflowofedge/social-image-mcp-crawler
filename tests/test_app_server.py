import importlib.util
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
import threading

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
