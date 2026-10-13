# sh-telecom-iptv — 上海电信 IPTV 软终端

一个跑在 NAS / 任意 Docker 容器里的上海电信 IPTV 客户端：**213 个频道网页直播、时移回看、7 天内按节目点播、全网节目搜索**，同时输出标准 M3U 供 TiviMate / DIYP 等播放器使用。协议栈（认证、EPG、RTSP 时移、HLS 点播）全部为 Python 纯标准库逆向实现，无重依赖。

> 仅用于个人学习网络协议，请在自己家庭宽带授权范围内使用。
>
> 本项目的所有代码均由 AI 生成（模型：GLM，智谱 Z.ai）。

## 功能

- **网页直播**：全部频道经容器同源代理播放（mpegts.js），含 10 个 4K 频道（HEVC 3840x2160）
- **时移回看**：RTSP 时移代理，`?back=N` 回退 N 秒 / `?start=T` 按时间点播（受运营商滚动回看缓冲限制，实测 1–4 小时）
- **按节目点播（7 天）**：走机顶盒同款 `getTvodPlayUrl` → HLS 通道，把分段 TS 串联为连续流，7 天内任意已播节目可点播，网页进度条可拖动定位，支持 0.5–2 倍速变速播放（中途切换不重连）
- **节目单与搜索**：过去 7 天 + 未来 1 天节目单；全网 50000+ 条节目模糊搜索，点结果即点播
- **4K 频道可用**：4K 频道音轨为 AC-3，浏览器普遍不解码，容器端 ffmpeg 转码为 AAC（视频直通、CPU 占用极低）；5.1 源自动降混立体声
- **频道分类**：央视 / 卫视 / 本地 / 4K / 高清 / 标清 / 少儿 / 收费 / 购物，默认过滤未知与购物频道
- **M3U 输出**：`playlist.m3u`（直播）、`playlist_ts.m3u`（时移，带 `catchup-source` 标签，TiviMate / DIYP 可按节目回看）
- **移动端适配**：纯 CSS 媒体查询，手机竖屏 / 横屏自适应；iPhone 需 iOS 17.1+（Safari 仅支持 ManagedMediaSource）

## 工作原理

```
浏览器 ──HTTP──▶ 容器(FastAPI :8300) ──┬── 认证链 4kLogAuth → ottauth → EPG 会话(EPG 服务器)
                 │                     ├── 直播  udpxy(路由器) → IGMP 组播
                 │                     ├── 时移  RTSP 302 链 → MP2T/TCP interleaved
                 │                     ├── 点播  getTvodPlayUrl → HLS 分段串联
                 │                     └── 4K   ffmpeg AC-3→AAC 音频转码
                 └── mpegts.js 播放器 / 节目单 / 搜索
```

关键协议细节（AES-128-ECB authenticator、EPG 会话状态机、RTSP interleaved 帧、HLS 分段）见 `app/iptv/` 各模块源码注释，均为实机抓包逆向并实测验证。

## 前置条件

