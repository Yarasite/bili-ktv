# B站点歌台

把 Mac 当成 KTV 主机：手机点歌，大屏浏览器自动排队播放 B 站视频。

## 功能

- 粘贴 BV 号 / 分享链接 / `b23.tv` 短链即可点歌
- 队列、正在播放、历史记录实时同步（WebSocket）
- 大屏页自动连播，启动时自动打开 Chrome（允许无手势自动播放）
- 局域网扫码点歌；可选 SSH 反向隧道，公网也能点

## 环境要求

- macOS
- Python 3.10+
- Google Chrome（推荐，用于大屏自动播放）

## 快速开始（局域网）

```bash
./start.sh
```

脚本会自动创建虚拟环境、安装依赖，并用 `caffeinate` 阻止休眠。

| 页面 | 地址 |
|------|------|
| 大屏播放 | http://localhost:8080/tv |
| 手机点歌 | http://\<Mac 局域网 IP\>:8080/ |

默认端口 `8080`，可用环境变量修改：

```bash
PORT=9000 ./start.sh
```

## 公网穿透（可选）

需要一台有公网 IP 的云服务器（Ubuntu，开放 SSH，并放行所选公网端口）：

```bash
./start_remote.sh
# 或
./start_remote.sh --host <公网IP> --user ubuntu --port 8888
```

按提示输入 SSH 密码后会：

1. 配置远端 `GatewayPorts`
2. 启动本地点歌服务
3. 建立 SSH 反向隧道
4. 打印带访问密钥的公网点歌地址（大屏二维码同步切换）

`Ctrl+C` 会同时关闭服务和隧道。SSH 密码也可通过环境变量传入：

```bash
KTV_SSH_PASSWORD='***' ./start_remote.sh --host <公网IP>
```

## 手动运行

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python server.py
```

可选环境变量：

| 变量 | 说明 |
|------|------|
| `PORT` | 局域网服务端口（默认 `8080`） |
| `HOST_IP` | 强制指定二维码里的局域网 IP |
| `BILI_SESSDATA` | B 站 Cookie，提高清晰度/成功率 |
| `TUNNEL_PORT` / `ACCESS_KEY` / `PUBLIC_URL` | 穿透模式由 `remote.py` 自动设置 |

## 项目结构

```
bili.py          # B 站链接解析与播放地址获取
server.py        # HTTP / WebSocket 点歌服务
remote.py        # SSH 反向隧道一键穿透
static/index.html  # 手机点歌页
static/tv.html     # 大屏播放页
start.sh         # 局域网一键启动
start_remote.sh  # 点歌 + 穿透一键启动
```

运行时状态写在本地 `data/`（已 gitignore），不会提交到仓库。
