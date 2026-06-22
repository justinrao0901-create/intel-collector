"""clean.py — 正文清洗。合并旧 cleaners.py + prepare_daily._clean_boilerplate。

注意:Cowork 侧的 web_fetch 自带可读性提取，会接掉大部分清洗。本模块用于本地预抓时
先做一遍清洗，避免把 CSS/JS/导航垃圾喂给模型。site-specific 规则保留几个老大难站点。
"""
from __future__ import annotations
import re

_BOILER = re.compile(
    r"var\(--[^)]+\)|:root\s*\{[^}]*--wp--|--wp--preset--[a-z0-9-]+\s*:|@font-face\s*\{|"
    r"@keyframes\s+\w+\s*\{|linear-gradient\(|window\.[A-Z_]\w*|__assign\s*=|"
    r"Object\.prototype\.hasOwnProperty\.call|__NEXT_DATA__|webpack|NREUM|gtag\(|googletag|"
    r"dataLayer|@media\s*\(|font-family\s*:|font-size\s*:|xmlns(?::\w+)?=|data-[a-z0-9_-]+=",
    re.I,
)
_NAV = ["contact us", "sign in", "login", "sign up", "privacy policy", "terms of service",
        "cookie policy", "about us", "careers", "newsletter", "subscribe", "all rights reserved",
        "learn more", "read more", "back to top", "会员登录", "会员注册", "投稿"]

SPA_HEAVY = ("a16zcrypto.com", "nium.com", "chainalysis.com", "stripe.com",
             "circle.com", "thepaypers.com", "ripple.com", "checkout.com")


def clean_text(text):
    """通用清洗:去 CSS/JS 块、导航/页脚、句子感极弱页面判空。"""
    if not text:
        return ""
    text = text.replace("\x00", " ")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r":root\s*\{[^}]{0,8000}--wp--[^}]{0,8000}\}", " ", text)
    text = re.sub(r"\{[^{}]{0,4000}--[a-z0-9-]+:[^{}]{0,4000}\}", " ", text)
    text = re.sub(r"linear-gradient\([^)]{0,1000}\)", " ", text)
    text = re.sub(r"@(?:font-face|keyframes\s+\w+|media[^{]*)\s*\{[^}]{0,3000}\}", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    if _BOILER.search(text[:1500]):
        return ""
    low = text.lower()
    if sum(1 for k in _NAV if k in low) >= 5:
        return ""
    for ph in ("all rights reserved", "privacy policy", "terms of service", "cookie policy",
               "manage cookies", "follow us on", "subscribe to our"):
        text = re.sub(re.escape(ph), " ", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()

    sentences = len(re.findall(r"[\.!?。；:]", text[:1200]))
    if len(text) < 200 or sentences < 3:
        return ""
    return text


def site_specific(text, url=""):
    """几个老大难站点的定向清洗。返回 '' 仅当确为非正文(JS bundle 等)。"""
    if not text:
        return ""
    u = (url or "").lower()
    if "mpaypass.com.cn" in u:
        text = re.sub(r"^[\s\S]*?(?=移动支付网\s*\d+月|来源：移动支付网|据外媒报道|据中国人民银行)", "", text)
        for c in ("资讯 焦点 业界 视角 创新 评测 企业 投融资 政策 国际 调研 专栏 活动 数据库",
                  "牌照查询 资料查询 财报查询 支付数据查询 交通支付查询", "会员登录", "会员注册", "投稿"):
            text = text.replace(c, " ")
    if "a16zcrypto.com" in u:
        text = re.sub(r"\{ Alpine\.store[\s\S]*?\}", " ", text)
        text = re.sub(r"class=[\"'][^\"']{0,300}[\"']", " ", text)
        text = re.sub(r"<[^>]{0,200}>", " ", text)
        m = re.search(r"(?:Stablecoins|This piece|This article|We argue|In this|Today,|For decades|The state of)", text)
        if m and m.start() > 50:
            text = text[m.start():]
    if "chainalysis.com" in u:
        paras = re.findall(r"(?:[A-Z][^\.\n]{40,400}\.)", text)
        if paras and len(" ".join(paras)) > 600:
            text = " ".join(paras)
        if sum(1 for s in ("=>", "Object.values", "use strict") if s in text[:1500]) >= 2:
            return ""
    return re.sub(r"\s+", " ", text).strip()


def from_html(html, url=""):
    """HTML → 干净正文。本地预抓用。"""
    if not html:
        return ""
    t = re.sub(r"<(script|style|nav|header|footer|aside)[^>]*>[\s\S]*?</\1>", " ", html, flags=re.I)
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = re.sub(r"</p\s*>", "\n\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = clean_text(t)
    t = site_specific(t, url)
    return t
