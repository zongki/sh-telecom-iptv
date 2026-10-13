#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IPTV 容器服务 (FastAPI, 端口 8300)。

/                Web 界面(mpegts.js)
/api/channels    频道列表 JSON
/api/programs/{pid}  频道节目单(过去 7 天 + 未来 1 天)
/api/search?q=       全网节目搜索(所有频道所有日期, 供按节目点播)
/api/ts_window/{pid} 频道回看缓冲窗口(运营商实际只保留滚动 1-4 小时)
/api/status       认证状态与频道数
/api/env          环境自检(配置/认证/udpxy/ffmpeg), 页面打开时调用
/playlist.m3u    直播 M3U(组播经 udpxy)
/playlist_ts.m3u 时移 M3U(容器 RTSP 代理, 带 catchup 标签)
/live/{pid}.ts   直播(302 到 udpxy)
/ts/{pid}.ts?back=N   时移 TS 流(N 秒前)
/ts/{pid}.ts?start=T  按时间点播(T=unix 秒, 亦接受 utc=T, 受时移缓冲限制)
/vod/{pid}.ts?bill=&start=&end=  按节目点播(epg7 getTvodPlayUrl 的 HLS 中继,
                     支持 7 天内任意已播节目, 机顶盒同款通道; sid=会话 ID
                     绑定倍速, /api/vod_speed 可中途变速)
