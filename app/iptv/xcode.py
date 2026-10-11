#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""流转换: AC-3 音频转 AAC(视频直通), 供浏览器 MSE 播放。

4K 频道为 HEVC+AC-3, 浏览器 MSE 普遍无 ac-3 解码许可, 音轨进不了
MediaSource(无声且音量键不可用)。NAS 容器内 ffmpeg 对裸 TS 只重编码
音频, 视频流原样拷贝(CPU 开销极小)。输入为裸 TS 字节迭代器。
"""
import subprocess
import threading


def aac_restream(ts_iter, log=None):
    """把裸 TS 迭代器经 ffmpeg 转码音频, 返回新的字节迭代器。"""
    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-i", "pipe:0",
         "-c:v", "copy",                # 4K HEVC 转码不现实, 直通
         "-c:a", "aac", "-b:a", "256k",
         # 5.1 源必须降混为立体声: mpegts.js 在 Chrome 上给 AAC 声明 HE-AAC
         # (mp4a.40.5), Chrome 的 HE-AAC 解码不支持 5.1, 6 声道会导致整个
         # MediaSource 会话失败(视频也播不出)。立体声实测正常(四川路径)。
         "-ac", "2",
         "-f", "mpegts", "pipe:1"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL)

    def pump():
        try:
            for chunk in ts_iter:
                proc.stdin.write(chunk)
        except (OSError, ValueError):
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    threading.Thread(target=pump, daemon=True).start()

    def out():
        try:
            while True:
                b = proc.stdout.read(65536)
                if not b:
                    break
                yield b
        finally:
            proc.kill()

    return out()
