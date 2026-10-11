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


def iter_ts(play_url, log=None):
    """对 playURL 拉播放列表并按序产出 TS 字节。

    按播放列表时长以实时节奏发段(下载快于实时时补齐间隔),
    避免播放器缓冲无限膨胀; 分段失败跳过(不中断整体)。
    """
    segs, durs = _load_playlist(play_url)
    if log:
        log("vod: 播放列表 %d 段, 约 %.0f 分钟" % (len(segs), sum(durs) / 60))
    if not segs:
        raise ValueError("播放列表无分段")

    import time as _time

    def gen():
        t0 = _time.time()
        done = 0.0
        for i, seg in enumerate(segs):
            done += durs[i]
            wait = t0 + done - _time.time()
            if wait > 0.05:
                _time.sleep(min(wait, 12))  # 分段上限, 避免长阻塞丢连接
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

    return gen, len(segs)
