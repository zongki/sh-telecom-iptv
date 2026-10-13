#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按节目点播(HLS TVOD): getTvodPlayUrl 下发的 playURL 为 m3u8 播放列表,
每段为 10 秒 MPEG-TS(绝对 URL)。逐段下载串联即连续 TS 流, mpegts.js 可直接播。

实测链路: playURL(:8006, 会 302) -> 播放列表(:8112, application/x-mpegURL)
        -> 分段(:8114, video/MP2T, 支持 Range 断点)。
"""
import urllib.request

from .auth import UA

SEG_UA = "HMTL RTSP 1.0; CTC/2.0"  # 机顶盒所用 UA, 原样带上


def _fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": SEG_UA, "Accept": "*/*"})
    return urllib.request.urlopen(req, timeout=timeout)


def _load_playlist(url, depth=0):
    """GET 播放列表, 返回 (分段 URL 列表, 每段时长秒)。"""
    with _fetch(url, timeout=20) as r:
        body = r.read().decode("utf-8", "replace")
    lines = [l.strip() for l in body.splitlines() if l.strip()]
    if any(l.startswith("#EXT-X-STREAM-INF") for l in lines) and depth < 2:
        for l in lines:
            if not l.startswith("#"):
                return _load_playlist(l, depth + 1)
    segs, durs = [], []
    dur = 0.0
    for l in lines:
        if l.startswith("#EXTINF:"):
            try:
                dur = float(l.split(":")[1].split(",")[0])
            except (IndexError, ValueError):
                dur = 0.0
        elif not l.startswith("#"):
            segs.append(l)
            durs.append(dur or 10.0)
    return segs, durs


def iter_ts(play_url, log=None, speed_fn=None):
    """对 playURL 拉播放列表并按序产出 TS 字节。

    按播放列表时长以实时节奏发段(下载快于实时时补齐间隔),
    避免播放器缓冲无限膨胀; 分段失败跳过(不中断整体)。
    speed_fn() 返回当前倍速(变速播放): 发段节奏按其缩放, 与前端
    playbackRate 匹配; 变速后从当前时刻重新起拍, 立即跟上新节奏。
    """
    segs, durs = _load_playlist(play_url)
    if log:
        log("vod: 播放列表 %d 段, 约 %.0f 分钟" % (len(segs), sum(durs) / 60))
    if not segs:
        raise ValueError("播放列表无分段")

    import time as _time

    def gen():
        def speed():
            try:
                return max(float(speed_fn() or 1.0), 0.1)
            except Exception:
                return 1.0

        next_t = _time.time()
        last_sp = speed()
        for i, seg in enumerate(segs):
            # 分小片睡眠: 变速时最迟 0.25s 醒来重新起拍, 不必等整段睡完
            while True:
                wait = next_t - _time.time()
                if wait <= 0.05:
                    break
                _time.sleep(min(wait, 0.25))
                sp = speed()
                if sp != last_sp:   # 变速: 从当前时刻重排发段节奏
                    last_sp = sp
                    next_t = _time.time()
            try:
                with _fetch(seg) as r:
                    while True:
                        chunk = r.read(65536)
                        if not chunk:
                            break
                        yield chunk
            except Exception as e:
                if log:
                    log("vod: 段 %d/%d 失败跳过: %s" % (i + 1, len(segs), e))
            next_t += durs[i] / last_sp  # 下载超时导致落后时自然追赶(不睡)

    return gen, len(segs)
