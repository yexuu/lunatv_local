"""LunaTV-config 本地同步服务。

定时从 GitHub 上游 hafrey1/LunaTV-config 拉取三个版本的采集源配置
(jin18 纯净版 / jingjian 含成人版 / full 完整版)，本地缓存并同时
输出 MoonTV 与 TVBox 两种格式的订阅接口。

用法:
    1. (可选) 复制 .env.example 为 .env 并按需修改配置
    2. 安装依赖: pip install requests python-dotenv
    3. 运行: python lunatv_sync.py
    4. 订阅地址 (默认版本由 LUNATV_DEFAULT_SOURCE 指定, 默认 jin18):
       - MoonTV 默认版:  http://<部署机IP>:8899/config.json
       - TVBox 默认版:   http://<部署机IP>:8899/tvbox.json
       - MoonTV 指定版:  http://<部署机IP>:8899/config/full.json
       - TVBox 指定版:   http://<部署机IP>:8899/tvbox/jingjian.json
       - 同步状态:       http://<部署机IP>:8899/status
"""

import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # python-dotenv 未安装时仅使用系统环境变量, 不影响运行
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("lunatv_sync")

# 上游订阅文件名映射: 版本名 -> 仓库内文件名
SOURCE_FILES: Dict[str, str] = {
    "jin18": "jin18.json",
    "jingjian": "jingjian.json",
    "full": "LunaTV-config.json",
}
UPSTREAM_BASE = (
    "https://raw.githubusercontent.com/hafrey1/LunaTV-config/main/"
)
# 内置 GitHub 加速镜像前缀, 官方地址失败/超时后按序切换;
# 可用 LUNATV_MIRROR_PREFIX 环境变量覆盖(多个用英文逗号分隔)
DEFAULT_MIRROR_PREFIXES: Tuple[str, ...] = (
    "https://ghproxy.net/",
    "https://ghfast.top/",
    "https://gh-proxy.com/",
)
REQUEST_TIMEOUT = 15  # 秒, 单地址超时后即切换下一个候选地址
# 路由: /config/<版本>.json 或 /tvbox/<版本>.json
ROUTE_PATTERN = re.compile(r"^/(config|tvbox)/([a-z0-9]+)\.json$")


@dataclass
class ServiceConfig:
    """服务运行配置, 全部来自环境变量(或 .env)。"""

    default_source: str
    mirror_prefixes: Tuple[str, ...]
    port: int
    refresh_minutes: int
    data_dir: Path

    @classmethod
    def from_env(cls) -> "ServiceConfig":
        """从环境变量构造配置。

        Returns:
            ServiceConfig: 已校验的配置对象。

        Raises:
            ValueError: 当 LUNATV_DEFAULT_SOURCE 取值非法时。
        """
        default_source = os.getenv(
            "LUNATV_DEFAULT_SOURCE", "jin18"
        ).strip()
        if default_source not in SOURCE_FILES:
            raise ValueError(
                f"LUNATV_DEFAULT_SOURCE 非法: {default_source}, "
                f"可选值: {list(SOURCE_FILES)}"
            )
        mirror_env = os.getenv("LUNATV_MIRROR_PREFIX", "").strip()
        mirror_prefixes = tuple(
            p.strip() for p in mirror_env.split(",") if p.strip()
        ) or DEFAULT_MIRROR_PREFIXES
        return cls(
            default_source=default_source,
            mirror_prefixes=mirror_prefixes,
            port=int(os.getenv("LUNATV_PORT", "8899")),
            refresh_minutes=int(os.getenv("LUNATV_REFRESH_MINUTES", "360")),
            data_dir=Path(os.getenv("LUNATV_DATA_DIR", "data")),
        )

    def candidate_urls(self, source: str) -> Tuple[str, ...]:
        """返回指定版本的候选下载地址(官方优先, 加速镜像兜底)。

        Args:
            source: 版本名, 见 SOURCE_FILES。

        Returns:
            Tuple[str, ...]: 按尝试顺序排列的完整下载地址。
        """
        official = f"{UPSTREAM_BASE}{SOURCE_FILES[source]}"
        return (official,) + tuple(
            f"{prefix}{official}" for prefix in self.mirror_prefixes
        )


