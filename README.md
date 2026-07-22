# LunaTV-Sync 本地采集源同步服务

定时从 GitHub 上游 [hafrey1/LunaTV-config](https://github.com/hafrey1/LunaTV-config)
拉取最新采集源配置，本地缓存并同时输出 **MoonTV/LunaTV** 与 **TVBox**
两种格式的订阅接口。上游每小时自动测活并剔除失效源，本服务定时同步，
即可让 MoonTV、TVBox 等客户端始终使用可用的采集源。
感谢hafrey1大大

## 功能特性

- **三版本同时同步**：jin18（纯净）/ jingjian（含成人）/ full（完整），
  单版本拉取失败不影响其他版本
- **双格式输出**：MoonTV 格式原样转发；TVBox 格式由 `api_site`
  自动无损转换（苹果 CMS `provide/vod` JSON 接口 → TVBox `type: 1` 站点）
- **断网兜底**：每次同步成功即落盘（写入后回读校验），重启或上游
  不可达时沿用旧缓存，服务不中断
- **零依赖部署**：单文件 Python 服务，仅依赖 `requests`，
  支持 Docker 与裸机两种方式

## 目录结构

```
lunatv_local/
├── lunatv_sync.py      # 服务主程序（单文件）
├── requirements.txt    # Python 依赖
├── Dockerfile          # 镜像构建
├── docker-compose.yml  # 一键部署（含 data 卷映射）
├── package.sh          # 离线部署打包脚本（产物在 dist/）
├── deploy/             # 离线部署模板（deploy.sh + 纯镜像 compose）
├── .env.example        # 裸机部署配置模板
└── data/               # 缓存目录（自动生成，Docker 下映射到宿主机）
    ├── moontv_<版本>.json
    └── tvbox_<版本>.json
```

## 快速开始

### 方式一：Docker（推荐）

```bash
cd lunatv_local
docker compose up -d --build
docker compose logs -f    # 应看到三个版本 "同步完成"
```

`data/` 目录通过卷映射持久化在宿主机，容器重建后自动沿用旧缓存。

### 方式二：离线打包部署（无外网/无源码机器）

在能构建镜像的机器上打包：

```bash
cd lunatv_local
bash package.sh    # 无 docker 权限时: sudo bash package.sh
# 产物: dist/lunatv-sync-deploy.tar.gz
```

拷贝到目标机器后一键导入并启动：

```bash
tar -xzf lunatv-sync-deploy.tar.gz
cd lunatv-sync-deploy
bash deploy.sh     # docker load 导入镜像 + 启动服务
```

目标机器只需安装 Docker，无需源码、无需访问 GitHub。

### 方式三：裸机 + systemd

```bash
# Python 3.9+
pip install -r requirements.txt
cp .env.example .env      # 可选，全部有默认值
python3 lunatv_sync.py    # 前台试运行

# 常驻运行
sudo tee /etc/systemd/system/lunatv-sync.service <<'EOF'
[Unit]
Description=LunaTV-config sync service
After=network-online.target

[Service]
WorkingDirectory=/opt/lunatv_local
ExecStart=/usr/bin/python3 /opt/lunatv_local/lunatv_sync.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable --now lunatv-sync
```

## 订阅接口

| 路径 | 说明 |
| --- | --- |
| `/config.json` | MoonTV 格式，默认版本（默认 jin18） |
| `/tvbox.json` | TVBox 格式，默认版本 |
| `/config/jin18.json` | MoonTV 格式，纯净版 |
| `/config/jingjian.json` | MoonTV 格式，含成人版 |
| `/config/full.json` | MoonTV 格式，完整版 |
| `/tvbox/jin18.json` | TVBox 格式，纯净版 |
| `/tvbox/jingjian.json` | TVBox 格式，含成人版 |
| `/tvbox/full.json` | TVBox 格式，完整版 |
| `/status` | 各版本最后同步时间与源数量 |

客户端填写示例（IP 替换为部署机地址）：

- **MoonTV**：管理后台"配置订阅"填
  `http://192.168.x.x:8899/config.json`，
  或定时拉取该地址覆盖 `config.json`
- **TVBox**：配置地址填 `http://192.168.x.x:8899/tvbox.json`

## 配置项

全部通过环境变量配置（裸机可用 `.env` 文件，Docker 在
`docker-compose.yml` 的 `environment` 中修改）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `LUNATV_DEFAULT_SOURCE` | `jin18` | `/config.json`、`/tvbox.json` 返回的默认版本 |
| `LUNATV_PORT` | `8899` | HTTP 服务端口 |
| `LUNATV_REFRESH_MINUTES` | `360` | 自动同步间隔（分钟） |
| `LUNATV_MIRROR_PREFIX` | 内置列表 | GitHub 加速镜像前缀，逗号分隔多个；默认内置 `ghproxy.net`、`ghfast.top`、`gh-proxy.com`，官方地址失败/超时自动按序切换 |
| `LUNATV_DATA_DIR` | `data` | 缓存目录（Docker 内固定为 `/app/data`） |

## 缓存文件格式

- `data/moontv_<版本>.json` —— MoonTV 原始格式：

```json
{
  "cache_time": 7200,
  "api_site": {
    "example.com": {
      "name": "示例源",
      "api": "https://example.com/api.php/provide/vod"
    }
  }
}
```

- `data/tvbox_<版本>.json` —— 转换后的 TVBox 格式：

```json
{
  "spider": "",
  "sites": [
    {
      "key": "example.com",
      "name": "示例源",
      "type": 1,
      "api": "https://example.com/api.php/provide/vod",
      "searchable": 1,
      "quickSearch": 1,
      "filterable": 1
    }
  ],
  "parses": [],
  "lives": []
}
```

## 常见问题

**Q: 同步的源数量比上游 README 宣传的少？**
上游每小时测活会自动剔除失效源，数量浮动属正常现象，
这正是"自更新"机制在起作用。

**Q: 远程机器访问不了 raw.githubusercontent.com？**
无需配置：官方地址失败或超时（15 秒）会自动按序切换内置加速镜像
（`ghproxy.net` → `ghfast.top` → `gh-proxy.com`），日志中会提示
"已切换加速地址"。若内置镜像全部失效，可用 `LUNATV_MIRROR_PREFIX`
自定义镜像列表（逗号分隔）。

**Q: 换默认版本需要改客户端地址吗？**
不需要。改 `LUNATV_DEFAULT_SOURCE` 后重启服务即可，客户端订阅
地址不变；也可以让客户端直接订阅带版本号的路径。

**Q: data 目录下文件属主是 root？**
Docker 容器以 root 运行所致，仅影响宿主机手动编辑需 sudo，
不影响服务运行。

## 注意事项

- `jingjian` 与 `full` 版本包含成人内容源，请注意订阅地址的
  分发范围；服务建议仅在内网开放，勿暴露公网
- 采集源来自第三方公开接口，仅供个人学习研究使用
