#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上海电信 IPTV 认证链: 4kLogAuth -> ottauth -> EPG 会话。纯标准库。

authenticator: JSON{"Randon","EncryToken","UserID","SN","IP","MAC","MagicCode","UpdateTime"}
               AES-128-ECB(密钥=MD5("123456")) PKCS7, hex。
会话 Cookie: EPG 用 jsSetConfig 的 SessionID 作为 JSESSIONID。
EPG 节目单接口必须先访问一次 portal.jsp, 否则查不到数据。
"""
import hashlib
import html
import http.cookiejar
import json
import os
import random
import re
import ssl
import threading
import time
import urllib.parse
import urllib.request

UA = "webkit;Resolution(PAL,720P,1080P,2106P,4K)"
FIXED_UPDATE_TIME = "20230301175307"
AUTH_MAX_AGE = 3 * 3600  # AuthInfo 实测窗口 4h, 提前刷新

# ---------------- AES-128-ECB (纯 Python, 仅加密) ----------------
_SBOX = [
    0x63,0x7c,0x77,0x7b,0xf2,0x6b,0x6f,0xc5,0x30,0x01,0x67,0x2b,0xfe,0xd7,0xab,0x76,
    0xca,0x82,0xc9,0x7d,0xfa,0x59,0x47,0xf0,0xad,0xd4,0xa2,0xaf,0x9c,0xa4,0x72,0xc0,
    0xb7,0xfd,0x93,0x26,0x36,0x3f,0xf7,0xcc,0x34,0xa5,0xe5,0xf1,0x71,0xd8,0x31,0x15,
    0x04,0xc7,0x23,0xc3,0x18,0x96,0x05,0x9a,0x07,0x12,0x80,0xe2,0xeb,0x27,0xb2,0x75,
    0x09,0x83,0x2c,0x1a,0x1b,0x6e,0x5a,0xa0,0x52,0x3b,0xd6,0xb3,0x29,0xe3,0x2f,0x84,
    0x53,0xd1,0x00,0xed,0x20,0xfc,0xb1,0x5b,0x6a,0xcb,0xbe,0x39,0x4a,0x4c,0x58,0xcf,
    0xd0,0xef,0xaa,0xfb,0x43,0x4d,0x33,0x85,0x45,0xf9,0x02,0x7f,0x50,0x3c,0x9f,0xa8,
    0x51,0xa3,0x40,0x8f,0x92,0x9d,0x38,0xf5,0xbc,0xb6,0xda,0x21,0x10,0xff,0xf3,0xd2,
    0xcd,0x0c,0x13,0xec,0x5f,0x97,0x44,0x17,0xc4,0xa7,0x7e,0x3d,0x64,0x5d,0x19,0x73,
    0x60,0x81,0x4f,0xdc,0x22,0x2a,0x90,0x88,0x46,0xee,0xb8,0x14,0xde,0x5e,0x0b,0xdb,
    0xe0,0x32,0x3a,0x0a,0x49,0x06,0x24,0x5c,0xc2,0xd3,0xac,0x62,0x91,0x95,0xe4,0x79,
    0xe7,0xc8,0x37,0x6d,0x8d,0xd5,0x4e,0xa9,0x6c,0x56,0xf4,0xea,0x65,0x7a,0xae,0x08,
    0xba,0x78,0x25,0x2e,0x1c,0xa6,0xb4,0xc6,0xe8,0xdd,0x74,0x1f,0x4b,0xbd,0x8b,0x8a,
    0x70,0x3e,0xb5,0x66,0x48,0x03,0xf6,0x0e,0x61,0x35,0x57,0xb9,0x86,0xc1,0x1d,0x9e,
    0xe1,0xf8,0x98,0x11,0x69,0xd9,0x8e,0x94,0x9b,0x1e,0x87,0xe9,0xce,0x55,0x28,0xdf,
    0x8c,0xa1,0x89,0x0d,0xbf,0xe6,0x42,0x68,0x41,0x99,0x2d,0x0f,0xb0,0x54,0xbb,0x16,
]
_RCON = [0x01,0x02,0x04,0x08,0x10,0x20,0x40,0x80,0x1b,0x36]


def _xtime(a):
    a <<= 1
    if a & 0x100:
        a ^= 0x11b
    return a & 0xFF


def _mul(a, b):
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        b >>= 1
        a = _xtime(a)
    return p


def aes128_ecb_encrypt(key16, data):
    pad = 16 - (len(data) % 16)
    data = data + bytes([pad]) * pad
    w = [list(key16[i * 4:i * 4 + 4]) for i in range(4)]
    for i in range(4, 44):
        t = list(w[i - 1])
        if i % 4 == 0:
            t = t[1:] + t[:1]
            t = [_SBOX[b] for b in t]
            t[0] ^= _RCON[i // 4 - 1]
        w.append([w[i - 4][j] ^ t[j] for j in range(4)])

    def encrypt_block(blk):
        s = [[blk[r + 4 * c] for c in range(4)] for r in range(4)]
        for c in range(4):
            for r in range(4):
                s[r][c] ^= w[c][r]
        for rnd in range(1, 10):
            s = [[_SBOX[b] for b in row] for row in s]
            for r in range(1, 4):
                s[r] = s[r][r:] + s[r][:r]
            for c in range(4):
                a = [s[r][c] for r in range(4)]
                s[0][c] = _mul(a[0], 2) ^ _mul(a[1], 3) ^ a[2] ^ a[3]
                s[1][c] = a[0] ^ _mul(a[1], 2) ^ _mul(a[2], 3) ^ a[3]
                s[2][c] = a[0] ^ a[1] ^ _mul(a[2], 2) ^ _mul(a[3], 3)
                s[3][c] = _mul(a[0], 3) ^ a[1] ^ a[2] ^ _mul(a[3], 2)
            for c in range(4):
                for r in range(4):
                    s[r][c] ^= w[rnd * 4 + c][r]
        s = [[_SBOX[b] for b in row] for row in s]
        for r in range(1, 4):
            s[r] = s[r][r:] + s[r][:r]
        for c in range(4):
            for r in range(4):
                s[r][c] ^= w[40 + c][r]
        return bytes(s[r][c] for c in range(4) for r in range(4))

    return b"".join(encrypt_block(data[i:i + 16]) for i in range(0, len(data), 16))


def build_authenticator(cfg, encrytoken, update_time=FIXED_UPDATE_TIME):
    ip_padded = ",".join(p.zfill(3) for p in cfg["stb_ip"].split("."))
    obj = {
        "Randon": "".join(random.choice("0123456789") for _ in range(8)),
        "EncryToken": encrytoken,
        "UserID": cfg["userid"],
        "SN": cfg["sn"],
        "IP": ip_padded,
        "MAC": cfg["mac"],
        "MagicCode": "CTC",
        "UpdateTime": update_time,
    }
    plain = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    key = hashlib.md5(b"123456").digest()
    return aes128_ecb_encrypt(key, plain).hex()


# ---------------- HTTP ----------------
class HttpClient:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=ctx))

    def request(self, url, method="GET", form=None, referer=None, origin=None, cookie=None,
                timeout=15):
        data = None
        headers = {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-cn",
            "Accept-Charset": "utf-8, iso-8859-1, utf-16, *;q=0.7",
        }
        if form is not None:
            data = urllib.parse.urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if referer:
            headers["Referer"] = referer
        if origin:
            headers["Origin"] = origin
        if cookie:
            headers["Cookie"] = cookie
        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        with self.opener.open(req, timeout=timeout) as resp:
            return resp.geturl(), resp.read()


# ---------------- HTML 小工具 ----------------
def _attr(tag, name):
    m = re.search(r'%s\s*=\s*["\']([^"\']*)["\']' % name, tag, re.I)
    return html.unescape(m.group(1)) if m else None


def parse_forms(body, base_url):
    text = body.decode("utf-8", "replace")
    forms = []
    for m in re.finditer(r"<form\b[^>]*>", text, re.I):
        tag = m.group(0)
        action = _attr(tag, "action") or ""
        method = (_attr(tag, "method") or "get").lower()
        end = text.find("</form", m.end())
        seg = text[m.end():end if end > 0 else len(text)]
        fields = {}
        for im in re.finditer(r"<input\b[^>]*>", seg, re.I):
            it = im.group(0)
            name = _attr(it, "name")
            if name:
                fields[name] = _attr(it, "value") or ""
        forms.append({"action": urllib.parse.urljoin(base_url, action), "method": method,
                      "fields": fields})
    return forms


def get_var(text, name):
    m = re.search(r'var\s+%s\s*=\s*["\']([^"\']*)["\']' % re.escape(name), text)
    return m.group(1) if m else None


def get_top_location(text):
    m = re.search(r"top\.document\.location\s*=\s*['\"]([^'\"]+)['\"]", text)
    return m.group(1) if m else None


# ---------------- 频道解析 ----------------
_CH_RE = re.compile(
    r'ChannelID="(?P<channel_id>[^"]*)"[^,]*,UserChannelID="(?P<user_channel_id>[^"]*)",'
    r'ChannelURL="(?P<igmp>[^"]*)"[^,]*,TimeShift="(?P<ts>[^"]*)",'
    r'ChannelSDP="rtsp://[^"]*/SHDXFH_live/(?P<pid>\d+)"[^,]*,'
    r'TimeShiftURL="(?P<timeshift_url>rtsp://[^"]*)"')


def parse_channels(ottauth_text):
    """ottauth 响应的 channelArray -> [{pid, igmp, timeshift_url, ts}]。"""
    out = []
    for m in _CH_RE.finditer(ottauth_text):
        out.append({
            "pid": m.group("pid"),
            "igmp": m.group("igmp"),
            "ts": m.group("ts") == "1",
            "timeshift_url": m.group("timeshift_url").replace("&amp;", "&"),
        })
    return out


# ---------------- 认证主流程 ----------------
def run_full_auth(cfg, datadir, log=print):
    """完整认证链。成功返回 state dict, 并落盘 datadir/auth_state.json + ottauth_response.html。"""
    c = HttpClient()
    state = {"auth_time": int(time.time())}

    # 1. pre4kLogAuth
    auth_host = cfg["auth_host"]
    u1 = "http://%s/iptv3a/4kLogAuth.do" % auth_host
    q1 = {"Action": "Login", "UserID": cfg["userid"], "SN": cfg["sn"], "Type": "iptv4k",
          "Mode": "MENU.SMG-4K", "FCCSupport": "1"}
    url, body = c.request(u1 + "?" + urllib.parse.urlencode(q1))
    log("auth: pre4kLogAuth ok (%d bytes)" % len(body))

    # 2. r4kLogAuth
    f = parse_forms(body, url)[0]
    url, body = c.request(f["action"], f["method"].upper(), f["fields"],
                          referer=u1, origin="http://" + auth_host)
    text = body.decode("utf-8", "replace")
    encrytoken = get_var(text, "encrytoken")
    if not encrytoken:
        raise RuntimeError("未取到 encrytoken: %s" % text[:300])

    # 3. ottauth
    f = parse_forms(body, url)[0]
    f["fields"]["authenticator"] = build_authenticator(cfg, encrytoken)
    ottauth_url = f["action"]
    url, body = c.request(ottauth_url, f["method"].upper(), f["fields"],
                          referer="http://%s/iptv3a/4kLogAuth.do" % urllib.parse.urlparse(url).netloc,
                          origin="http://" + urllib.parse.urlparse(url).netloc)
    text = body.decode("utf-8", "replace")
    usertoken = get_var(text, "usertoken")
    if not usertoken:
        raise RuntimeError("ottauth 失败: %s" % text[:500])
    state["usertoken"] = usertoken
    state["epggroup"] = get_var(text, "epggroup") or ""
    state["ottauth_text"] = text
    log("auth: ottauth OK usertoken=%s" % usertoken)

    # 4. EPG index -> loadbalance
    forms = parse_forms(body, url)
    epgform = next((x for x in forms if "index.jsp" in x["action"]), None)
    if not epgform:
        raise RuntimeError("未找到 epgform")
    url, body = c.request(epgform["action"], "POST", epgform["fields"], referer=ottauth_url)
    lb = get_top_location(body.decode("utf-8", "replace"))
    if lb:
        url, body = c.request(urllib.parse.urljoin(url, lb), referer=url)

    # 5. funcportalauth
    forms = parse_forms(body, url)
    pa = next((x for x in forms if "portalauth" in x["action"]), None)
    if pa is None:
        raise RuntimeError("未找到 portalauth 表单")
    url, body = c.request(pa["action"], pa["method"].upper(), pa["fields"], referer=url)

    # 6. jsSetConfig 会话参数
    text = body.decode("utf-8", "replace")
    cfgmap = dict(re.findall(r"jsSetConfig\(\s*['\"](\w+)['\"]\s*,\s*['\"]([^'\"]*)['\"]\s*\)", text))
    session_id = cfgmap.get("SessionID", "")
    framecode = cfgmap.get("framecode", "")
    ip_port = cfgmap.get("IpPort", "")
    if not (framecode and ip_port):
        raise RuntimeError("未取到 SessionID/framecode/IpPort: %s" % cfgmap)
    epg_host_url = "http://%s/iptvepg/%s" % (ip_port, framecode)
    state["session_id"] = session_id
    state["epg_host_url"] = epg_host_url
    log("auth: EPG 会话 %s @ %s" % (framecode, ip_port))

    # portal.jsp: 初始化服务端节目单会话状态(必须)
    _, _ = c.request(epg_host_url + "/portal.jsp", cookie="JSESSIONID=" + session_id)

    # 7. AuthByAjax -> bims_user_token
    auth_url = epg_host_url + "/service/auth/AuthByAjax.jsp?action=auth"
    url, body = c.request(auth_url, cookie="JSESSIONID=" + session_id)
    try:
        authinfo = json.loads(body.decode("utf-8", "replace"))
        state["bims_user_token"] = authinfo.get("bimsUserToken", "")
    except Exception:
        state["bims_user_token"] = ""
    log("auth: AuthByAjax OK")

    # 落盘
    os.makedirs(datadir, exist_ok=True)
    with open(os.path.join(datadir, "auth_state.json"), "w", encoding="utf-8") as fp:
        json.dump({k: v for k, v in state.items() if k != "ottauth_text"},
                  fp, ensure_ascii=False, indent=1)
    with open(os.path.join(datadir, "ottauth_response.html"), "w", encoding="utf-8") as fp:
        fp.write(text)
    return state


class AuthManager:
    """线程安全: 会话持有与过期刷新。"""

    def __init__(self, cfg, datadir, log=print):
        self.cfg = cfg
        self.datadir = datadir
        self.log = log
        self.lock = threading.Lock()
        self.state = None

    def ensure(self, force=False):
        with self.lock:
            if self.state and not force and \
                    time.time() - self.state["auth_time"] < AUTH_MAX_AGE:
                return self.state
            self.state = run_full_auth(self.cfg, self.datadir, self.log)
            return self.state

    def invalidate(self):
        with self.lock:
            self.state = None
