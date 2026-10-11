#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""时移: RTSP 客户端(302 链 -> MP2T/TCP SETUP/PLAY -> 裸 TS)。

服务器为 FonsView: DESCRIBE 走 222.68.211.94:554 -> 若干 302 -> SDP(200)
Content-Base = rtsp://<ss>:55554/LIVE/<lid>?AuthInfo=...&TRANSPORT=MP2T/...
55554 端口 SETUP Transport: MP2T/TCP;interleaved=0-1, PLAY 后 $ 帧 interleaved
封装, 载荷即裸 MPEG-TS(0x47@188)。
"""
import calendar
import re
import socket
import time
import urllib.parse


class TimeshiftError(Exception):
    pass


def parse_window(content_base):
    """从 Content-Base 的 DURATION= 参数解析回看缓冲窗口。

    返回 (start, end) epoch 秒; 无该参数返回 (None, None)。
    实测缓冲为滚动窗口, 深度随频道配置不同(1-4 小时), 且会重建。
    """
    m = re.search(r"DURATION=(\d{8}T\d{6}Z)-(\d{8}T\d{6}Z)", content_base)
    if not m:
        return None, None

    def ts(s):
        return calendar.timegm(time.strptime(s, "%Y%m%dT%H%M%SZ"))

    return ts(m.group(1)), ts(m.group(2))


def _rtsp_request(sock, url, method, cseq, extra=""):
    sock.sendall(("%s %s RTSP/1.0\r\nCSeq: %d\r\nUser-Agent: IPTV-Container/1.0\r\n%s\r\n"
                  % (method, url, cseq, extra)).encode())


def _read_response(sock, timeout=10):
    sock.settimeout(timeout)
    data = b""
    try:
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    except socket.timeout:
        pass
    return data


def resolve(base_tsurl, log=None):
    """跟随 302 链, 返回 (sdp_url, content_base_url)。"""
    u = base_tsurl
    for cseq in range(1, 6):
        p = urllib.parse.urlsplit(u)
        try:
            s = socket.create_connection((p.hostname, p.port or 554), timeout=10)
        except OSError as e:
            raise TimeshiftError("连接 %s 失败: %s" % (p.hostname, e))
        try:
            _rtsp_request(s, u, "DESCRIBE", cseq, "Accept: application/sdp\r\n")
            r = _read_response(s)
        finally:
            s.close()
        if r.startswith(b"RTSP/1.0 401") or r.startswith(b"RTSP/1.0 454"):
            raise TimeshiftError("凭据失效(%s)" % r.split(b"\r\n")[0].decode())
        m = re.search(rb"Location:\s*(\S+)", r)
        if m:
            u = m.group(1).decode()
            continue
        if r.startswith(b"RTSP/1.0 200"):
            cb = re.search(rb"Content-Base:\s*(\S+)", r)
            if cb:
                return u, cb.group(1).decode()
        raise TimeshiftError("DESCRIBE 未能解析: %s" % r[:200])
    raise TimeshiftError("302 重定向超过 5 跳")


def open_stream(base_url, back_seconds=0, start_epoch=None, log=None):
    """对 Content-Base 建立 SETUP+PLAY, 返回 (socket, 流迭代器)。

    start_epoch 为绝对回看起点(unix 秒, 按节目点播用); 否则 back_seconds>0
    时从相对 N 秒前回看, 都不给则直播边缘。
    迭代器产出裸 TS 字节; 关闭时断开 RTSP 会话。
    """
    p = urllib.parse.urlsplit(base_url)
    s = socket.create_connection((p.hostname, p.port), timeout=15)
    try:
        _rtsp_request(s, base_url, "SETUP", 10, "Transport: MP2T/TCP;interleaved=0-1\r\n")
        r = _read_response(s, 12)
        if not r.startswith(b"RTSP/1.0 200"):
            raise TimeshiftError("SETUP 失败: %s" % r[:200])
        ms = re.search(rb"Session:\s*([^;\r]+)", r)
        sess = ms.group(1).decode() if ms else ""
        extra = "Session: %s\r\n" % sess
        if start_epoch is not None and start_epoch > 0:
            start = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(start_epoch))
            extra += "Range: clock=%s-\r\n" % start
        elif back_seconds > 0:
            start = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() - back_seconds))
            extra += "Range: clock=%s-\r\n" % start
        _rtsp_request(s, base_url, "PLAY", 11, extra)
        r = _read_response(s, 12)
        if not r.startswith(b"RTSP/1.0 200"):
            raise TimeshiftError("PLAY 失败: %s" % r[:200])
    except Exception:
        s.close()
        raise

    def _iter_ts():
        try:
            buf = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
                # 解析 $ interleaved 帧: 0x24 channel len16 payload
                while True:
                    i = buf.find(b"$")
                    if i < 0 or len(buf) - i < 4:
                        buf = buf[i:] if i >= 0 else b""
                        break
                    if i > 0:
                        buf = buf[i:]
                    ln = int.from_bytes(buf[2:4], "big")
                    if len(buf) < 4 + ln:
                        break
                    if ln:
                        yield buf[4:4 + ln]
                    buf = buf[4 + ln:]
        finally:
            try:
                s.close()
            except OSError:
                pass

    return s, _iter_ts()
