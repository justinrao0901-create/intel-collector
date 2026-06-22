"""util.py — 配置加载 / 路径 / HTTP / RSS·Atom·RDF 解析 / 日期 / URL 工具。纯标准库。

设计要点:
- 路径全部来自 config/settings.json 的 root_dir，零硬编码。
- HTTP 直连(Mac 有真实外网)，带重试 + UA + 宽松 SSL。
- RSS 解析内联(取代旧 rss_proxy 守护进程):同时吃 RSS2.0 / RSS1.0(RDF) / Atom。
"""
from __future__ import annotations
import os, re, ssl, json, time, hashlib
import urllib.request, urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_CFG_DIR = os.path.join(os.path.dirname(_HERE), "config")

_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE

# ─── 配置 ────────────────────────────────────────────────────────────────
def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def settings():
    return load_json(os.path.join(_CFG_DIR, "sources.json")), load_json(os.path.join(_CFG_DIR, "settings.json")), load_json(os.path.join(_CFG_DIR, "keywords.json"))

def root_dir():
    # 从脚本自身位置自动推导项目根(= 数据根 = lib/ 的上一级)。
    # 自动适配两种挂载:本地项目根；Cowork 沙箱 /sessions/.../mnt/Documents/Intel-Reports。
    # settings.json 的 root_dir 仅作记录，不再硬编码使用,避免跨环境路径不一致。
    return os.path.dirname(_HERE)

def path_for(kind, *parts):
    """kind ∈ settings.subdirs。返回绝对路径并确保目录存在。"""
    s = load_json(os.path.join(_CFG_DIR, "settings.json"))
    base = os.path.join(root_dir(), s["subdirs"][kind])
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, *parts) if parts else base

# ─── 日期 ────────────────────────────────────────────────────────────────
CN_TZ = timezone(timedelta(hours=8))   # 北京时区:整套系统按北京调度(日报7:25/周报五18:30/Cowork 8:00·20:00)

def today_str():
    # 必须用北京日期(非UTC):否则 7:25(北京)=UTC前一天,本地产物日期会和包装脚本(Mac本地=北京)、Cowork任务对不上
    return datetime.now(CN_TZ).strftime("%Y-%m-%d")

def parse_date(s):
    """尽力解析多种日期格式 → aware datetime(UTC)。失败返回 None。"""
    if not s:
        return None
    s = s.strip()
    try:
        dt = parsedate_to_datetime(s)  # RFC822 (RSS pubDate)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s[:len(fmt)+8] if "%z" in fmt else s[:19] if "T" in fmt or " " in fmt else s[:10], fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            continue
    return None

def within_window(pub_dt, window_h, now=None):
    """window_h=None → 永远 True(不按日期过滤)。pub 解析失败 → True(保守保留)。"""
    if window_h is None or pub_dt is None:
        return True
    now = now or datetime.now(timezone.utc)
    return (now - pub_dt) <= timedelta(hours=window_h)

# ─── 新近性硬校验(组装端确定性机器闸,四类报告共用)────────────────────────
_RECENCY_DATE_RE = re.compile(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})")
_RECENCY_TOPJ_RE = re.compile(r"Nature|Science|Cell|Lancet|NEJM|JAMA|npj|Immunity|Sci\.?\s*Transl|PNAS|柳叶刀", re.I)

def recency_flags(md, end_date, default_limit=3, topj_limit=7):
    """扫组装后正文,返回超期条目 [{title,pub,age,limit}]。end_date='YYYY-MM-DD'。
    从每条 **Source** 行抽发布日期;顶刊放宽到 topj_limit 天;抽不到日期不判(不误杀)。"""
    try:
        end = datetime.strptime(end_date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return []
    out = []
    for blk in re.split(r"(?m)^### ", md)[1:]:
        head = blk.split("\n", 1)[0].strip()
        m = re.search(r"\*\*Source\*\*[:：][^\n]*", blk)
        src = m.group(0) if m else head
        topj = bool(_RECENCY_TOPJ_RE.search(src))
        d = _RECENCY_DATE_RE.search(src)
        if not d:
            continue
        try:
            pub = datetime(int(d.group(1)), int(d.group(2)), int(d.group(3))).date()
        except ValueError:
            continue
        lim = topj_limit if topj else default_limit
        age = (end - pub).days
        if age > lim:
            out.append({"title": head[:40], "pub": str(pub), "age": age, "limit": lim})
    return out

# ─── HTTP ────────────────────────────────────────────────────────────────
def http_get(url, timeout=15, retries=2, ua="IntelBot/1.0", max_bytes=None):
    """返回 (text, ok)。失败返回 ("", False)。"""
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as resp:
                raw = resp.read(max_bytes) if max_bytes else resp.read()
            return raw.decode("utf-8", errors="replace"), True
        except Exception as e:
            last = e
            time.sleep(1.2 * (attempt + 1))
    return "", False

# ─── RSS / Atom / RDF 解析(内联，取代 rss_proxy) ─────────────────────────
def _txt(el):
    return (el.text or "").strip() if el is not None else ""

def _localname(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag

def parse_feed(xml_text):
    """解析 RSS2.0 / RSS1.0(RDF) / Atom → [{title, link, summary, pub}]。命名空间无关。"""
    items = []
    if not xml_text.strip():
        return items
    try:
        root = ET.fromstring(xml_text.encode("utf-8", errors="ignore"))
    except Exception:
        try:
            root = ET.fromstring(re.sub(r"&(?!amp;|lt;|gt;|quot;|#)", "&amp;", xml_text).encode("utf-8", "ignore"))
        except Exception:
            return items

    entries = [e for e in root.iter() if _localname(e.tag) in ("item", "entry")]
    for e in entries:
        d = {"title": "", "link": "", "summary": "", "pub": ""}
        for c in e:
            ln = _localname(c.tag)
            if ln == "title" and not d["title"]:
                d["title"] = _txt(c)
            elif ln == "link":
                href = c.get("href")
                if href:
                    if c.get("rel") in (None, "alternate"):
                        d["link"] = href
                elif _txt(c):
                    d["link"] = _txt(c)
            elif ln in ("description", "summary", "content", "encoded") and not d["summary"]:
                d["summary"] = strip_html(_txt(c))
            elif ln in ("pubDate", "published", "updated", "date") and not d["pub"]:
                d["pub"] = _txt(c)
        if d["title"] or d["link"]:
            items.append(d)
    return items

def strip_html(text):
    if not text:
        return ""
    text = re.sub(r"<script[^>]*>[\s\S]*?</script>", " ", text, flags=re.I)
    text = re.sub(r"<style[^>]*>[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = (text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " "))
    return re.sub(r"\s+", " ", text).strip()

# ─── URL ────────────────────────────────────────────────────────────────
def extract_url(text):
    m = re.search(r"https?://[^\s<>\[\]]+", text or "")
    if not m:
        return None
    url = m.group(0).rstrip(".,;:!?")
    while url.endswith(")") and url.count("(") < url.count(")"):
        url = url[:-1]
    return url

def normalize_url(url):
    if not url:
        return ""
    url = re.sub(r"^https?://", "", url).split("?")[0].split("#")[0]
    return url.rstrip("/").lower()

def content_hash(title, link):
    return hashlib.md5((normalize_url(link) or (title or "").lower()).encode("utf-8")).hexdigest()[:12]
