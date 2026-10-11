#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EPG 数据: 频道列表合并与节目单抓取。"""
import json
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .auth import UA

# ottauth 有但 epg7 频道表没有的频道(4K 卫视等), 按 pid 尾号人工命名
# (2026-10-11 人工读直播截图识别, 截图存 output/probe4k/)
PID_NAMES = {
    "6000130": "北京卫视4K",
    "6000133": "广东卫视4K",
    "6000134": "深圳卫视4K",
    "6000138": "东方卫视4K",
    "6000139": "浙江卫视4K",
    "6000140": "湖南卫视4K",
    "6000141": "江苏卫视4K",
    "6000142": "四川卫视4K",
    "6000143": "山东卫视4K",
}

# AC-3 音频的频道(ffprobe 实测 2026-10-11): 全部 4K 频道(HEVC+AC-3)。
# 浏览器 MSE 普遍不支持 ac-3 解码(mpegts.js 1.7.3 需要浏览器有许可证),
# 播放时音轨进不了 MediaSource → 无声且音量键不可用。/live /ts 对这些
# 频道做容器端音频转码 AC-3->AAC(视频直通)。CCTV-16奥林匹克4K 是 AAC, 不在列。
AC3_PIDS = {
    "6000120",  # CCTV-4K
    "6000130", "6000133", "6000134", "6000138", "6000139",
    "6000140", "6000141", "6000142", "6000143",
}

PROG_CACHE_TTL = 15 * 60      # 整段节目单缓存 15 分钟

# 分类定义: (分类名, epg7 cateID)。顺序即显示优先级(取第一个为主分类)。
# 4K 无独立 cateID, 按频道名识别; epg7 分类表里没有电台类(已实测核实)。
CAT_CATES = [
    ("央视", "000405"), ("卫视", "000409"), ("本地", "000408"), ("4K", None),
    ("高清", "000404"), ("标清", "000403"), ("少儿", "00040B"),
    ("收费", "000401"), ("购物", "000400"),
]

# ottauth/epg7 表里都没有、但实测组播可用的频道(人工验证, 2026-10-11):
# 3840x2160 HEVC ~15Mbps, 抽帧见 CCTV-16 奥林匹克台标。pid 为合成值
# (6000114 已被怀旧剧场HD占用, 不能按组播尾号取), 仅直播无回看。
STATIC_CHANNELS = [
    {"no": 9999, "name": "CCTV-16奥林匹克4K", "pid": "cctv16oly4k",
     "code": "", "chid": "", "igmp": "igmp://233.18.204.114:5140",
     "ts": False, "hd": True, "timeshift_url": ""},
]
PROG_RANGE_BACK = 7           # 天(实测接口最多支持 7 天, 10 天返回异常)
PROG_RANGE_FWD = 1            # 天
SEARCH_TTL = 30 * 60          # 全网节目索引刷新间隔
SEARCH_WORKERS = 8            # 索引构建并发数(对运营商 EPG 保持克制)