class ConfigStore:
    """线程安全的多版本配置缓存。"""

    def __init__(self, data_dir: Path) -> None:
        """初始化缓存并自动创建数据目录。

        Args:
            data_dir: 磁盘缓存目录。
        """
        self._lock = threading.Lock()
        # 结构: {版本名: {"moontv":..., "tvbox":..., "last_sync":...}}
        self._data: Dict[str, Dict[str, Any]] = {}
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)

    def _paths(self, source: str) -> Tuple[Path, Path]:
        """返回指定版本的两个缓存文件路径。"""
        return (
            self.data_dir / f"moontv_{source}.json",
            self.data_dir / f"tvbox_{source}.json",
        )

    def load_from_disk(self) -> int:
        """尝试从磁盘恢复各版本缓存, 供断网启动时兜底。

        Returns:
            int: 成功恢复的版本数量。
        """
        restored = 0
        for source in SOURCE_FILES:
            moontv_path, tvbox_path = self._paths(source)
            try:
                with open(moontv_path, encoding="utf-8") as f:
                    moontv = json.load(f)
                with open(tvbox_path, encoding="utf-8") as f:
                    tvbox = json.load(f)
            except (IOError, json.JSONDecodeError):
                continue
            with self._lock:
                self._data[source] = {
                    "moontv": moontv,
                    "tvbox": tvbox,
                    "last_sync": "restored-from-disk",
                }
            restored += 1
        if restored:
            logger.info("已从磁盘缓存恢复 %d 个版本", restored)
        else:
            logger.warning("磁盘缓存不可用, 等待首次同步")
        return restored

    def update(
        self,
        source: str,
        moontv: Dict[str, Any],
        tvbox: Dict[str, Any],
    ) -> None:
        """更新指定版本缓存并落盘(写入后回读校验)。

        Args:
            source: 版本名。
            moontv: MoonTV 格式配置。
            tvbox: TVBox 格式配置。

        Raises:
            IOError: 写入失败时。
            json.JSONDecodeError: 回读校验失败时。
        """
        moontv_path, tvbox_path = self._paths(source)
        for path, obj in (
            (moontv_path, moontv),
            (tvbox_path, tvbox),
        ):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, indent=2)
            with open(path, encoding="utf-8") as f:
                json.load(f)  # 回读校验, 损坏则抛 JSONDecodeError
        with self._lock:
            self._data[source] = {
                "moontv": moontv,
                "tvbox": tvbox,
                "last_sync": datetime.now().isoformat(
                    timespec="seconds"
                ),
            }

    def get(self, source: str, fmt: str) -> Optional[Dict[str, Any]]:
        """获取指定版本、指定格式的配置。

        Args:
            source: 版本名。
            fmt: "moontv" 或 "tvbox"。

        Returns:
            Optional[Dict[str, Any]]: 配置内容, 未同步时为 None。
        """
        with self._lock:
            entry = self._data.get(source)
            return entry.get(fmt) if entry else None

    def status(self) -> Dict[str, Any]:
        """返回各版本的同步状态摘要。"""
        with self._lock:
            return {
                source: {
                    "last_sync": entry.get("last_sync"),
                    "site_count": len(
                        entry.get("tvbox", {}).get("sites", [])
                    ),
                }
                for source, entry in self._data.items()
            }


def fetch_upstream(urls: Tuple[str, ...]) -> Dict[str, Any]:
    """依次尝试候选地址拉取上游 JSON, 首个成功即返回。

    官方地址在前、加速镜像在后, 单地址失败或超时(REQUEST_TIMEOUT)
    自动切换下一个候选地址。

    Args:
        urls: 按尝试顺序排列的候选地址。

    Returns:
        Dict[str, Any]: 解析后的 MoonTV 格式配置。

    Raises:
        RuntimeError: 全部候选地址均失败时。
    """
    last_error: Optional[Exception] = None
    for idx, url in enumerate(urls, start=1):
        try:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            if "api_site" not in data or not data["api_site"]:
                raise ValueError("上游返回内容缺少 api_site 字段")
            if idx > 1:
                logger.info("官方地址不可用, 已切换加速地址: %s", url)
            return data
        except (
            requests.exceptions.RequestException,
            ValueError,
        ) as exc:
            last_error = exc
            logger.warning(
                "候选地址失败(%d/%d), 切换下一个: %s (%s)",
                idx, len(urls), url, exc,
            )
    raise RuntimeError("全部候选地址拉取失败") from last_error


