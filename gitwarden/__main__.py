"""入口：python -m gitwarden。单 worker 启动，调度器与 WebUI 同进程。"""

from __future__ import annotations

import logging
import sys

import uvicorn

from gitwarden.config import ConfigError, load_config
from gitwarden.web import create_app

_LOG = logging.getLogger("gitwarden")


def main() -> None:
    try:
        config = load_config()
    except ConfigError as exc:
        sys.exit(f"启动失败: {exc}")
    if config.host in ("0.0.0.0", "::"):
        _LOG.warning(
            "警告: 正在监听 %s:%s。在 --network host / network_mode: host 下会暴露"
            "宿主机所有网卡；在默认 bridge 网络下暴露面由端口映射（-p / GW_BIND）控制。"
            "设置 GW_HOST 或 [server] host 可收窄监听地址。容器内镜像默认"
            "GW_DEFAULT_HOST=0.0.0.0 保持不变。",
            config.host,
            config.port,
        )
    app = create_app(config)
    try:
        uvicorn.run(app, host=config.host, port=config.port, workers=1)
    except OSError as exc:
        sys.exit(f"启动失败: 无法绑定监听地址 {config.host}:{config.port}: {exc}")


if __name__ == "__main__":
    main()