/api/vod_speed    设置点播倍速(sid 会话, 流发段节奏随之缩放)
"""
import json
import os
import shutil
import threading
import time
import urllib.parse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse

from iptv.auth import AuthManager
from iptv.epg import Epg
from iptv import timeshift
from iptv import vod
from iptv import xcode

DATA_DIR = os.environ.get("IPTV_DATA", "/data")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
STATE_DIR = os.path.join(DATA_DIR, "state")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

DEFAULT_CONFIG = {
    # 机顶盒认证参数: 部署后在 /data/config.json 填写, 或用 IPTV_* 环境变量设置
    # (优先级: 环境变量 > config.json > 此处默认值, 见 ENV_MAP)
    "userid": "",       # 例: 12345678@etv1 (机顶盒设置页或抓包获取)
    "sn": "",           # 机顶盒 SN 序列号
    "mac": "",          # 机顶盒 MAC 地址 (AA:BB:CC:DD:EE:FF)
    "stb_ip": "",
    "auth_host": "222.68.208.73:7001",  # 上海电信 EPG 认证服务器
    "udpxy": "http://192.168.1.1:4022", # 路由器上 udpxy 的地址
    "auth_refresh_hours": 3,
    "public_host": "",  # M3U 里对外的主机:端口, 空则用请求 Host
}

app = FastAPI(title="IPTV Container", version="0.1")


# 环境变量名映射: 容器运行参数优先于 config.json(群晖 Container Manager 等
# 可在创建容器时直接填环境变量, 无需进终端改文件)
ENV_MAP = {
    "userid": "IPTV_USERID",
    "sn": "IPTV_SN",
    "mac": "IPTV_MAC",
    "stb_ip": "IPTV_STB_IP",
    "auth_host": "IPTV_AUTH_HOST",
    "udpxy": "IPTV_UDPXY",
    "auth_refresh_hours": "IPTV_AUTH_REFRESH_HOURS",
    "public_host": "IPTV_PUBLIC_HOST",
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    os.makedirs(DATA_DIR, exist_ok=True)  # 挂载目录不存在时自动创建
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fp:
            cfg.update(json.load(fp))
    except FileNotFoundError:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fp:
            json.dump(cfg, fp, ensure_ascii=False, indent=1)
    for key, env in ENV_MAP.items():
        val = os.environ.get(env)
        if not val:
            continue
        if isinstance(cfg.get(key), int):
            try:
                val = int(val)
            except ValueError:
                continue  # 非法数字用回文件里的值
        cfg[key] = val
    return cfg


CFG = load_config()
AUTH = AuthManager(CFG, STATE_DIR, log=lambda m: print(m, flush=True))
EPG = Epg(AUTH, log=lambda m: print(m, flush=True))
REFRESH_LOCK = threading.Lock()


def refresh_auth(force=False):
    """刷新认证并重建频道表。"""
    with REFRESH_LOCK:
        state = AUTH.ensure(force=force)
        EPG.refresh_channels(state)
        return state


def auth_refresh_loop():
    time.sleep(CFG["auth_refresh_hours"] * 3600)  # startup 线程已做首次认证
    while True:
        try:
            refresh_auth(force=True)
        except Exception as e:
            print("auth refresh 失败: %s" % e, flush=True)
        time.sleep(CFG["auth_refresh_hours"] * 3600)


def search_index_loop():
    """后台预热并定期刷新全网节目索引(215 频道 × 8 天, 约 1-2 分钟)。"""
    for _ in range(240):
        if EPG.get_channels():
            break
        time.sleep(5)
    while True:
        try:
            EPG.build_search_index()
        except Exception as e:
            print("节目索引构建失败: %s" % e, flush=True)
        time.sleep(1800)


@app.on_event("startup")
def startup():
    threading.Thread(target=refresh_auth, args=(), daemon=True).start()
    threading.Thread(target=auth_refresh_loop, daemon=True).start()
    threading.Thread(target=search_index_loop, daemon=True).start()


def find_channel(pid):
    for ch in EPG.get_channels():
        if ch["pid"] == pid:
            return ch
    return None


# ---------------- 页面与静态 ----------------
@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"),
                        headers={"Cache-Control": "no-cache"})


@app.get("/favicon.ico")
def favicon():
    return FileResponse(os.path.join(STATIC_DIR, "favicon.ico"),
                        headers={"Cache-Control": "no-cache"})


@app.get("/static/{name}")
def static_files(name: str):
    p = os.path.join(STATIC_DIR, name)
    if not os.path.isfile(p):
        raise HTTPException(404)
    return FileResponse(p, headers={"Cache-Control": "no-cache"})


# ---------------- API ----------------
@app.get("/api/channels")
def api_channels():
    chans = EPG.get_channels()
    if not chans:
        try:
            chans = EPG.get_channels() or EPG.refresh_channels(refresh_auth())
        except Exception as e:
            raise HTTPException(503, "认证未就绪: %s" % e)
    return [{"no": c["no"], "name": c["name"], "pid": c["pid"], "ts": c["ts"],
             "hd": c["hd"], "igmp": c["igmp"], "cats": c.get("cats", [])}
            for c in chans]


@app.get("/api/programs/{pid}")
def api_programs(pid: str):
    ch = find_channel(pid)
    if not ch:
        raise HTTPException(404, "频道不存在")
    return EPG.get_programs(pid, ch["chid"])


@app.get("/api/now")
def api_now(pid: str):
    ch = find_channel(pid)
    if not ch or not ch["chid"]:
        return {}
    return EPG.get_now_next(ch["chid"])


SEARCH_RESULT_LIMIT = 500


@app.get("/api/search")
def api_search(q: str = ""):
    """全网节目搜索: 频道名/节目名子串匹配, 按开始时间倒序。

    索引由后台线程预热(startup 后约 1-2 分钟就绪); 未就绪时返回
    building=true 并触发构建, 前端轮询即可。索引过期时后台重建,
    本次仍返回旧数据。
    """
    st = EPG.search_index_state()
    if not st["done"]:
        threading.Thread(target=EPG.build_search_index, daemon=True).start()
        return {"building": True, "results": []}
    if st["age"] > 1800:
        threading.Thread(target=EPG.build_search_index, daemon=True).start()
    kw = q.strip().lower()
    if not kw:
        return {"building": False, "results": []}
    res = [it for it in EPG.search_items() if kw in it["name"].lower()]
    return {"building": False, "total": len(res),
            "results": res[:SEARCH_RESULT_LIMIT]}


TS_WIN_CACHE = {}  # pid -> {"ts": epoch, "data": {...}}


@app.get("/api/ts_window/{pid}")
def api_ts_window(pid: str):
    """频道当前回看缓冲窗口(epoch 秒), 供前端置灰不可回看的节目。"""
    ch = find_channel(pid)
    if not ch:
        raise HTTPException(404, "频道不存在")
    if not ch["ts"]:
        return {"available": False, "reason": "该频道不支持时移"}
    ent = TS_WIN_CACHE.get(pid)
    if ent and time.time() - ent["ts"] < 300:
        return ent["data"]
    url = ch["timeshift_url"]
    data = {}
    for attempt in (0, 1):
        try:
            _, base = timeshift.resolve(url)
            w_start, w_end = timeshift.parse_window(base)
            if w_start:
                data = {"available": True, "start": w_start, "end": w_end,
                        "depth_hours": round((w_end - w_start) / 3600, 1)}
            break
        except timeshift.TimeshiftError:
            if attempt == 0:
                try:
                    refresh_auth(force=True)
                except Exception as e:
                    data = {"available": False, "reason": "认证刷新失败: %s" % e}
                    break
                ch2 = find_channel(pid)
                if ch2:
                    url = ch2["timeshift_url"]
            else:
                data = {"available": False, "reason": "时移服务不可达"}
    TS_WIN_CACHE[pid] = {"ts": time.time(), "data": data}
    return data


@app.get("/api/status")
def api_status():
    st = AUTH.state
    return {"auth_ok": bool(st), "auth_time": st["auth_time"] if st else 0,
            "channels": len(EPG.get_channels())}


# udpxy 探测结果短缓存, 避免页面每次打开都探测
UDPXY_CHECK = {"ts": 0.0, "ok": False, "detail": ""}
UDPXY_CHECK_TTL = 30


def check_udpxy():
    """探测 udpxy 可达性: /status 页面有 HTTP 响应即视为进程在。"""
    ent = UDPXY_CHECK
    if time.time() - ent["ts"] < UDPXY_CHECK_TTL:
        return {"ok": ent["ok"], "detail": ent["detail"]}
    url = (CFG["udpxy"] or "").rstrip("/")
    import urllib.error
    import urllib.request
    if not url:
        res = {"ok": False, "detail": "config.json 未配置 udpxy 地址"}
    else:
        try:
            req = urllib.request.Request(url + "/status")
            urllib.request.urlopen(req, timeout=3).close()
            res = {"ok": True, "detail": ""}
        except urllib.error.HTTPError:
            res = {"ok": True, "detail": ""}  # 有 HTTP 响应即进程在
        except Exception as e:
            detail = str(e)
            if detail.startswith("<") and detail.endswith(">"):
                detail = detail[1:-1].replace("urlopen error ", "")
            res = {"ok": False, "detail": detail}
    ent.update(ts=time.time(), **res)
    return res


@app.get("/api/env")
def api_env():
    """页面打开时的环境自检: 机顶盒账号配置 / 认证 / udpxy / ffmpeg。

    ffmpeg 仅 4K 频道(AC-3 音轨)需要, 缺失只作警告不作失败。
    """
    missing = [k for k in ("userid", "sn", "mac") if not CFG.get(k)]
    chans = EPG.get_channels()
    st = AUTH.state
    return {
        "config": {"ok": not missing, "missing": missing},
        "auth": {"ok": bool(st and chans), "channels": len(chans),
                 "auth_time": st["auth_time"] if st else 0},
        "udpxy": check_udpxy(),
        "ffmpeg": {"ok": shutil.which("ffmpeg") is not None},
    }


@app.post("/api/auth/refresh")
def api_auth_refresh():
    refresh_auth(force=True)
    return {"ok": True}


# ---------------- 播放 ----------------
@app.get("/live/{pid}.ts")
def live(pid: str):
    """直播: 同源代理 udpxy(302 跳转 udpxy 会被浏览器 CORS 拦截)。"""
    import urllib.request
    ch = find_channel(pid)
    if not ch:
        raise HTTPException(404, "频道不存在")
    hostport = ch["igmp"].split("//")[1]
    upstream = "%s/udp/%s" % (CFG["udpxy"], hostport)
    try:
        r = urllib.request.urlopen(upstream, timeout=15)
    except Exception as e:
        raise HTTPException(502, "udpxy 不可达: %s" % e)

    def gen():
        try:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            r.close()

    stream = gen()
    # AC-3 频道(4K): 容器端音频转码 AAC, 否则浏览器无声
    if ch.get("ac3"):
        stream = xcode.aac_restream(stream)

    return StreamingResponse(stream, media_type="video/mp2t",
                             headers={"Cache-Control": "no-store"})


@app.get("/ts/{pid}.ts")
def ts_stream(pid: str, back: int = 0, start: int = 0, utc: int = 0):
    """时移流: back=N 秒前 或 start/utc=unix 秒(按节目点播)。

    服务器回看缓冲是滚动窗口(DESCRIBE DURATION, 实测 1-4 小时),
    早于缓冲起点的请求服务器会静默钳制到缓冲起点。
    """
    ch = find_channel(pid)
    if not ch:
        raise HTTPException(404, "频道不存在")
    if not ch["ts"]:
        raise HTTPException(400, "该频道不支持时移")

    start_epoch = utc or start
    if start_epoch:
        if start_epoch > 1e12:  # 毫秒时间戳归一为秒
            start_epoch //= 1000
        if start_epoch >= time.time() - 30:  # 未来/刚刚: 直播边缘
            back, start_epoch = 0, None
        else:
            back = 0

    url = ch["timeshift_url"]
    for attempt in (0, 1):
        try:
            _, base = timeshift.resolve(url)
            if start_epoch is not None:
                w_start, _ = timeshift.parse_window(base)
                if w_start and start_epoch < w_start:
                    start_epoch = w_start
            sock, it = timeshift.open_stream(base, back_seconds=back,
                                             start_epoch=start_epoch)
            if ch.get("ac3"):   # AC-3 频道(4K): 音频转码 AAC
                it = xcode.aac_restream(it)
            headers = {"Cache-Control": "no-store",
                       "Content-Type": "video/mp2t"}
            return StreamingResponse(it, media_type="video/mp2t", headers=headers)
        except timeshift.TimeshiftError as e:
            if attempt == 0:
                # 凭据可能过期: 强刷认证后重试一次
                try:
                    refresh_auth(force=True)
                except Exception as ee:
                    raise HTTPException(502, "认证刷新失败: %s" % ee)
                ch2 = find_channel(pid)
                if ch2:
                    url = ch2["timeshift_url"]
            else:
                raise HTTPException(502, "时移失败: %s" % e)


# ---------------- 按节目点播 ----------------
# 点播会话: sid -> {"speed": 倍速, "ts": 最近活动时刻}。
# /vod 流的发送节奏随 speed 缩放(与前端 playbackRate 匹配), 变速不必重连。
VOD_SESSIONS = {}
VOD_SID_MAX = 64


def _vod_sid_ok(sid):
    return bool(sid) and len(sid) <= VOD_SID_MAX and sid.isalnum()


def vod_session(sid):
    now = time.time()
    ent = VOD_SESSIONS.get(sid)
    if not ent:
        ent = {"speed": 1.0, "ts": now}
        VOD_SESSIONS[sid] = ent
        if len(VOD_SESSIONS) > 500:   # 顺带清理陈旧会话
            for k in [k for k, v in VOD_SESSIONS.items()
                      if now - v["ts"] > 6 * 3600]:
                del VOD_SESSIONS[k]
    ent["ts"] = now
    return ent


@app.post("/api/vod_speed")
def api_vod_speed(sid: str = "", speed: float = 1.0):
    """设置点播倍速: /vod 流按此节奏发段, 前端同时调 playbackRate。"""
    if not _vod_sid_ok(sid):
        raise HTTPException(400, "非法会话 ID")
    ent = vod_session(sid)
    ent["speed"] = round(min(max(speed, 0.25), 4.0), 2)
    return {"ok": True, "speed": ent["speed"]}


@app.get("/vod/{pid}.ts")
def vod_stream(pid: str, bill: str = "", start: int = 0, end: int = 0,
               sid: str = ""):
    """按节目点播: epg7 getTvodPlayUrl -> HLS 播放列表 -> 串联 TS 中继。

    bill 为节目单条目 ID(playbillID), start/end 为节目起止 unix 秒,
    sid 为倍速会话(见 /api/vod_speed)。
    支持 7 天内任意已播节目, 不受 RTSP 时移缓冲(1-4 小时)限制。
    """
    ch = find_channel(pid)
    if not ch:
        raise HTTPException(404, "频道不存在")
    if not ch["chid"]:
        raise HTTPException(400, "该频道无 EPG 信息, 不支持节目点播")
    if not bill or not start:
        raise HTTPException(400, "缺少 bill 或 start 参数")
    sess = vod_session(sid) if _vod_sid_ok(sid) else None

    for attempt in (0, 1):
        try:
            play_url = EPG.get_tvod_play_url(ch["chid"], bill, start, end)
            gen, nseg = vod.iter_ts(
                play_url, log=lambda m: print(m, flush=True),
                speed_fn=(lambda: sess["speed"]) if sess else None)
            print("vod: %s bill=%s %d 段" % (pid, bill[-8:], nseg), flush=True)
            return StreamingResponse(gen(), media_type="video/mp2t",
                                     headers={"Cache-Control": "no-store"})
        except Exception as e:
            print("vod: getTvodPlayUrl 失败(attempt %d): %s" % (attempt + 1, e), flush=True)
            if attempt == 0:
                try:
                    refresh_auth(force=True)
                except Exception as ee:
                    raise HTTPException(502, "认证刷新失败: %s" % ee)
                ch2 = find_channel(pid)
                if not (ch2 and ch2["chid"]):
                    raise HTTPException(502, "频道信息失效")
            else:
                raise HTTPException(502, "点播失败: %s" % e)


# ---------------- M3U ----------------
def _base_host(request):
    if CFG["public_host"]:
        return CFG["public_host"]
    return request.headers.get("host", "localhost:8300")


def _m3u(request, mode):
    host = _base_host(request)
    all_ch = request.query_params.get("all") == "1"
    lines = ["#EXTM3U x-tvg-url=\"\""]
    for c in EPG.get_channels():
        cats = c.get("cats", [])
        # 未知频道(无分类)与购物频道默认过滤, ?all=1 输出全部
        if not all_ch and (not cats or "购物" in cats):
            continue
        if mode == "ts" and not c["ts"]:
            continue
        if mode == "live":
            url = "%s/live/%s.ts" % (host, c["pid"])
            group = cats[0] if cats else "其他"
            extra = ""
        else:
            url = "%s/ts/%s.ts?back=0" % (host, c["pid"])
            group = cats[0] if cats else "其他"
            extra = ' catchup="default" catchup-source="http://%s/ts/%s.ts?utc=${start}"' % (
                host, c["pid"])
        lines.append('#EXTINF:-1 tvg-id="%s" tvg-name="%s" group-title="%s"%s,%s'
                     % (c["pid"], c["name"], group, extra, c["name"]))
        lines.append(("http://" + url) if not url.startswith("http") else url)
    return Response("\n".join(lines) + "\n", media_type="audio/x-mpegurl")


@app.get("/playlist.m3u")
def playlist(request: Request):
    return _m3u(request, "live")


@app.get("/playlist_ts.m3u")
def playlist_ts(request: Request):
    return _m3u(request, "ts")
