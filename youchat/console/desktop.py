"""Desktop UI：pywebview 包装 Web UI（复用同一套前端）。

后台线程起 Flask，前台开 pywebview 窗口。pywebview 缺失或运行异常时
降级为打开系统浏览器（web 模式），绝不阻塞用户。
"""
from __future__ import annotations

import threading
import webbrowser

from .controller import AppController


def main(controller: AppController, host: str = "127.0.0.1", port: int = 5173) -> None:
    from .web import create_app

    app = create_app(controller)
    threading.Thread(
        target=lambda: app.run(host=host, port=port, use_reloader=False),
        daemon=True,
    ).start()

    url = f"http://{host}:{port}"
    print(f"YOUchat Desktop 窗口启动中...（{url}）")
    try:
        import webview  # noqa: PLC0415

        webview.create_window("YOUchat", url, width=1000, height=700)
        webview.start()
    except Exception as e:  # noqa: BLE001
        print(f"pywebview 不可用（{e}），降级为浏览器打开。")
        webbrowser.open(url)
        # 保持进程存活（用户关浏览器窗口后 Ctrl+C 退出）
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