def convert_to_tvbox(moontv: Dict[str, Any]) -> Dict[str, Any]:
    """将 MoonTV 的 api_site 转为 TVBox 接口格式。

    苹果 CMS 的 provide/vod JSON 接口对应 TVBox 的 type=1 站点。

    Args:
        moontv: MoonTV 格式配置(含 api_site)。

    Returns:
        Dict[str, Any]: TVBox 格式配置(sites 列表)。
    """
    sites = []
    for key, site in moontv.get("api_site", {}).items():
        api = site.get("api", "").strip()
        name = site.get("name", key).strip()
        if not api:
            logger.warning("跳过缺少 api 字段的源: %s", key)
            continue
        sites.append({
            "key": key,
            "name": name,
            "type": 1,
            "api": api,
            "searchable": 1,
            "quickSearch": 1,
            "filterable": 1,
        })
    return {"spider": "", "sites": sites, "parses": [], "lives": []}


def sync_all(config: ServiceConfig, store: ConfigStore) -> int:
    """同步全部三个版本, 单版本失败不影响其他版本。

    Args:
        config: 服务配置。
        store: 配置缓存。

    Returns:
        int: 本轮成功同步的版本数量。
    """
    ok = 0
    for source in SOURCE_FILES:
        try:
            moontv = fetch_upstream(config.candidate_urls(source))
            tvbox = convert_to_tvbox(moontv)
            store.update(source, moontv, tvbox)
            logger.info(
                "版本 %s 同步完成, 共 %d 个采集源",
                source, len(tvbox["sites"]),
            )
            ok += 1
        except (RuntimeError, IOError, json.JSONDecodeError) as exc:
            logger.error(
                "版本 %s 同步失败, 沿用旧缓存: %s", source, exc
            )
    return ok


def refresh_loop(
    config: ServiceConfig,
    store: ConfigStore,
    stop_event: threading.Event,
) -> None:
    """后台定时同步循环(守护线程运行)。

    同步为网络 IO 密集操作, 故使用线程而非进程。

    Args:
        config: 服务配置。
        store: 配置缓存。
        stop_event: 停止信号。
    """
    interval = config.refresh_minutes * 60
    while not stop_event.is_set():
        sync_all(config, store)
        stop_event.wait(interval)


class ConfigHandler(BaseHTTPRequestHandler):
    """HTTP 接口: 输出多版本、双格式订阅及同步状态。"""

    store: ConfigStore  # 由 main() 注入
    default_source: str = "jin18"  # 由 main() 注入

    def _route(self, path: str) -> Optional[Dict[str, Any]]:
        """根据路径解析出应返回的配置内容。

        Args:
            path: 去掉 query 的请求路径。

        Returns:
            Optional[Dict[str, Any]]: 配置内容, 路径非法时为 None。
        """
        if path == "/status":
            return self.store.status()
        if path == "/config.json":
            return self.store.get(self.default_source, "moontv")
        if path == "/tvbox.json":
            return self.store.get(self.default_source, "tvbox")
        match = ROUTE_PATTERN.match(path)
        if match and match.group(2) in SOURCE_FILES:
            fmt = "moontv" if match.group(1) == "config" else "tvbox"
            return self.store.get(match.group(2), fmt)
        return None

    def do_GET(self) -> None:  # noqa: N802 (http.server 固定命名)
        """路由分发。"""
        body = self._route(self.path.split("?")[0])
        if body is None:
            self.send_error(404, "not found")
            return
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header(
            "Content-Type", "application/json; charset=utf-8"
        )
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args: Any) -> None:
        """将访问日志接入 logging。"""
        logger.info("HTTP %s - %s", self.client_address[0], fmt % args)


def main() -> None:
    """服务入口: 启动同步线程与 HTTP 服务。"""
    config = ServiceConfig.from_env()
    store = ConfigStore(config.data_dir)
    logger.info(
        "默认版本: %s, 刷新间隔: %d 分钟, 端口: %d",
        config.default_source, config.refresh_minutes, config.port,
    )
    store.load_from_disk()

    stop_event = threading.Event()
    worker = threading.Thread(
        target=refresh_loop,
        args=(config, store, stop_event),
        daemon=True,
        name="sync-worker",
    )
    worker.start()

    ConfigHandler.store = store
    ConfigHandler.default_source = config.default_source
    server = ThreadingHTTPServer(("0.0.0.0", config.port), ConfigHandler)
    try:
        logger.info("服务已启动: http://0.0.0.0:%d", config.port)
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("收到退出信号, 正在停止...")
    finally:
        stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