class Epg:
    def __init__(self, auth_mgr, log=print):
        self.auth = auth_mgr
        self.log = log
        self.lock = threading.Lock()
        self.channels = []              # 合并后的频道列表
        self.prog_cache = {}            # pid -> {"ts": epoch, "data": [...]}
        self.search_lock = threading.Lock()
        self.search_index = {"ts": 0, "done": False, "items": []}
        self.search_building = False

    # ---------- 频道 ----------
    def refresh_channels(self, state):
        """ottauth channelArray + epg7 getChannelList -> 合并频道表。"""
        from .auth import parse_channels
        raw = parse_channels(state.get("ottauth_text", ""))
        # epg7 的 code 短号(1346060)与 SDP 长号(000...01346060)按零填充匹配。
        # 不能用裸 endswith: 短号 1359 同时是长号 ...00001359 和 ...181359(广东卫视)
        # 的尾缀, 会把 CCTV-14 挂到广东卫视的组播上(61/63/64 三个幽灵频道均因此错位)
        epg7 = self._epg7_channel_list(state)
        merged = []
        seen = set()
        for ch in epg7:
            num = self._pid_from_code(ch.get("code", ""))
            if not num:
                continue
            base = next((b for b in raw if self._pid_match(b["pid"], num)), None)
            if base is None:
                continue
            seen.add(base["pid"])
            merged.append({
                "no": int(ch.get("mixNo") or 0),
                "name": ch.get("name", ""),
                "pid": num,
                "code": ch.get("code", ""),
                "chid": ch.get("ID", ""),
                "igmp": base["igmp"],
                "ts": bool(base["ts"] and ch.get("isTs") == "1"),
                "hd": "HD" in ch.get("name", "") or "4K" in ch.get("name", ""),
                "timeshift_url": base["timeshift_url"],
            })
        # ottauth 有但 epg7 没有(如付费/购物)的频道附加在后
        n = 0
        for base in raw:
            if base["pid"] in seen:
                continue
            n += 1
            pid = base["pid"]
            short = self._short_pid(pid)
            name = PID_NAMES.get(short, "频道" + pid[-4:])
            merged.append({
                "no": 9000 + n, "name": name, "pid": pid,
                "code": "", "chid": "", "igmp": base["igmp"], "ts": base["ts"],
                "hd": "4K" in name, "timeshift_url": base["timeshift_url"],
            })
        # 人工补充的静态频道(ottauth/epg7 均无)
        have = {m["pid"] for m in merged}
        for sc in STATIC_CHANNELS:
            if sc["pid"] not in have:
                merged.append(dict(sc))
        self._apply_categories(state, merged)
        for m in merged:
            m["ac3"] = self._short_pid(m["pid"]) in AC3_PIDS
        merged.sort(key=lambda x: x["no"])
        with self.lock:
            self.channels = merged
        self.log("epg: 频道表合并完成, %d 条" % len(merged))
        return merged

    def _apply_categories(self, state, merged):
        """按 epg7 各分类频道表给合并后的频道打 cats 标签(原地写)。

        cats 为分类名列表, 顺序同 CAT_CATES; 空列表 = 未知(无任何分类归属)。
        某个分类拉取失败只影响该分类, 不阻断频道表刷新。
        """
        cat_nums = {}   # 分类名 -> epg7 短号集合
        for cname, cate_id in CAT_CATES:
            if not cate_id:
                continue
            try:
                cat_nums[cname] = {
                    self._pid_from_code(c.get("code", ""))
                    for c in self._epg7_channel_list(state, cate_id)
                    if self._pid_from_code(c.get("code", ""))
                }
            except Exception as e:
                self.log("epg: 分类 %s 拉取失败: %s" % (cname, e))
        for m in merged:
            cats = []
            for cname, _ in CAT_CATES:
                if cname == "4K":
                    if "4K" in m["name"]:
                        cats.append(cname)
                elif any(self._pid_match(m["pid"], num)
                         for num in cat_nums.get(cname, ())):
                    cats.append(cname)
            m["cats"] = cats
        self.log("epg: 分类完成, " +
                 ", ".join("%s %d" % (cn, sum(1 for m in merged if cn in m["cats"]))
                           for cn, _ in CAT_CATES))

    @staticmethod
    def _pid_from_code(code):
        # Umai:CHAN/1346060@BESTV.SMG.SMG -> 1346060
        try:
            return code.split("CHAN/")[1].split("@")[0]
        except IndexError:
            return ""

    @staticmethod
    def _short_pid(long_pid):
        # SDP 长号最后 8 位(06000130) -> 十进制短号(6000130); 已是短号则原样返回
        try:
            return str(int(long_pid[-8:]))
        except (ValueError, TypeError):
            return long_pid

    @staticmethod
    def _pid_match(long_pid, num):
        # SDP 长号 = 路由前缀 + 十进制零填充的频道号, 匹配 = 尾部是 num 且紧邻前一位是 0
        return long_pid.endswith(num) and \
            (len(long_pid) == len(num) or long_pid[-len(num) - 1] == "0") \
            if num else False

    def _epg7_channel_list(self, state, cate_id="000406"):
        base = state["epg_host_url"]
        cookie = "JSESSIONID=" + state["session_id"]
        if state.get("bims_user_token"):
            cookie += "; bims_user_token=" + state["bims_user_token"]
        url = base + "/function/ajax/epg7getChannelByAjax.jsp"
        req = urllib.request.Request(
            url, data=urllib.parse.urlencode(
                {"action": "getChannelList", "cateID": cate_id}).encode(),
            headers={"User-Agent": UA, "Cookie": cookie,
                     "Content-Type": "application/x-www-form-urlencoded",
                     "Referer": base + "/portal.jsp"}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        return data.get("data") or []

    def get_channels(self):
        with self.lock:
            return list(self.channels)

    # ---------- 节目单 ----------
    def _ajax(self, state, form):
        base = state["epg_host_url"]
        cookie = "JSESSIONID=" + state["session_id"]
        if state.get("bims_user_token"):
            cookie += "; bims_user_token=" + state["bims_user_token"]
        url = base + "/function/ajax/epg7getChannelByAjax.jsp"
        req = urllib.request.Request(
            url, data=urllib.parse.urlencode(form).encode(),
            headers={"User-Agent": UA, "Cookie": cookie,
                     "Content-Type": "application/x-www-form-urlencoded",
                     "Referer": base + "/portal.jsp"}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def get_programs(self, pid, chid, force=False):
        """整段节目单(过去 7 天 + 未来 1 天), 带缓存。"""
        with self.lock:
            ent = self.prog_cache.get(pid)
            if ent and not force and time.time() - ent["ts"] < PROG_CACHE_TTL:
                return ent["data"]
        state = self.auth.ensure()
        now_ms = int(time.time() * 1000)
        try:
            resp = self._ajax(state, {
                "action": "getChannelProg", "code": "Umai:CHAN/%s@BESTV.SMG.SMG" % pid,
                "channelID": chid,
                "startTime": str(now_ms - PROG_RANGE_BACK * 86400 * 1000),
                "endTime": str(now_ms + PROG_RANGE_FWD * 86400 * 1000),
                "offset": "0", "limit": "2000"})
            data = resp.get("data") or []
        except Exception as e:
            self.log("epg: getChannelProg(%s) 失败: %s" % (pid, e))
            data = []
        seen, dedup = set(), []
        for p in data:  # epg7 对部分频道每条节目返回两条(同起止同名, 仅 ID 不同), 去重
            k = (p.get("startTime"), p.get("endTime"), p.get("name"))
            if k not in seen:
                seen.add(k)
                dedup.append(p)
        data = dedup
        with self.lock:
            self.prog_cache[pid] = {"ts": time.time(), "data": data}
        return data

    def get_now_next(self, chid):
        state = self.auth.ensure()
        try:
            resp = self._ajax(state, {"action": "getPreCurNextProg", "channelID": chid,
                                      "time": str(int(time.time() * 1000))})
            return resp.get("data") or {}
        except Exception as e:
            self.log("epg: getPreCurNextProg(%s) 失败: %s" % (chid, e))
            return {}

    def get_tvod_play_url(self, chid, playbill_id, start, end):
        """按节目点播: epg7 getTvodPlayUrl -> playURL (HTTP HLS, 含鉴权)。

        playbillID 为节目单条目的 ID 字段; startTime/endTime 为 unix 秒。
        机顶盒即走此接口实现 7 天回看点播(与 RTSP 时移缓冲无关)。
        """
        state = self.auth.ensure()
        resp = self._ajax(state, {
            "action": "getTvodPlayUrl",
            "channelID": chid,
            "playbillID": playbill_id,
            "startTime": str(int(start)),
            "endTime": str(int(end)),
        })
        data = resp.get("data") or {}
        url = data.get("playURL") or ""
        if not url:
            raise RuntimeError("getTvodPlayUrl 未返回 playURL: %s" % str(resp)[:200])
        return url

    # ---------- 全网节目搜索 ----------
    def search_index_state(self):
        with self.search_lock:
            return {"done": self.search_index["done"],
                    "age": (time.time() - self.search_index["ts"])
                           if self.search_index["done"] else -1,
                    "building": self.search_building}

    def search_items(self):
        with self.search_lock:
            return self.search_index["items"]

    def build_search_index(self):
        """构建全网节目索引(所有频道 × 过去7天+未来1天), 供节目搜索。

        已有未过期索引或正在构建时直接返回; 重复调用并发安全。
        复用 get_programs 的 15 分钟缓存, 与节目单页签共享数据。
        """
        with self.search_lock:
            if self.search_building:
                return
            if self.search_index["done"] and \
                    time.time() - self.search_index["ts"] < SEARCH_TTL:
                return
            self.search_building = True
        try:
            chans = [c for c in self.get_channels() if c["chid"]]

            def fetch(ch):
                try:
                    return ch, self.get_programs(ch["pid"], ch["chid"])
                except Exception:
                    return ch, []

            items = []
            with ThreadPoolExecutor(max_workers=SEARCH_WORKERS) as ex:
                for ch, progs in ex.map(fetch, chans):
                    for p in progs:
                        items.append({
                            "pid": ch["pid"], "no": ch["no"],
                            "chname": ch["name"],
                            "name": p.get("name", ""), "ID": p.get("ID", ""),
                            "startTime": p.get("startTime"),
                            "endTime": p.get("endTime"),
                        })
            items.sort(key=lambda x: int(x["startTime"] or 0), reverse=True)
            idx = {"ts": time.time(), "done": True, "items": items}
            with self.search_lock:
                if not self.search_index["done"] or \
                        self.search_index["ts"] < idx["ts"]:
                    self.search_index = idx
            self.log("epg: 节目索引构建完成: %d 频道 %d 条节目"
                     % (len(chans), len(items)))
        finally:
            with self.search_lock:
                self.search_building = False