1. **上海电信 IPTV**：光猫 IPTV 口接出的局域网内可访问运营商 EPG 与组播（机顶盒能看即可）
2. **udpxy**：路由器（如 OpenWRT）上安装 udpxy，把 UDP 组播转 HTTP；建议 `max_clients` 调到 20 以上（默认 3，快速换台会 502），配置见下文[路由器配置](#路由器配置openwrt-示例)
3. **机顶盒账号信息**：`userid`（形如 `12345678@etv1`）、`sn`、`mac`。获取方式：
   - 机顶盒设置页"关于/系统信息"里直接看；
   - 或在光猫/路由器 WAN 口抓机顶盒开机报文，`4kLogAuth.do?UserID=...&SN=...` 请求里有全部三个参数。
4. **同一局域网**的 NAS / 服务器 / 软路由，可运行 Docker（也可直接用 Python 3.9+ 运行）

## 路由器配置（OpenWRT 示例）

容器所在机器通常不在 IPTV 机顶盒网段内，路由器需要把 IPTV VLAN 桥接进来并提供两条通路：**组播转 HTTP**（udpxy，直播用）和**通往运营商 EPG / RTSP / HLS 服务器的路由**（认证、时移、点播用）。以下为上海电信实测值，其他地区请按抓包结果调整 VLAN 与网段。

### 1. IPTV VLAN 接口（`/etc/config/network`）

```
config device 'IPTV'
    option type '8021q'
    option ifname 'eth5'        # WAN 口物理网卡
    option vid '85'             # 上海电信 IPTV VLAN

config interface 'iptv'
    option proto 'dhcp'
    option ifname 'eth5.85'
    option defaultroute '0'     # 不要抢默认路由
```

### 2. udpxy（`/etc/config/udpxy`）

```
config udpxy
    option disabled '0'
    option respawn '1'
    option bind 'br-lan'
    option port '4022'
    option source 'eth5.85'
    option max_clients '20'
```

`max_clients` 不低于 20：容器代理侧的断开检测有约 11 秒延迟，换台后旧连接仍占槽，默认值 3 时快速换台就会 502。

### 3. 静态路由（通往运营商 EPG / 时移 / 点播网段）

EPG 认证（222.68.208.73 等）、RTSP 时移、HLS 点播服务器都在运营商内网，需经 IPTV 网关（实测 30.170.0.1）可达。写成 UCI 静态路由（netifd 管理、开机自动恢复，**不要**用 `rc.local` 里的 `ip route add`，换行丢失或 DHCP 换地址都会失效）：

```
config route
    option interface 'iptv'
    option target '30.170.0.0/16'          # IPTV 内网, DHCP 自带直连, 兜底
config route
    option interface 'iptv'
    option target '222.68.0.0/16'
    option gateway '30.170.0.1'
config route
    option interface 'iptv'
    option target '218.83.0.0/16'
    option gateway '30.170.0.1'
config route
    option interface 'iptv'
    option target '124.75.0.0/16'
    option gateway '30.170.0.1'
config route
    option interface 'iptv'
    option target '222.73.0.0/16'
    option gateway '30.170.0.1'
config route
    option interface 'iptv'
    option target '180.169.0.0/16'
    option gateway '30.170.0.1'
```

### 4. 防火墙（`/etc/config/firewall`）

```
config zone
    option name 'iptv'
    list network 'iptv'
    option input 'ACCEPT'
    option output 'ACCEPT'
    option forward 'ACCEPT'
    option masq '1'             # 把 NAS/容器流量 SNAT 成路由器的 IPTV 地址

config forwarding
    option src 'lan'
    option dest 'iptv'
```

注意同名 zone 只定义一次；若运营商侧不限制源地址，`masq` 也可改由 `/etc/firewall.user` 的 `iptables -t nat -A POSTROUTING -o eth5.85 -j MASQUERADE` 承担，二选一即可。

### 5. 应用并验证

```
ifup iptv
/etc/init.d/firewall restart
/etc/init.d/udpxy restart

ip route show | grep eth5.85   # 应看到上述网段
ping -c1 30.170.0.1            # IPTV 网关可达
curl -s -o /dev/null -w '%{http_code}\n' http://<路由器IP>:4022/status   # 200
```

## 快速开始（Docker）

```bash
# 构建镜像（基于 homeassistant 镜像, 自带 python3.9 + ffmpeg）
cd app
docker build -t sh-telecom-iptv:latest .

# 运行(配置既可以用 -e 环境变量, 也可以稍后编辑 data/config.json, 见下文)
mkdir -p /volume1/docker/iptv/data
docker run -d --name iptv --restart unless-stopped \
  -p 8300:8300 \
  -e IPTV_USERID=12345678@etv1 \
  -e IPTV_SN=000200202016081810102473 \
  -e IPTV_MAC=F0:40:7B:35:06:24 \
  -e IPTV_UDPXY=http://192.168.1.1:4022 \
  -e IPTV_PORT=8300 \
  -v /volume1/docker/iptv/data:/data \
  sh-telecom-iptv:latest
```

打开 `http://<NAS地址>:8300/` 即可。首次启动认证 + 建立搜索索引约需 1–2 分钟。

不用 Docker 也可以直接跑（机器上需自行装 ffmpeg 供 4K 转码）：

```bash
cd app && pip install "fastapi<0.100" "pydantic<2" uvicorn
IPTV_DATA=./data python3 -m uvicorn main:app --host 0.0.0.0 --port 8300
```

### 配置

所有参数都支持两种设置方式，**优先级：环境变量 > data/config.json > 默认值**。改完任一配置重启容器生效。

| config.json 字段 | 环境变量 | 说明 |
|---|---|---|
| `userid` | `IPTV_USERID` | 机顶盒账号，形如 `12345678@etv1` |
| `sn` | `IPTV_SN` | 机顶盒序列号 |
| `mac` | `IPTV_MAC` | 机顶盒 MAC 地址 |
| `stb_ip` | `IPTV_STB_IP` | 机顶盒 IP（部分链路需要，可留空） |
| `auth_host` | `IPTV_AUTH_HOST` | EPG 认证服务器（默认即上海电信现网地址，一般不用改） |
| `udpxy` | `IPTV_UDPXY` | udpxy 地址，如 `http://192.168.1.1:4022` |
| `auth_refresh_hours` | `IPTV_AUTH_REFRESH_HOURS` | 认证刷新周期（小时），AuthInfo 实测 4 小时过期，默认 3 |
| `public_host` | `IPTV_PUBLIC_HOST` | M3U 里对外的主机:端口；外网访问时填 NAS 的公网地址，留空用请求 Host |
| （无 config.json 对应项） | `IPTV_PORT` | 容器监听端口，默认 8300；改动后 `-p`/端口映射的容器侧要同步改 |

**群晖 Container Manager 设置示例**：容器 → 创建时向导第 3 步"环境变量"中逐条添加上表的环境变量（如 `IPTV_USERID` = `12345678@etv1`）；已有容器则在 Container Manager 里停止容器 → 详情 → 环境 → 添加变量 → 启动。存储空间挂载 `/docker/iptv/data` 到容器 `/data`，端口映射本地 8300 → 容器 8300。

### 用外部播放器收看

容器自带两个 M3U 播放列表，任何支持"网络播放列表"的播放器都能用（`NAS_IP` 为 NAS 地址）：

- 直播：`http://NAS_IP:8300/playlist.m3u`
- 时移（带 7 天回看标签）：`http://NAS_IP:8300/playlist_ts.m3u`
- 加 `?all=1` 可包含默认隐藏的未知/购物频道

| 平台 | 播放器 | 操作 |
|---|---|---|
| Windows / macOS | VLC | 媒体 → 打开网络串流，粘贴 M3U 地址 |
| Windows | PotPlayer | 打开 URL，粘贴 M3U 地址 |
| Android TV / 盒子 | TiviMate、Kodi（IPTV Simple 插件） | 添加播放列表 → 填 M3U 地址 |
| Android 手机/盒子 | DIYP、百川等 | 设置里填直播源地址 |
| iOS / Apple TV | iPlayTV、ntPlayer、GSE 等 | 添加 M3U 播放列表 |

**回看**：`playlist_ts.m3u` 每个频道带 `catchup="default"` 标签，TiviMate 等支持 catchup 的播放器可在频道内调出节目单回看，自动请求 `/ts/{pid}.ts?utc=时间戳`。注意 RTSP 时移只有滚动 1–4 小时；7 天点播走的是网页的 `/vod` 接口，需要节目单条目 ID，外部播放器用不了。

**注意事项**：

- **须在同一局域网**：直播走 udpxy 组播，跨网段/外网不可达。外网观看需在路由器把 8300 端口映射到 NAS，并把 `public_host` 设为公网地址让 M3U 生成正确 URL——但 8300 端口没有鉴权，不建议暴露公网。
- **4K 频道可直接播**：VLC 等自带 AC-3 解码器，不需要容器的 AAC 转码（那是为浏览器准备的）。
- **无节目单 EPG**：M3U 的 `x-tvg-url` 目前为空（XMLTV 输出在待办清单），外部播放器看不到节目预告。
- 全网节目搜索 + 按节目点播只有网页界面有。

## 接口

| 端点 | 说明 |
|---|---|
| `/` | Web 界面（直播 / 节目单 / 搜索三个页签） |
| `/api/channels` | 频道列表 JSON（含分类、是否 4K/AC-3、组播地址） |
| `/api/programs/{pid}` | 频道节目单（过去 7 天 + 未来 1 天） |
| `/api/search?q=` | 全网节目搜索 |
| `/api/ts_window/{pid}` | 频道当前 RTSP 回看缓冲窗口 |
| `/api/status` · `/api/auth/refresh` | 认证状态 / 手动刷新认证 |
| `/api/env` | 环境自检（配置 / 认证 / udpxy / ffmpeg），页面打开时自动调用 |
| `/live/{pid}.ts` | 直播流（同源代理 udpxy） |
| `/ts/{pid}.ts?back=N` · `?utc=T` | 时移流（N 秒前 / 绝对时间点） |
| `/vod/{pid}.ts?bill=&start=&end=` | 按节目点播（7 天内，HLS 中继；`sid` 绑定倍速会话） |
| `POST /api/vod_speed?sid=&speed=` | 设置点播倍速（0.25–4，流的发段节奏随之缩放，变速不重连） |
| `/playlist.m3u` · `/playlist_ts.m3u` | M3U 播放列表（`?all=1` 输出全部频道） |

## 数据更新频率

各数据的刷新时机由独立定时器决定，均可按需调节：

| 数据 | 更新时机 | 说明 |
|---|---|---|
| 频道表 | 默认 **3 小时**（`auth_refresh_hours`），随认证刷新整体重建 | 从 ottauth 组播表与 epg7 频道列表实时抓取合并，启动时立即建；时移/点播遇到 AuthInfo 过期（约 4 小时）也会强制重刷，实际间隔不会比 3 小时更久 |
| 节目单 | 按需拉取，单频道缓存 **15 分钟** | 数据源 `epg7 getChannelProg` 仅提供过去 7 天 + 未来 1 天的滚动窗口；运营商发布的预告最多 15 分钟后可见 |
| 全网搜索索引 | 后台每 **30 分钟**重建 | 启动后预热约 1–2 分钟（全部频道 × 8 天并行抓取）；索引未就绪时 `/api/search` 返回 `building=true`，前端自动轮询 |
| "正在播"显示 | 前端每 **30 秒**轮询 `/api/now` | — |
| 回看缓冲窗口 | 按频道缓存 **5 分钟** | 运营商时移缓冲是滚动窗口（1–4 小时）且会重建，此值只作展示参考 |

M3U 播放列表不做缓存，每次请求都从当前频道表实时生成。

## 已知限制（实测结论）

- **RTSP 时移缓冲只有滚动 1–4 小时**，且会随服务器重建而变化；请求早于缓冲起点会被服务器静默钳制到起点（播错节目）。7 天内按节目点播请走 `/vod`，它不受此限制。
- 4K 频道（除 CCTV-16 奥林匹克外）音轨为 AC-3，容器会自动转码 AAC；无转码能力的裸 M3U 播放器播 4K 直播可能无声。
- 部分频道（如个别 4K 频道）运营商侧无 EPG 数据，仅直播、无节目单与点播。
- iOS Safari 需 iOS 17.1+（mpegts.js 1.8.0 的 ManagedMediaSource 路径）。

## 目录结构

```
app/
├── main.py            # FastAPI 入口：API / 直播 / 时移 / 点播 / M3U
├── iptv/
│   ├── auth.py        # 认证链：authenticator AES-128-ECB → ottauth → EPG 会话
│   ├── epg.py         # 频道表合并、节目单、全网搜索索引、分类
│   ├── timeshift.py   # RTSP 时移客户端（302 链、MP2T/TCP、回看窗口解析）
│   ├── vod.py         # getTvodPlayUrl HLS 分段串联
│   └── xcode.py       # ffmpeg AC-3→AAC 音频转码
├── static/            # Web 界面（mpegts.js 1.8.0，无前端框架）
├── Dockerfile
└── requirements.txt
```

## 致谢

- [denymz/sh-tel-iptv-spider](https://github.com/denymz/sh-tel-iptv-spider) — 上海电信 IPTV 抓取程序（Go）。本项目的机顶盒认证算法（authenticator 的 JSON 结构与 AES-128-ECB 加密，密钥为 MD5 摘要）参考自该项目，在其基础上用 Python 重新实现，并独立打通了 EPG 会话、RTSP 时移、HLS 点播等其余链路。没有它，认证这一步无从下手。
- [xqq/mpegts.js](https://github.com/xqq/mpegts.js) — Web 端 MSE/ManagedMediaSource 的 MPEG-TS 播放器（Apache-2.0），随本项目捆绑分发于 `app/static/`。

## 免责声明

本项目仅供学习研究网络协议与流媒体技术之用，不提供任何内容，不关联任何运营商或版权方。所有频道内容的版权归原权利方所有，请勿用于商业用途或在授权范围外分发。使用本项目产生的一切后果由使用者自行承担。

## License

[MIT](LICENSE)
