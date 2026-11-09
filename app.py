from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import threading
import webbrowser

from workbench.server import create_server


def acquire_instance(directory):
    directory.mkdir(parents=True, exist_ok=True)
    handle = (directory / "server.lock").open("a+b")
    if handle.seek(0, 2) == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise SystemExit("该数据目录已有工作台在运行，请使用已有窗口或指定不同的 --data-dir。") from None
    return handle


def main():
    arguments = argparse.ArgumentParser(description="Java 说明工作台 — Swagger / JavaDoc")
    arguments.add_argument("--port", type=int, default=8765)
    arguments.add_argument("--data-dir", help="任务和备份存储目录，默认用户本地应用数据目录")
    arguments.add_argument("--no-browser", action="store_true")
    args = arguments.parse_args()
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    default_data = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share"))) / "JavaDescriptionWorkbench"
    data_dir = Path(args.data_dir).resolve() if args.data_dir else default_data
    instance = acquire_instance(data_dir)
    server = None
    for port in range(args.port, min(args.port + 20, 65536)):
        try:
            _, server = create_server(data_dir, base / "static", port)
            break
        except OSError:
            continue
    if server is None:
        raise SystemExit("端口被占用，请使用 --port 指定其他端口")
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"Java 说明工作台 1.1.1\n本机地址：{url}\n任务与备份：{data_dir}\n关闭此窗口可退出程序。", flush=True)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        instance.close()


if __name__ == "__main__":
    main()
