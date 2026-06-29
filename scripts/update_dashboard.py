#!/usr/bin/env python3
"""
VPN 信息源观察日报自动更新脚本 v3

本版规则按用户要求重做：
1) 只输出与当前时间窗口一致的信息，超过窗口或无法确认发布时间的条目不会进入日报重点。
2) 每条统一要点后都附原始来源链接；多个来源反映同一件事时合并为同一个要点。
3) 面板仅保留 5 类：竞品动态、社交媒体、reddit讨论、政策风险、第三方网站。
4) 移除增长/运营行动建议，只做信息观察、来源追踪和时效说明。
"""
from __future__ import annotations

import csv
import datetime as dt
import email.utils
import html
import json
import os
import re
import sys
import time
import traceback
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, quote_plus, urlencode, urljoin, urlparse, urlunparse

try:
    import requests
except Exception:  # pragma: no cover
    print("requests is required. Install with: pip install -r requirements.txt", file=sys.stderr)
    raise

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover
    print("beautifulsoup4 is required. Install with: pip install -r requirements.txt", file=sys.stderr)
    raise

try:
    from dateutil import parser as date_parser  # type: ignore
except Exception:  # pragma: no cover
    date_parser = None  # type: ignore

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
DOCS_DIR = ROOT / "docs"
DATA_DIR = DOCS_DIR / "data"
ARCHIVE_DIR = DOCS_DIR / "archive"
REPORT_DIR = DOCS_DIR / "reports"
STATUS_DIR = DOCS_DIR / "status"

SOURCES_CSV = CONFIG_DIR / "sources.csv"
MANUAL_CSV = CONFIG_DIR / "manual_inputs.csv"

def env_int(name: str, default: int) -> int:
    value = os.getenv(name, "")
    if value is None or str(value).strip() == "":
        return default
    try:
        return int(float(str(value).strip()))
    except Exception:
        return default


MAX_SOURCES_PER_RUN = env_int("MAX_SOURCES_PER_RUN", 140)
MAX_ITEMS_PER_SOURCE = env_int("MAX_ITEMS_PER_SOURCE", 8)
HTTP_TIMEOUT = env_int("HTTP_TIMEOUT_SECONDS", 12)
MAX_WORKERS = env_int("MAX_WORKERS", 8)
SKIP_NETWORK = os.getenv("SKIP_NETWORK", "").lower() in {"1", "true", "yes"}

# Strict freshness window. Default is 36 hours for every category.
# User requirement: no old baseline items in the panel; undated items are hidden by default.
FRESHNESS_HOURS = env_int("DASHBOARD_LOOKBACK_HOURS", env_int("FRESHNESS_HOURS", env_int("FRESHNESS_WINDOW_HOURS", 36)))
FRESHNESS_DAYS = {cat: FRESHNESS_HOURS / 24.0 for cat in ["竞品动态", "社交媒体", "reddit讨论", "政策风险", "第三方网站"]}
UNKNOWN_DATE_POLICY = os.getenv("UNKNOWN_DATE_POLICY", "exclude").lower()  # exclude | include

USER_AGENT = os.getenv(
    "DASHBOARD_USER_AGENT",
    "web:com.example.vpn-dashboard:v3.0 (contact: dashboard-owner) Python/requests",
)
REDDIT_USER_AGENT = os.getenv(
    "REDDIT_USER_AGENT",
    "web:com.example.vpn-dashboard:v3.0 (by /u/dashboard-owner)",
)
X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN", "").strip()

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
        "Accept-Language": "en-GB,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
    }
)

TAXONOMY: List[Dict[str, str]] = [
    {
        "name": "竞品动态",
        "definition": "只放竞品官方网站、官方博客、新闻室、更新日志、状态页等竞品一手内容；用户论坛不放这里。",
        "examples": "NordVPN/ExpressVPN/Proton VPN/Surfshark/Windscribe/Mullvad/PIA/CyberGhost 官网内容。",
    },
    {
        "name": "社交媒体",
        "definition": "只放竞品官方社交账号发布的内容。非官方讨论、泛搜索和 KOC 内容不放在这里。",
        "examples": "竞品官方 X/Twitter、YouTube、TikTok、LinkedIn、Instagram。",
    },
    {
        "name": "reddit讨论",
        "definition": "只放 Reddit 用户发布或讨论的帖子。",
        "examples": "r/VPN、r/nordvpn、r/ProtonVPN、r/Express_VPN 等。",
    },
    {
        "name": "政策风险",
        "definition": "只放政府、监管机构、官方政策公告与合规页面。",
        "examples": "GOV.UK、Ofcom、ICO、欧盟/美国/英国监管机构。",
    },
    {
        "name": "第三方网站",
        "definition": "除以上四类外的媒体、评测站、搜索结果、商店评论、论坛、Trustpilot、YouTube 泛搜索等。",
        "examples": "TechRadar、Tom’s Guide、Comparitech、Top10VPN、Google/应用商店搜索、独立论坛。",
    },
]
TAXONOMY_ORDER = [x["name"] for x in TAXONOMY]
TAXONOMY_META = {x["name"]: x for x in TAXONOMY}


def normalize_category(value: Any) -> str:
    text = clean_text(value)
    mapping = {
        "Reddit讨论": "reddit讨论",
        "Reddit 討論": "reddit讨论",
        "reddit": "reddit讨论",
        "Reddit": "reddit讨论",
        "政策监管": "政策风险",
        "政策/监管": "政策风险",
        "竞品官网": "竞品动态",
        "竞品官方网站": "竞品动态",
        "第三方": "第三方网站",
    }
    text = mapping.get(text, text)
    return text if text in TAXONOMY_ORDER else ""

COMPETITOR_DOMAINS = {
    "nordvpn.com", "nordsecurity.com", "expressvpn.com", "protonvpn.com", "proton.me",
    "surfshark.com", "windscribe.com", "blog.windscribe.com", "mullvad.net",
    "privateinternetaccess.com", "cyberghostvpn.com", "airvpn.org", "tunnelbear.com",
    "hide.me", "vyprvpn.com", "ivpn.net", "purevpn.com",
}
GOVERNMENT_DOMAINS = {
    "gov.uk", "ofcom.org.uk", "ico.org.uk", "europa.eu", "ec.europa.eu", "ftc.gov",
    "fcc.gov", "legislation.gov.uk", "parliament.uk", "congress.gov",
}
SOCIAL_DOMAINS = {
    "x.com", "twitter.com", "youtube.com", "youtu.be", "tiktok.com", "instagram.com",
    "facebook.com", "linkedin.com", "threads.net", "mastodon.social",
}
COMPETITOR_BRANDS = [
    "nordvpn", "nord vpn", "expressvpn", "express vpn", "protonvpn", "proton vpn", "surfshark",
    "windscribe", "mullvad", "private internet access", "pia", "cyberghost", "airvpn", "tunnelbear",
    "hide.me", "vyprvpn", "ivpn", "purevpn",
]

ISSUE_RULES: List[Tuple[str, List[str]]] = [
    ("年龄验证/未成年人政策", ["age assurance", "age verification", "age check", "under-16", "under 16", "children", "child online", "online safety", "ofcom", "ico", "年龄", "未成年人", "儿童", "青少年"]),
    ("流媒体/平台识别", ["netflix", "iplayer", "bbc", "prime video", "disney", "streaming", "geo", "geoblock", "region", "detected", "unblock", "流媒体", "解锁", "地区", "被识别"]),
    ("价格/续费/退款/取消", ["price", "pricing", "renewal", "refund", "cancel", "subscription", "deal", "discount", "coupon", "trial", "价格", "续费", "退款", "取消", "折扣", "优惠"]),
    ("节点/速度/连接稳定", ["slow", "speed", "latency", "disconnect", "connection", "server", "node", "captcha", "ip reputation", "blocked", "速度", "延迟", "掉线", "节点", "连接", "验证码"]),
    ("隐私/审计/无日志", ["privacy", "no-log", "no logs", "audit", "jurisdiction", "transparency", "webrtc", "dns leak", "leak", "wireguard", "隐私", "无日志", "审计", "透明", "泄漏", "协议"]),
    ("产品/功能更新", ["app", "feature", "release", "update", "protocol", "android", "ios", "windows", "macos", "linux", "extension", "功能", "发布", "更新", "客户端", "协议"]),
    ("榜单/评测/SEO", ["best vpn", "review", "comparison", "vs", "ranking", "top vpn", "guide", "榜单", "评测", "对比", "排名"]),
    ("公共 Wi-Fi/旅行/学生", ["wifi", "wi-fi", "airport", "hotel", "travel", "student", "university", "uni", "eduroam", "5g", "公共", "机场", "酒店", "旅行", "学生", "校园"]),
    ("免费版/入门需求", ["free vpn", "free", "freemium", "免费", "试用", "入门"]),
]

SUBCATEGORY_RULES: Dict[str, List[Tuple[str, List[str]]]] = {
    "竞品动态": [
        ("产品/功能更新", ["release", "update", "feature", "app", "protocol", "server", "extension", "download", "功能", "版本", "发布", "协议"]),
        ("安全/隐私/审计", ["privacy", "audit", "security", "no-log", "transparency", "漏洞", "隐私", "审计", "安全"]),
        ("研究/报告/新闻室", ["research", "report", "survey", "press", "newsroom", "study", "研究", "报告", "新闻"]),
        ("营销/合作/奖项", ["award", "partner", "sponsor", "campaign", "discount", "合作", "奖项", "营销"]),
    ],
    "社交媒体": [
        ("官方公告", ["announce", "launch", "release", "update", "发布", "公告", "上线"]),
        ("官方活动/互动", ["giveaway", "campaign", "event", "webinar", "活动", "互动", "直播"]),
        ("官方客服回应", ["support", "issue", "fix", "outage", "客服", "修复", "故障"]),
    ],
    "reddit讨论": [
        ("竞品口碑", ["nord", "express", "proton", "surfshark", "windscribe", "mullvad", "pia", "cyberghost"]),
        ("购买/退款/续费", ["price", "refund", "cancel", "renewal", "subscription", "coupon", "deal", "价格", "退款", "续费", "取消"]),
        ("连接/节点/速度", ["slow", "speed", "disconnect", "server", "captcha", "latency", "blocked", "速度", "节点", "掉线", "验证码"]),
        ("使用场景", ["netflix", "streaming", "wifi", "school", "uni", "travel", "流媒体", "校园", "旅行", "公共"]),
        ("隐私/安全", ["privacy", "logs", "audit", "leak", "wireguard", "隐私", "泄漏", "协议"]),
    ],
    "政策风险": [
        ("年龄验证/未成年人", ["age", "children", "child", "under-16", "online safety", "ofcom", "ico", "年龄", "儿童", "未成年人"]),
        ("网络安全/隐私监管", ["privacy", "data protection", "security", "enforcement", "gdpr", "隐私", "数据保护", "执法"]),
        ("立法/监管时间表", ["act", "bill", "roadmap", "guidance", "consultation", "deadline", "法案", "路线图", "指南", "咨询"]),
    ],
    "第三方网站": [
        ("媒体评测/榜单", ["best vpn", "review", "comparison", "ranking", "tom", "techradar", "comparitech", "榜单", "评测", "对比"]),
        ("X-VPN 外部评测", ["x-vpn", "xvpn", "top10vpn"]),
        ("商店/口碑", ["app store", "google play", "trustpilot", "rating", "review", "应用商店", "评分", "评论"]),
        ("论坛/问答", ["forum", "discussion", "student room", "privacyguides", "论坛", "问答"]),
        ("开放社媒搜索/非官方", ["x 搜索", "youtube 搜索", "tiktok 搜索", "search", "搜索"]),
    ],
}

PRIORITY_LABELS = [(86, "P0"), (74, "P1"), (60, "P2"), (0, "P3")]
STOP_WORDS = {
    "the", "and", "for", "with", "from", "that", "this", "are", "you", "your", "vpn", "vpns",
    "https", "http", "www", "com", "reddit", "best", "can", "how", "what", "why", "does", "have",
    "official", "blog", "news", "privacy",
}

_REDDIT_TOKEN: Optional[str] = None
_REDDIT_TOKEN_EXPIRES = 0.0


def now_sg() -> dt.datetime:
    if ZoneInfo:
        return dt.datetime.now(tz=ZoneInfo("Asia/Singapore"))
    return dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc) + dt.timedelta(hours=8)


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def read_csv_dicts(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return [{k: clean_text(v) for k, v in row.items()} for row in reader]


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(clean_text(value)))
    except Exception:
        return default


def source_priority(row: Dict[str, str]) -> int:
    for key in ["监控优先级分", "priority", "优先级"]:
        if clean_text(row.get(key)):
            return safe_int(row.get(key), 0)
    return 0


def include_source(row: Dict[str, str]) -> bool:
    layer = row.get("日报层级", "")
    freq = row.get("追踪频率", "")
    candidate = row.get("是否候选", "")
    sustainable = row.get("是否可持续追踪", "")
    pri = source_priority(row)
    return (
        "核心" in layer
        or "周更" in layer
        or "每日" in freq
        or "每周" in freq
        or sustainable == "是"
        or candidate == "是"
        or pri >= 5
    )


def domain_of(url: str) -> str:
    parsed = urlparse(clean_text(url if url.startswith("http") else "https://" + url))
    host = (parsed.netloc or "").lower().split("@")[(-1)].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host


def domain_matches(domain: str, candidates: Iterable[str]) -> bool:
    return any(domain == c or domain.endswith("." + c) for c in candidates)


def is_competitor_official_social(row: Dict[str, str], domain: str, text: str) -> bool:
    if not domain_matches(domain, SOCIAL_DOMAINS):
        return False
    if "搜索" in row.get("来源名称", "") or "search" in row.get("URL/入口", "").lower():
        return False
    return any(b in text for b in COMPETITOR_BRANDS) or "官方" in text or "official" in text


def source_taxonomy(row: Dict[str, str]) -> str:
    # Source-type rules first. New v3 labels are preferred over legacy columns.
    explicit = normalize_category(row.get("新版分类") or row.get("面板类别") or row.get("面板分类") or row.get("情报大类"))
    url = clean_text(row.get("URL/入口"))
    domain = domain_of(url) if url.startswith("http") else ""
    text = " ".join([
        row.get("来源类别", ""), row.get("平台", ""), row.get("来源名称", ""),
        row.get("备注", ""), row.get("URL/入口", ""), row.get("采集策略", ""), row.get("子分类", ""), explicit,
    ]).lower()
    if "reddit" in domain or "reddit" in text:
        return "reddit讨论"
    if domain and domain_matches(domain, GOVERNMENT_DOMAINS):
        return "政策风险"
    if any(k in text for k in ["government", "regulator", "gov.uk", "ofcom", "ico", "监管机构", "政府"]):
        return "政策风险"
    if is_competitor_official_social(row, domain, text):
        return "社交媒体"
    # Official-domain forums, community boards and user reviews are not official competitor announcements.
    if any(k in text for k in ["forum", "forums", "community", "社区", "论坛", "review", "reviews", "trustpilot"]):
        return "第三方网站"
    official_page_hint = any(k in text for k in [
        "官方博客", "官方新闻", "官方公告", "official blog", "official news", "official announcements",
        "press room", "press area", "newsroom", "status", "changelog", "release notes", "privacy hub",
    ])
    if explicit == "竞品动态" and domain and domain_matches(domain, COMPETITOR_DOMAINS):
        return "竞品动态"
    if domain and domain_matches(domain, COMPETITOR_DOMAINS) and official_page_hint:
        return "竞品动态"
    if official_page_hint:
        return "竞品动态"
    # Explicit labels are honored after hard source-type checks; stale 政策风险 labels are not honored for non-government pages.
    if explicit and explicit != "政策风险":
        return explicit
    return "第三方网站"


def guess_category_from_item(item: Dict[str, Any], source_row: Dict[str, str]) -> str:
    cat = source_taxonomy(source_row)
    if cat in TAXONOMY_ORDER:
        return cat
    return "第三方网站"


def match_rule(text: str, rules: List[Tuple[str, List[str]]], default: str) -> Tuple[str, List[str]]:
    lowered = text.lower()
    best_name = default
    best_hits: List[str] = []
    for name, keywords in rules:
        hits = [kw for kw in keywords if kw.lower() in lowered]
        if len(hits) > len(best_hits):
            best_name = name
            best_hits = hits[:8]
    return best_name, best_hits


def classify_issue(text: str) -> Tuple[str, List[str]]:
    return match_rule(text, ISSUE_RULES, "综合观察")


def infer_subcategory(category: str, text: str) -> Tuple[str, List[str]]:
    defaults = {
        "竞品动态": "官网综合动态",
        "社交媒体": "官方社媒综合",
        "reddit讨论": "用户综合讨论",
        "政策风险": "政策综合风险",
        "第三方网站": "第三方综合观察",
    }
    return match_rule(text, SUBCATEGORY_RULES.get(category, []), defaults.get(category, "综合观察"))


def priority_label(score: float) -> str:
    for threshold, label in PRIORITY_LABELS:
        if score >= threshold:
            return label
    return "P3"


def with_limit(url: str, limit: int = MAX_ITEMS_PER_SOURCE) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault("limit", str(limit))
    return urlunparse(parsed._replace(query=urlencode(query)))


def fetch_url(url: str, *, accept_json: bool = False, reddit: bool = False, bearer: Optional[str] = None) -> requests.Response:
    headers: Dict[str, str] = {}
    if accept_json:
        headers["Accept"] = "application/json,text/plain,*/*"
    if reddit:
        headers["User-Agent"] = REDDIT_USER_AGENT
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
        headers["User-Agent"] = REDDIT_USER_AGENT
    resp = SESSION.get(url, timeout=HTTP_TIMEOUT, headers=headers, allow_redirects=True)
    resp.raise_for_status()
    return resp


def parse_datetime(value: Any, now: Optional[dt.datetime] = None) -> Optional[dt.datetime]:
    text = clean_text(value)
    if not text:
        return None
    now = now or now_sg()
    lowered = text.lower()
    rel = re.search(r"(\d+)\s*(minute|minutes|min|hour|hours|day|days|week|weeks|month|months|year|years)\s+ago", lowered)
    if rel:
        n = int(rel.group(1))
        unit = rel.group(2)
        if unit.startswith("min"):
            return now - dt.timedelta(minutes=n)
        if unit.startswith("hour"):
            return now - dt.timedelta(hours=n)
        if unit.startswith("day"):
            return now - dt.timedelta(days=n)
        if unit.startswith("week"):
            return now - dt.timedelta(days=7 * n)
        if unit.startswith("month"):
            return now - dt.timedelta(days=30 * n)
        if unit.startswith("year"):
            return now - dt.timedelta(days=365 * n)
    if lowered in {"today", "今天"}:
        return now
    if lowered in {"yesterday", "昨天"}:
        return now - dt.timedelta(days=1)
    if re.fullmatch(r"\d{9,11}(\.\d+)?", text):
        try:
            return dt.datetime.fromtimestamp(float(text), tz=dt.timezone.utc)
        except Exception:
            pass
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    except Exception:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(text)
        if parsed:
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    except Exception:
        pass
    if date_parser is not None:
        try:
            parsed = date_parser.parse(text, fuzzy=True)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
        except Exception:
            return None
    return None


MONTHS = "Jan|January|Feb|February|Mar|March|Apr|April|May|Jun|June|Jul|July|Aug|August|Sep|Sept|September|Oct|October|Nov|November|Dec|December"
DATE_PATTERNS = [
    rf"\b(?:{MONTHS})\s+\d{{1,2}},\s*20\d{{2}}\b",
    rf"\b\d{{1,2}}\s+(?:{MONTHS})\s+20\d{{2}}\b",
    r"\b20\d{2}[-/]\d{1,2}[-/]\d{1,2}\b",
    r"\b\d{1,2}[-/]\d{1,2}[-/]20\d{2}\b",
]


def extract_date_from_text(text: str, now: Optional[dt.datetime] = None) -> Optional[dt.datetime]:
    text = clean_text(text)
    if not text:
        return None
    for pat in DATE_PATTERNS:
        m = re.search(pat, text, flags=re.I)
        if m:
            parsed = parse_datetime(m.group(0), now)
            if parsed:
                return parsed
    rel = re.search(r"\b\d+\s*(?:minute|minutes|min|hour|hours|day|days|week|weeks|month|months|year|years)\s+ago\b", text, flags=re.I)
    if rel:
        return parse_datetime(rel.group(0), now)
    return None


def to_sg(dt_value: dt.datetime) -> dt.datetime:
    tz = ZoneInfo("Asia/Singapore") if ZoneInfo else dt.timezone(dt.timedelta(hours=8))
    if dt_value.tzinfo is None:
        dt_value = dt_value.replace(tzinfo=dt.timezone.utc)
    return dt_value.astimezone(tz)


def to_iso(dt_value: Optional[dt.datetime]) -> str:
    if not dt_value:
        return ""
    return to_sg(dt_value).isoformat()


def extract_page_date(soup: BeautifulSoup, now: Optional[dt.datetime] = None) -> Optional[dt.datetime]:
    selectors = [
        ("meta", {"property": "article:published_time"}),
        ("meta", {"property": "article:modified_time"}),
        ("meta", {"property": "og:updated_time"}),
        ("meta", {"name": "date"}),
        ("meta", {"name": "pubdate"}),
        ("meta", {"name": "publishdate"}),
        ("meta", {"name": "timestamp"}),
        ("meta", {"name": "DC.date"}),
        ("meta", {"itemprop": "datePublished"}),
        ("meta", {"itemprop": "dateModified"}),
    ]
    for name, attrs in selectors:
        tag = soup.find(name, attrs=attrs)
        if tag:
            val = clean_text(tag.get("content") or tag.get("datetime") or tag.get_text(" "))
            parsed = parse_datetime(val, now)
            if parsed:
                return parsed
    for script in soup.find_all("script", attrs={"type": "application/ld+json"})[:10]:
        try:
            payload = json.loads(script.string or script.get_text(" ") or "")
        except Exception:
            continue
        stack = payload if isinstance(payload, list) else [payload]
        while stack:
            obj = stack.pop(0)
            if isinstance(obj, list):
                stack.extend(obj)
            elif isinstance(obj, dict):
                for key in ["datePublished", "dateModified", "uploadDate", "publishedAt"]:
                    parsed = parse_datetime(obj.get(key), now)
                    if parsed:
                        return parsed
                for val in obj.values():
                    if isinstance(val, (dict, list)):
                        stack.append(val)
    time_tag = soup.find("time")
    if time_tag:
        parsed = parse_datetime(time_tag.get("datetime") or time_tag.get("title") or time_tag.get_text(" "), now)
        if parsed:
            return parsed
    body_text = clean_text(soup.get_text(" "))[:5000]
    return extract_date_from_text(body_text, now)


def extract_item_date(container: Any, page_date: Optional[dt.datetime], now: Optional[dt.datetime] = None) -> Optional[dt.datetime]:
    if container:
        time_tag = container.find("time") if hasattr(container, "find") else None
        if time_tag:
            parsed = parse_datetime(time_tag.get("datetime") or time_tag.get("title") or time_tag.get_text(" "), now)
            if parsed:
                return parsed
        for attr in ["datetime", "data-date", "data-time", "data-published", "title"]:
            if hasattr(container, "get"):
                parsed = parse_datetime(container.get(attr), now)
                if parsed:
                    return parsed
        if hasattr(container, "get_text"):
            parsed = extract_date_from_text(container.get_text(" ")[:900], now)
            if parsed:
                return parsed
    return page_date


def format_date(value: Any) -> str:
    parsed = parse_datetime(value)
    if not parsed:
        return "未确认"
    return to_sg(parsed).strftime("%Y-%m-%d %H:%M SGT")


def item_age_days(item: Dict[str, Any], now: Optional[dt.datetime] = None) -> Optional[float]:
    now = now or now_sg()
    parsed = parse_datetime(item.get("published_at"), now)
    if not parsed:
        return None
    delta = now - to_sg(parsed)
    return max(0.0, delta.total_seconds() / 86400)


def age_label(age_days: Optional[float]) -> str:
    if age_days is None:
        return "无法确认"
    hours = age_days * 24
    if hours < 1:
        return "1小时内"
    if hours < 24:
        return f"{int(hours)}小时前"
    if hours < 2 * 24:
        return "1天前"
    return f"{int(age_days)}天前"


def freshness_window(category: str) -> float:
    return float(FRESHNESS_DAYS.get(category, env_int("FRESHNESS_WINDOW_DAYS", 7)))


def freshness_window_hours(category: str) -> int:
    return int(round(freshness_window(category) * 24))


def freshness_window_label(category: str) -> str:
    days = freshness_window(category)
    if days < 1:
        return f"{int(round(days * 24))}小时"
    if abs(days - int(days)) < 0.001:
        return f"{int(days)}天"
    return f"{days:.1f}天"


def freshness_window_label(category: str) -> str:
    return f"{freshness_window_hours(category)}小时"


def is_current_item(item: Dict[str, Any], now: Optional[dt.datetime] = None) -> Tuple[bool, str]:
    now = now or now_sg()
    category = item.get("intelligence_category") or "第三方网站"
    max_days = freshness_window(category)
    max_hours = freshness_window_hours(category)
    age = item_age_days(item, now)
    if age is None:
        if UNKNOWN_DATE_POLICY == "include":
            return True, "未确认发布时间；已按 UNKNOWN_DATE_POLICY=include 纳入"
        return False, "无法确认发布时间，已从主面板隐藏"
    if age <= max_days:
        return True, f"{age_label(age)}，在 {max_hours} 小时时效窗口内"
    return False, f"{age_label(age)}，超过 {max_hours} 小时时效窗口"


def reddit_listing_parts(url: str) -> Dict[str, str]:
    parsed = urlparse(url)
    path = parsed.path or "/"
    query = parsed.query
    path = re.sub(r"/+$", "", path)
    if re.match(r"^/r/[^/]+$", path, flags=re.I):
        path = path + "/new"
    if not path.startswith("/r/"):
        return {}
    if path.endswith(".json"):
        path = path[:-5]
    if path.endswith(".rss"):
        path = path[:-4]
    return {"path": path, "query": query}


def reddit_candidate_urls(url: str) -> Dict[str, List[str]]:
    parts = reddit_listing_parts(url)
    if not parts:
        return {"oauth": [], "json": [], "rss": [], "old_html": []}
    path = parts["path"]
    query = parts["query"]
    json_url = urlunparse(("https", "www.reddit.com", path + ".json", "", query, ""))
    rss_url = urlunparse(("https", "www.reddit.com", path + ".rss", "", query, ""))
    old_url = urlunparse(("https", "old.reddit.com", path + "/", "", query, ""))
    oauth_url = urlunparse(("https", "oauth.reddit.com", path, "", query, ""))
    alt_rss = []
    m = re.match(r"^/r/([^/]+)", path, flags=re.I)
    if m:
        alt_rss.append(f"https://www.reddit.com/r/{m.group(1)}/.rss?limit={MAX_ITEMS_PER_SOURCE}")
    return {
        "oauth": [with_limit(oauth_url)],
        "json": [with_limit(json_url)],
        "rss": [with_limit(rss_url)] + alt_rss,
        "old_html": [with_limit(old_url)],
    }


def get_reddit_token() -> Optional[str]:
    global _REDDIT_TOKEN, _REDDIT_TOKEN_EXPIRES
    if _REDDIT_TOKEN and time.time() < _REDDIT_TOKEN_EXPIRES - 60:
        return _REDDIT_TOKEN
    client_id = os.getenv("REDDIT_CLIENT_ID", "").strip()
    client_secret = os.getenv("REDDIT_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return None
    auth = requests.auth.HTTPBasicAuth(client_id, client_secret)
    headers = {"User-Agent": REDDIT_USER_AGENT}
    data = {"grant_type": "client_credentials"}
    resp = SESSION.post("https://www.reddit.com/api/v1/access_token", auth=auth, data=data, headers=headers, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()
    _REDDIT_TOKEN = payload.get("access_token")
    _REDDIT_TOKEN_EXPIRES = time.time() + int(payload.get("expires_in", 3600))
    return _REDDIT_TOKEN


def parse_reddit_json(data: Any, row: Dict[str, str], fallback_url: str) -> List[Dict[str, Any]]:
    children: List[Dict[str, Any]] = []
    if isinstance(data, dict):
        children = data.get("data", {}).get("children", []) or []
    elif isinstance(data, list) and data:
        children = data[0].get("data", {}).get("children", []) or []
    items: List[Dict[str, Any]] = []
    for child in children[:MAX_ITEMS_PER_SOURCE]:
        d = child.get("data", {}) if isinstance(child, dict) else {}
        title = clean_text(d.get("title"))
        if not title:
            continue
        permalink = d.get("permalink") or ""
        item_url = "https://www.reddit.com" + permalink if permalink.startswith("/") else clean_text(d.get("url") or fallback_url)
        created = d.get("created_utc")
        created_iso = to_iso(parse_datetime(str(created))) if created else ""
        items.append(
            {
                "title": title,
                "snippet": clean_text(d.get("selftext") or d.get("link_flair_text") or "")[:520],
                "url": item_url,
                "published_at": created_iso,
                "source_name": row.get("来源名称", "Reddit"),
                "platform": "Reddit",
                "source_category": row.get("来源类别", "社区"),
                "raw_score": int(d.get("score") or 0),
                "comments": int(d.get("num_comments") or 0),
                "fetch_method": "reddit_json_or_oauth",
            }
        )
    return items


def parse_reddit_rss(text: str, row: Dict[str, str], feed_url: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(text, "xml")
    entries = soup.find_all("entry") or soup.find_all("item")
    items: List[Dict[str, Any]] = []
    for entry in entries[:MAX_ITEMS_PER_SOURCE]:
        title = clean_text(entry.find_text("title"))
        if not title:
            continue
        link = ""
        link_tag = entry.find("link")
        if link_tag:
            link = clean_text(link_tag.get("href") or link_tag.get_text())
        snippet = clean_text(entry.find_text("summary") or entry.find_text("description") or entry.find_text("content"))[:520]
        published = clean_text(entry.find_text("updated") or entry.find_text("published") or entry.find_text("pubDate"))
        items.append(
            {
                "title": title,
                "snippet": snippet,
                "url": link or feed_url,
                "published_at": to_iso(parse_datetime(published)) if published else "",
                "source_name": row.get("来源名称", "Reddit"),
                "platform": "Reddit",
                "source_category": row.get("来源类别", "社区"),
                "fetch_method": "reddit_rss",
            }
        )
    return items


def parse_old_reddit_html(text: str, row: Dict[str, str], page_url: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(text, "html.parser")
    items: List[Dict[str, Any]] = []
    for thing in soup.select("div.thing")[:MAX_ITEMS_PER_SOURCE]:
        a = thing.select_one("a.title")
        if not a:
            continue
        title = clean_text(a.get_text(" "))
        href = clean_text(a.get("href"))
        if href.startswith("/"):
            href = "https://www.reddit.com" + href
        comments = thing.select_one("a.comments")
        if comments and comments.get("href"):
            href = clean_text(comments.get("href"))
        time_tag = thing.select_one("time") or thing.select_one(".live-timestamp")
        published = ""
        if time_tag:
            parsed = parse_datetime(time_tag.get("datetime") or time_tag.get("title") or time_tag.get_text(" "))
            published = to_iso(parsed) if parsed else ""
        items.append(
            {
                "title": title,
                "snippet": clean_text(thing.get_text(" "))[:520],
                "url": href or page_url,
                "published_at": published,
                "source_name": row.get("来源名称", "Reddit"),
                "platform": "Reddit",
                "source_category": row.get("来源类别", "社区"),
                "fetch_method": "old_reddit_html",
            }
        )
    return items


def fetch_reddit(row: Dict[str, str]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    url = row.get("URL/入口", "")
    candidates = reddit_candidate_urls(url)
    errors: List[str] = []
    try:
        token = get_reddit_token()
    except Exception as exc:
        token = None
        errors.append(f"oauth_token: {str(exc)[:120]}")
    if token:
        for api_url in candidates.get("oauth", []):
            try:
                resp = fetch_url(api_url, accept_json=True, reddit=True, bearer=token)
                items = parse_reddit_json(resp.json(), row, api_url)
                if items:
                    return items, {"status": "ok", "fetched": len(items), "method": "reddit_oauth", "url": api_url}
                errors.append("oauth: empty")
            except Exception as exc:
                errors.append(f"oauth: {str(exc)[:120]}")
    for json_url in candidates.get("json", []):
        try:
            resp = fetch_url(json_url, accept_json=True, reddit=True)
            items = parse_reddit_json(resp.json(), row, json_url)
            if items:
                return items, {"status": "ok", "fetched": len(items), "method": "reddit_public_json", "url": json_url}
            errors.append("public_json: empty")
        except Exception as exc:
            errors.append(f"public_json: {str(exc)[:120]}")
    for rss_url in candidates.get("rss", []):
        try:
            resp = fetch_url(rss_url, reddit=True)
            items = parse_reddit_rss(resp.text, row, rss_url)
            if items:
                return items, {"status": "ok", "fetched": len(items), "method": "reddit_rss", "url": rss_url}
            errors.append("rss: empty")
        except Exception as exc:
            errors.append(f"rss: {str(exc)[:120]}")
    for old_url in candidates.get("old_html", []):
        try:
            resp = fetch_url(old_url, reddit=True)
            items = parse_old_reddit_html(resp.text, row, old_url)
            if items:
                return items, {"status": "ok", "fetched": len(items), "method": "old_reddit_html", "url": old_url}
            errors.append("old_html: empty")
        except Exception as exc:
            errors.append(f"old_html: {str(exc)[:120]}")
    reason = " | ".join(errors[:5]) or "所有 Reddit 兜底路径均无内容"
    return [], {"status": "failed", "reason": reason[:360], "method": "reddit_multifallback", "url": url}


def useful_title(text: str) -> bool:
    text = clean_text(text)
    if len(text) < 14 or len(text) > 220:
        return False
    low = text.lower()
    bad = {"privacy policy", "terms of service", "cookie policy", "log in", "login", "sign up", "subscribe", "menu", "home"}
    return not any(low == b or low.startswith(b + " ") for b in bad)


def extract_web_items(row: Dict[str, str], soup: BeautifulSoup, url: str) -> List[Dict[str, Any]]:
    now = now_sg()
    items: List[Dict[str, Any]] = []
    page_date = extract_page_date(soup, now)
    title = clean_text(soup.title.string if soup.title and soup.title.string else "")
    desc_tag = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    meta_desc = clean_text(desc_tag.get("content") if desc_tag and desc_tag.get("content") else "")

    seen = set()
    selectors = [
        "article a[href]", "li a[href]", "h1 a[href]", "h2 a[href]", "h3 a[href]",
        ".post a[href]", ".article a[href]", ".card a[href]", ".entry-title a[href]",
    ]
    for selector in selectors:
        for link in soup.select(selector)[:90]:
            text = clean_text(link.get_text(" "))
            if not useful_title(text):
                continue
            href = clean_text(link.get("href"))
            if not href or href.startswith("#") or href.lower().startswith("javascript"):
                continue
            href = urljoin(url, href)
            key = (re.sub(r"\W+", "", text.lower())[:100], href.split("?")[0].rstrip("/"))
            if key in seen:
                continue
            seen.add(key)
            container = link.find_parent(["article", "li", "div", "section"]) or link.parent
            item_dt = extract_item_date(container, page_date, now)
            snippet = clean_text(container.get_text(" ") if container else meta_desc)[:520]
            items.append(
                {
                    "title": text,
                    "snippet": snippet or meta_desc or title,
                    "url": href,
                    "published_at": to_iso(item_dt),
                    "source_name": row.get("来源名称", "Web"),
                    "platform": row.get("平台", "Web"),
                    "source_category": row.get("来源类别", "网页"),
                    "fetch_method": "html_listing",
                }
            )
            if len(items) >= MAX_ITEMS_PER_SOURCE:
                return items
    # Article/single-page fallback: only if page itself has a valid date.
    h1 = soup.find("h1")
    page_title = clean_text(h1.get_text(" ") if h1 else title)
    if useful_title(page_title):
        items.append(
            {
                "title": page_title[:220],
                "snippet": meta_desc or clean_text(soup.get_text(" "))[:520],
                "url": url,
                "published_at": to_iso(page_date),
                "source_name": row.get("来源名称", "Web"),
                "platform": row.get("平台", "Web"),
                "source_category": row.get("来源类别", "网页"),
                "fetch_method": "html_page",
            }
        )
    return items


def fetch_x_official(row: Dict[str, str]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Fetch recent posts from an official X/Twitter account if X_BEARER_TOKEN is configured."""
    url = row.get("URL/入口", "")
    parsed = urlparse(url)
    handle = parsed.path.strip("/").split("/")[0]
    if not handle or handle.lower() in {"search", "i"}:
        return [], {"status": "manual_required", "reason": "无法从 X/Twitter URL 识别官方账号 handle", "method": "x_recent_search_api"}
    if not X_BEARER_TOKEN:
        return [], {"status": "limited", "reason": "X/Twitter 官方账号需要 X_BEARER_TOKEN；未配置时不抓取，避免旧页面内容误入日报。", "method": "x_api_required"}
    try:
        endpoint = "https://api.twitter.com/2/tweets/search/recent"
        params = {
            "query": f"from:{handle} -is:retweet",
            "max_results": str(max(10, min(100, MAX_ITEMS_PER_SOURCE))),
            "tweet.fields": "created_at,public_metrics,lang",
        }
        headers = {"Authorization": f"Bearer {X_BEARER_TOKEN}", "User-Agent": USER_AGENT}
        resp = SESSION.get(endpoint, params=params, headers=headers, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        items: List[Dict[str, Any]] = []
        for t in data.get("data", [])[:MAX_ITEMS_PER_SOURCE]:
            text = clean_text(t.get("text"))
            if not text:
                continue
            tid = clean_text(t.get("id"))
            created = clean_text(t.get("created_at"))
            metrics = t.get("public_metrics", {}) or {}
            items.append({
                "title": text[:180],
                "snippet": text,
                "url": f"https://x.com/{handle}/status/{tid}" if tid else url,
                "published_at": to_iso(parse_datetime(created)),
                "source_name": row.get("来源名称", f"X @{handle}"),
                "platform": "X/Twitter",
                "source_category": row.get("来源类别", "竞品官方社媒"),
                "fetch_method": "x_recent_search_api",
                "raw_score": safe_int(metrics.get("like_count"), 0),
                "comments": safe_int(metrics.get("reply_count"), 0),
            })
        return items, {"status": "ok", "fetched": len(items), "method": "x_recent_search_api"}
    except Exception as exc:
        return [], {"status": "failed", "reason": str(exc)[:260], "method": "x_recent_search_api"}


def fetch_youtube_light(row: Dict[str, str]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    url = row.get("URL/入口", "")
    api_key = os.getenv("YOUTUBE_API_KEY", "").strip()
    query = "best vpn uk"
    parsed = urlparse(url)
    params = dict(parse_qsl(parsed.query))
    if params.get("search_query"):
        query = params["search_query"].replace("+", " ")
    if api_key:
        try:
            endpoint = (
                "https://www.googleapis.com/youtube/v3/search"
                f"?part=snippet&type=video&maxResults={MAX_ITEMS_PER_SOURCE}&order=date&q={quote_plus(query)}&key={api_key}"
            )
            resp = fetch_url(endpoint, accept_json=True)
            data = resp.json()
            items = []
            for v in data.get("items", []):
                sn = v.get("snippet", {})
                vid = v.get("id", {}).get("videoId", "")
                items.append(
                    {
                        "title": clean_text(sn.get("title")),
                        "snippet": clean_text(sn.get("description"))[:520],
                        "url": f"https://www.youtube.com/watch?v={vid}" if vid else url,
                        "published_at": to_iso(parse_datetime(sn.get("publishedAt"))),
                        "source_name": row.get("来源名称", "YouTube"),
                        "platform": "YouTube",
                        "source_category": row.get("来源类别", "视频"),
                        "fetch_method": "youtube_api",
                    }
                )
            return items, {"status": "ok", "fetched": len(items), "method": "youtube_api"}
        except Exception as exc:
            return [], {"status": "failed", "reason": str(exc)[:220], "method": "youtube_api"}
    return [], {"status": "limited", "reason": "YouTube 搜索若需稳定发布时间与原视频链接，建议配置 YOUTUBE_API_KEY；未配置时不纳入重点，避免旧视频混入。", "method": "youtube_api_missing"}


def fetch_web(row: Dict[str, str]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    url = row.get("URL/入口", "")
    if SKIP_NETWORK:
        return [], {"status": "skipped", "reason": "SKIP_NETWORK=1，仅生成预览", "method": "preview"}
    if not url.startswith("http"):
        return [], {"status": "manual_required", "reason": "没有可直接抓取的公开 URL"}
    platform = row.get("平台", "").lower()
    name = row.get("来源名称", "").lower()
    if "twitter" in platform or "x 搜索" in name or "x.com" in url or "twitter.com" in url:
        if source_taxonomy(row) == "社交媒体":
            return fetch_x_official(row)
        return [], {"status": "limited", "reason": "X/Twitter 泛搜索需要官方 API/监听工具或手工补充；未抓取时不生成旧信息。", "method": "x_api_required"}
    if "tiktok" in platform or "tiktok.com" in url:
        return [], {"status": "limited", "reason": "TikTok 搜索页反爬较强；建议用 manual_inputs.csv 补充当天官方贴。", "method": "tiktok_manual"}
    if "youtube" in platform or "youtube.com/results" in url:
        return fetch_youtube_light(row)
    if "reddit.com" in url or "old.reddit.com" in url:
        return fetch_reddit(row)
    try:
        resp = fetch_url(url)
        ctype = resp.headers.get("content-type", "")
        if "json" in ctype:
            data = resp.json()
            title = clean_text(data.get("title") if isinstance(data, dict) else row.get("来源名称"))
            published = ""
            if isinstance(data, dict):
                for key in ["published_at", "publishedAt", "datePublished", "dateModified", "updated_at"]:
                    parsed = parse_datetime(data.get(key))
                    if parsed:
                        published = to_iso(parsed)
                        break
            return [
                {
                    "title": title or row.get("来源名称", url),
                    "snippet": clean_text(str(data))[:520],
                    "url": url,
                    "published_at": published,
                    "source_name": row.get("来源名称", "Web"),
                    "platform": row.get("平台", "Web"),
                    "source_category": row.get("来源类别", "网页"),
                    "fetch_method": "json",
                }
            ], {"status": "ok", "fetched": 1, "method": "json"}
        soup = BeautifulSoup(resp.text, "html.parser")
        items = extract_web_items(row, soup, resp.url)
        return items, {"status": "ok", "fetched": len(items), "method": "html", "url": resp.url}
    except Exception as exc:
        return [], {"status": "failed", "reason": str(exc)[:260], "method": "html"}


def read_manual_inputs(today: dt.date) -> List[Dict[str, Any]]:
    rows = read_csv_dicts(MANUAL_CSV)
    items: List[Dict[str, Any]] = []
    for row in rows:
        title = clean_text(row.get("标题"))
        source_label = clean_text(row.get("来源"))
        if not title or "示例" in title or "example.com" in clean_text(row.get("链接")):
            continue
        date_text = clean_text(row.get("发布时间") or row.get("日期")) or today.isoformat()
        parsed = parse_datetime(date_text)
        date_iso = to_iso(parsed) if parsed else today.isoformat()
        if parsed and to_sg(parsed).date() != today:
            continue
        items.append(
            {
                "title": title,
                "snippet": clean_text(row.get("摘要")),
                "url": clean_text(row.get("链接")) or "#",
                "published_at": date_iso,
                "source_name": source_label or "手工补充",
                "platform": "Manual",
                "source_category": "手工补充",
                "manual_subcategory": clean_text(row.get("子分类") or row.get("主题")),
                "manual_score": clean_text(row.get("重要性分")),
                "manual_taxonomy": clean_text(row.get("信息类别") or row.get("面板类别") or row.get("分类") or row.get("情报大类")),
                "fetch_method": "manual_inputs",
            }
        )
    return items


def enrich_item(item: Dict[str, Any], source_lookup: Dict[str, Dict[str, str]]) -> Dict[str, Any]:
    source_name = item.get("source_name", "")
    row = source_lookup.get(source_name, {})
    combined = " ".join(
        [
            clean_text(item.get("title")),
            clean_text(item.get("snippet")),
            clean_text(row.get("来源名称")),
            clean_text(row.get("备注")),
            clean_text(row.get("关键信息")),
            clean_text(row.get("信息价值")),
            clean_text(row.get("情报大类")),
            clean_text(row.get("URL/入口")),
        ]
    )
    category = normalize_category(item.get("manual_taxonomy")) or guess_category_from_item(item, row)
    if category not in TAXONOMY_ORDER:
        category = "第三方网站"
    issue, issue_hits = classify_issue(combined)
    subcategory, sub_hits = infer_subcategory(category, combined)
    if item.get("manual_subcategory"):
        subcategory = item["manual_subcategory"]
    base = source_priority(row) or 5
    layer_bonus = 8 if "核心" in row.get("日报层级", "") else 4 if "周更" in row.get("日报层级", "") else 0
    freq_bonus = 5 if "每日" in row.get("追踪频率", "") else 2 if "每周" in row.get("追踪频率", "") else 0
    engagement_bonus = min(8, safe_int(item.get("comments"), 0) // 5) + min(5, safe_int(item.get("raw_score"), 0) // 10)
    issue_bonus = min(14, (len(issue_hits) + len(sub_hits)) * 3)
    source_type_bonus = {"政策风险": 10, "reddit讨论": 8, "竞品动态": 8, "社交媒体": 6, "第三方网站": 5}.get(category, 4)
    manual_score = safe_int(item.get("manual_score"), 0)
    score = manual_score or min(99, 36 + base * 4 + layer_bonus + freq_bonus + engagement_bonus + issue_bonus + source_type_bonus)
    age = item_age_days(item)
    current_ok, current_reason = is_current_item({**item, "intelligence_category": category})
    item.update(
        {
            "intelligence_category": category,
            "subcategory": subcategory,
            "issue": issue,
            "matched_keywords": list(dict.fromkeys(issue_hits + sub_hits))[:10],
            "importance_score": int(score),
            "priority": priority_label(score),
            "audience": row.get("目标用户", "public") or "public",
            "source_note": row.get("备注", ""),
            "source_strategy": row.get("采集策略", ""),
            "evidence_role": row.get("证据角色", ""),
            "age_days": round(age, 2) if age is not None else None,
            "age_label": age_label(age),
            "freshness_window_days": freshness_window(category),
            "freshness_window_label": freshness_window_label(category),
            "freshness_window_hours": freshness_window_hours(category),
            "is_current": current_ok,
            "freshness_reason": current_reason,
        }
    )
    return item


def dedupe_items(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out = []
    for item in items:
        title_key = re.sub(r"\W+", "", clean_text(item.get("title", "")).lower())[:120]
        url_key = clean_text(item.get("url", "")).lower().split("?")[0].rstrip("/")
        key = url_key or title_key
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def filter_current_items(items: List[Dict[str, Any]], now: dt.datetime) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    current: List[Dict[str, Any]] = []
    filtered: List[Dict[str, Any]] = []
    for item in items:
        ok, reason = is_current_item(item, now)
        item["is_current"] = ok
        item["freshness_reason"] = reason
        if ok:
            current.append(item)
        else:
            filtered.append(item)
    return current, filtered


def normalized_issue_key(text: str) -> str:
    low = text.lower()
    for key, kws in [
        ("policy-age-assurance", ["age assurance", "age verification", "under-16", "under 16", "online safety", "ofcom", "ico", "children"]),
        ("streaming-detection", ["netflix", "iplayer", "streaming", "geo", "geoblock", "detected", "unblock"]),
        ("pricing-refund-cancel", ["price", "pricing", "refund", "cancel", "renewal", "subscription", "coupon", "deal"]),
        ("performance-connection", ["slow", "speed", "latency", "disconnect", "server", "captcha", "ip reputation", "blocked"]),
        ("privacy-audit", ["privacy", "no-log", "no logs", "audit", "transparency", "webrtc", "dns leak", "leak"]),
        ("product-update", ["release", "feature", "update", "app", "protocol", "extension"]),
        ("review-ranking", ["best vpn", "review", "ranking", "comparison", "top vpn", "guide"]),
        ("public-wifi-travel-student", ["wifi", "wi-fi", "airport", "hotel", "travel", "student", "university", "uni"]),
    ]:
        if any(k in low for k in kws):
            return key
    tokens = re.findall(r"[a-z][a-z0-9_-]{2,}|[\u4e00-\u9fff]{2,}", low)
    tokens = [t for t in tokens if t not in STOP_WORDS][:8]
    return "generic-" + "-".join(tokens[:4]) if tokens else "generic"


def cluster_key(item: Dict[str, Any]) -> str:
    text = " ".join([item.get("title", ""), item.get("snippet", ""), item.get("source_name", "")])
    return f"{item.get('intelligence_category')}::{item.get('subcategory')}::{normalized_issue_key(text)}"


def make_finding_title(category: str, subcategory: str, issue: str, examples: List[Dict[str, Any]]) -> str:
    if examples:
        top_title = clean_text(examples[0].get("title"))
        if top_title:
            return f"{subcategory}｜{top_title[:110]}"
    return f"{subcategory}｜{issue or category}"


def build_source_links(items: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    seen = set()
    out: List[Dict[str, str]] = []
    for item in items:
        url = clean_text(item.get("url"))
        if not url or url == "#" or url in seen:
            continue
        seen.add(url)
        out.append(
            {
                "title": clean_text(item.get("title"))[:140],
                "url": url,
                "source": clean_text(item.get("source_name")),
                "published_at": format_date(item.get("published_at")),
                "age": clean_text(item.get("age_label")),
                "method": clean_text(item.get("fetch_method")),
            }
        )
    return out


def build_findings(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    clusters: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in items:
        clusters[cluster_key(item)].append(item)
    findings: List[Dict[str, Any]] = []
    for _, group in clusters.items():
        group_sorted = sorted(group, key=lambda x: (x.get("importance_score", 0), -float(x.get("age_days") or 999)), reverse=True)
        top = group_sorted[0]
        category = top.get("intelligence_category", "第三方网站")
        subcategory = top.get("subcategory", "综合观察")
        issue = top.get("issue", "综合观察")
        avg_score = sum(int(x.get("importance_score", 0)) for x in group_sorted[:6]) / max(1, min(6, len(group_sorted)))
        source_names = sorted({clean_text(x.get("source_name")) for x in group_sorted if x.get("source_name")})
        source_links = build_source_links(group_sorted)
        evidence = "；".join(dict.fromkeys([clean_text(x.get("title"))[:120] for x in group_sorted[:5] if x.get("title")]))
        dates = [parse_datetime(x.get("published_at")) for x in group_sorted if parse_datetime(x.get("published_at"))]
        latest = max(dates) if dates else None
        earliest = min(dates) if dates else None
        tags: List[str] = []
        for x in group_sorted:
            tags.extend(x.get("matched_keywords") or [])
            if x.get("issue"):
                tags.append(x.get("issue"))
            if x.get("subcategory"):
                tags.append(x.get("subcategory"))
        findings.append(
            {
                "priority": priority_label(avg_score + min(6, len(source_links) * 2)),
                "category": category,
                "subcategory": subcategory,
                "issue": issue,
                "title": make_finding_title(category, subcategory, issue, group_sorted),
                "evidence": evidence,
                "source_count": len(source_links),
                "source_names": source_names,
                "source_links": source_links,
                "url": source_links[0]["url"] if source_links else top.get("url", "#"),
                "latest_published_at": to_iso(latest),
                "earliest_published_at": to_iso(earliest),
                "freshness": f"最新 {format_date(to_iso(latest)) if latest else '未确认'}；窗口 {freshness_window_hours(category)} 小时",
                "importance_score": int(avg_score),
                "tags": [t for t, _ in Counter(tags).most_common(8)],
                "items": group_sorted[:12],
            }
        )
    rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    findings.sort(key=lambda x: (TAXONOMY_ORDER.index(x.get("category")) if x.get("category") in TAXONOMY_ORDER else 99, rank.get(x.get("priority", "P3"), 9), -x.get("source_count", 0), -x.get("importance_score", 0)))
    # Keep balanced coverage: top clusters per category.
    balanced: List[Dict[str, Any]] = []
    for cat in TAXONOMY_ORDER:
        balanced.extend([f for f in findings if f.get("category") == cat][:5])
    return balanced[:24]


def build_category_panels(items: List[Dict[str, Any]], sources: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    item_counts = Counter(x.get("intelligence_category", "第三方网站") for x in items)
    source_counts = Counter(source_taxonomy(s) for s in sources)
    panels = []
    for cat in TAXONOMY_ORDER:
        group = [x for x in items if x.get("intelligence_category") == cat]
        avg = int(sum(int(x.get("importance_score", 0)) for x in group) / len(group)) if group else 0
        subcats = Counter(x.get("subcategory", "综合") for x in group).most_common(5)
        panels.append(
            {
                "name": cat,
                "definition": TAXONOMY_META[cat]["definition"],
                "examples": TAXONOMY_META[cat]["examples"],
                "items": item_counts.get(cat, 0),
                "sources": source_counts.get(cat, 0),
                "heat": avg,
                "priority": priority_label(avg) if group else "无新信息",
                "subcategories": subcats,
                "freshness_days": freshness_window(cat),
                "freshness_label": freshness_window_label(cat),
                "freshness_hours": freshness_window_hours(cat),
            }
        )
    return panels


def build_stats(sources: List[Dict[str, str]], current_items: List[Dict[str, Any]], raw_items: List[Dict[str, Any]], filtered_items: List[Dict[str, Any]], statuses: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "total_sources": len(sources),
        "monitored_sources": sum(1 for s in sources if include_source(s)),
        "current_items": len(current_items),
        "raw_items": len(raw_items),
        "filtered_items": len(filtered_items),
        "sources_ok": sum(1 for s in statuses if s.get("status") == "ok"),
        "sources_limited": sum(1 for s in statuses if s.get("status") in {"limited", "manual_required", "skipped"}),
        "sources_failed": sum(1 for s in statuses if s.get("status") == "failed"),
        "reddit_oauth_ready": bool(os.getenv("REDDIT_CLIENT_ID") and os.getenv("REDDIT_CLIENT_SECRET")),
        "x_api_ready": bool(X_BEARER_TOKEN),
        "freshness_windows": {cat: f"{freshness_window_hours(cat)}小时" for cat in TAXONOMY_ORDER},
        "freshness_hours": FRESHNESS_HOURS,
        "freshness_days": FRESHNESS_DAYS,
    }


def build_limitations(statuses: List[Dict[str, Any]], filtered_items: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    limited = [s for s in statuses if s.get("status") in {"limited", "manual_required", "failed", "skipped"}]
    out = []
    for s in limited[:18]:
        source = clean_text(s.get("source_name"))
        reason = clean_text(s.get("reason")) or "抓取受限或暂无新内容"
        method = clean_text(s.get("method"))
        if "reddit" in method or "reddit" in clean_text(s.get("url")).lower():
            next_step = "已内置 OAuth/public JSON/RSS/old.reddit 兜底；若仍失败，配置 Reddit Secrets 或当天手工补原帖链接。"
        elif "x_" in method or "twitter" in reason.lower():
            next_step = "社交媒体官方账号建议接官方 API，或把当天官方帖粘到 manual_inputs.csv。"
        else:
            next_step = "换成 RSS/API/具体文章页，或在 manual_inputs.csv 手工补当天线索。"
        out.append({"source": source, "status": clean_text(s.get("status")), "method": method, "reason": reason, "next_step": next_step})
    filtered_counter = Counter(clean_text(x.get("freshness_reason")) for x in filtered_items)
    for reason, count in filtered_counter.most_common(8):
        out.append({"source": f"时效过滤 {count} 条", "status": "filtered", "method": "freshness_guard", "reason": reason, "next_step": f"不会进入重点卡；如需放宽，调整 .github/workflows/daily-update.yml 中 DASHBOARD_LOOKBACK_HOURS。"})
    return out[:30]


def sanitize_filtered_items(filtered_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep only metadata for hidden stale/undated items so old titles do not resurface in the panel or JSON."""
    return [
        {
            "source_name": clean_text(x.get("source_name")),
            "intelligence_category": clean_text(x.get("intelligence_category")),
            "published_at": clean_text(x.get("published_at")),
            "freshness_reason": clean_text(x.get("freshness_reason")),
            "age_label": clean_text(x.get("age_label")),
        }
        for x in filtered_items
    ]


def keyword_cloud(items: List[Dict[str, Any]]) -> List[Tuple[str, int]]:
    text = " ".join([clean_text(x.get("title", "")) + " " + clean_text(x.get("snippet", "")) for x in items])
    words = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}|[\u4e00-\u9fff]{2,}", text.lower())
    counter = Counter(w for w in words if w not in STOP_WORDS and len(w) <= 28)
    return counter.most_common(30)


def citation_links(findings: List[Dict[str, Any]], items: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    seen = set()
    out: List[Dict[str, str]] = []
    for f in findings:
        for link in f.get("source_links", []):
            url = clean_text(link.get("url"))
            if url.startswith("http") and url not in seen:
                out.append({"label": f"{link.get('source')}｜{link.get('title')}", "url": url, "role": f.get("category", "")})
                seen.add(url)
    for it in items[:80]:
        url = clean_text(it.get("url"))
        if url.startswith("http") and url not in seen:
            out.append({"label": clean_text(it.get("source_name")) + "｜" + clean_text(it.get("title"))[:80], "url": url, "role": it.get("intelligence_category", "")})
            seen.add(url)
    return out[:120]


def render_source_links(links: List[Dict[str, str]]) -> str:
    if not links:
        return "<p class='muted'>无可用原始链接</p>"
    rows = []
    for link in links:
        title = html.escape(clean_text(link.get("title")))
        url = html.escape(clean_text(link.get("url")))
        source = html.escape(clean_text(link.get("source")))
        date = html.escape(clean_text(link.get("published_at")))
        age = html.escape(clean_text(link.get("age")))
        rows.append(f"<li><a href='{url}' target='_blank' rel='noreferrer'>{title}</a><small>{source} · {date} · {age}</small></li>")
    return "<ol class='sourceList'>" + "".join(rows) + "</ol>"


def render_finding_card(f: Dict[str, Any]) -> str:
    def esc(x: Any) -> str:
        return html.escape(clean_text(x))
    p = esc(f.get("priority", "P2"))
    cls = p.lower()
    tags = "".join(f"<span>{esc(t)}</span>" for t in (f.get("tags") or [])[:8])
    links_html = render_source_links(f.get("source_links", []))
    return f"""
    <article class="card {cls}">
      <div class="meta"><span class="pill {cls}">{p}</span><span class="pill">{esc(f.get('category'))}</span><span class="pill">{esc(f.get('subcategory'))}</span><span class="pill">{esc(f.get('source_count'))} 个来源</span></div>
      <h3><a href="{esc(f.get('url'))}" target="_blank" rel="noreferrer">{esc(f.get('title'))}</a></h3>
      <p><b>统一要点：</b>{esc(f.get('evidence'))}</p>
      <p><b>时效：</b>{esc(f.get('freshness'))}</p>
      <div class="tags">{tags}</div>
      <h4>原始信息源</h4>
      {links_html}
    </article>
    """


def render_html(data: Dict[str, Any]) -> str:
    generated_at = html.escape(data.get("generated_at", ""))
    stats = data.get("stats", {})
    findings = data.get("findings", [])
    items = data.get("items", [])
    filtered_items = data.get("filtered_items", [])
    limitations = data.get("limitations", [])
    sources = data.get("sources", [])
    kw = data.get("keyword_cloud", [])
    categories = data.get("category_panels", [])
    links = data.get("citation_links", [])

    def esc(x: Any) -> str:
        return html.escape(clean_text(x))

    stat_cards = "".join(
        f"<div class='stat'><b>{esc(v)}</b><span>{esc(k)}</span></div>"
        for k, v in [
            ("信息源总数", stats.get("total_sources", 0)),
            ("本次监控", stats.get("monitored_sources", 0)),
            ("当前有效信息", stats.get("current_items", 0)),
            ("原始抓取", stats.get("raw_items", 0)),
            ("过滤旧/无日期", stats.get("filtered_items", 0)),
            ("受限/失败来源", stats.get("sources_limited", 0) + stats.get("sources_failed", 0)),
        ]
    )
    reddit_note = "已配置 Reddit OAuth" if stats.get("reddit_oauth_ready") else "未配置 Reddit OAuth，使用 public JSON/RSS/old.reddit 兜底；仍失败时走 manual_inputs.csv"
    x_note = "X API 已配置" if stats.get("x_api_ready") else "X API 未配置，官方社媒需手工补充或配置 X_BEARER_TOKEN"

    category_cards = "".join(
        f"<article class='taxonomy'><div class='taxHead'><b>{esc(c.get('name'))}</b><span>{esc(c.get('priority'))}</span></div><p>{esc(c.get('definition'))}</p><small>{esc(c.get('examples'))}</small><div class='taxNums'><i>来源 {esc(c.get('sources'))}</i><i>当前信息 {esc(c.get('items'))}</i><i>窗口 {esc(c.get('freshness_hours'))} 小时</i></div></article>"
        for c in categories
    )
    finding_cards = "".join(render_finding_card(f) for f in findings) or "<div class='empty'>当前时效窗口内没有抓到可确认发布时间的新信息。旧信息和无日期信息不会进入重点卡。</div>"
    keywords_html = "".join(f"<span>{esc(k)} · {v}</span>" for k, v in kw) or "<span>暂无关键词</span>"
    item_rows = "".join(
        f"<tr><td><a href='{esc(it.get('url'))}' target='_blank' rel='noreferrer'>{esc(it.get('title'))}</a><small>{esc(it.get('snippet'))}</small></td><td>{esc(it.get('source_name'))}<small>{esc(it.get('fetch_method'))}</small></td><td>{esc(it.get('intelligence_category'))}<small>{esc(it.get('subcategory'))}｜{esc(it.get('issue'))}</small></td><td>{esc(format_date(it.get('published_at')))}<small>{esc(it.get('age_label'))}；窗口 {esc(it.get('freshness_window_hours'))} 小时</small></td><td><b>{esc(it.get('priority'))}</b><br>{esc(it.get('importance_score'))}</td></tr>"
        for it in items[:120]
    ) or "<tr><td colspan='5'>当前时效窗口内暂无有效条目。</td></tr>"
    filtered_summary = Counter(clean_text(it.get("freshness_reason")) or "其他原因" for it in filtered_items)
    filtered_rows = "".join(
        f"<tr><td>{esc(reason)}</td><td>{esc(count)}</td></tr>"
        for reason, count in filtered_summary.most_common(12)
    ) or "<tr><td colspan='2'>没有被过滤的旧信息或无日期信息。</td></tr>"
    limitation_rows = "".join(
        f"<tr><td>{esc(l.get('source'))}</td><td>{esc(l.get('status'))}<small>{esc(l.get('method'))}</small></td><td>{esc(l.get('reason'))}</td><td>{esc(l.get('next_step'))}</td></tr>"
        for l in limitations
    ) or "<tr><td colspan='4'>暂无受限来源。</td></tr>"
    source_rows = "".join(
        f"<tr><td><a href='{esc(s.get('URL/入口'))}' target='_blank' rel='noreferrer'>{esc(s.get('来源名称'))}</a><small>{esc(s.get('备注'))}</small></td><td>{esc(source_taxonomy(s))}</td><td>{esc(s.get('平台'))}</td><td>{esc(s.get('追踪频率'))}</td><td>{esc(s.get('日报层级'))}</td><td>{esc(s.get('采集策略'))}</td></tr>"
        for s in sources[:220]
    )
    link_rows = "".join(
        f"<tr><td>{esc(l.get('label'))}</td><td>{esc(l.get('role'))}</td><td><a href='{esc(l.get('url'))}' target='_blank' rel='noreferrer'>{esc(l.get('url'))}</a></td></tr>"
        for l in links
    ) or "<tr><td colspan='3'>当前没有证据链接。</td></tr>"
    windows = stats.get("freshness_windows", {})
    window_text = "；".join(f"{k} {v}" for k, v in windows.items())

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>VPN 信息源观察日报</title>
<style>
:root {{ --bg:#08111f; --panel:#111c35; --panel2:#0c1628; --line:#22314f; --text:#e5eefb; --muted:#94a3b8; --accent:#60a5fa; --good:#34d399; --warn:#fbbf24; --bad:#fb7185; }}
* {{ box-sizing:border-box; }} body {{ margin:0; background:linear-gradient(180deg,#08111f,#0b1324 46%,#0f172a); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Noto Sans SC','PingFang SC',Arial,sans-serif; }}
a {{ color:#93c5fd; text-decoration:none; }} a:hover {{ text-decoration:underline; }} header,main {{ width:min(1240px,94vw); margin:auto; }} header {{ padding:38px 0 24px; }} .kicker {{ color:var(--good); font-size:13px; letter-spacing:.14em; text-transform:uppercase; }} h1 {{ margin:10px 0; font-size:38px; }} h2 {{ margin:0 0 14px; font-size:22px; }} h3 {{ margin:12px 0 10px; font-size:19px; line-height:1.45; }} h4 {{ margin:15px 0 8px; font-size:14px; color:#dbeafe; }} .lead {{ color:#cbd5e1; max-width:980px; line-height:1.7; }} .notice {{ background:#172554; border:1px solid #2a4b8d; padding:12px 14px; border-radius:14px; color:#dbeafe; margin-top:14px; }}
.grid {{ display:grid; gap:16px; }} .stats {{ grid-template-columns:repeat(6,1fr); margin:18px 0 28px; }} .stat,.card,.panel,.taxonomy {{ background:rgba(17,28,53,.88); border:1px solid var(--line); border-radius:18px; box-shadow:0 18px 60px rgba(0,0,0,.18); }} .stat {{ padding:16px; }} .stat b {{ display:block; font-size:26px; color:#fff; }} .stat span {{ color:var(--muted); font-size:13px; }} .section {{ margin:22px 0; }} .taxgrid {{ grid-template-columns:repeat(5,1fr); }} .taxonomy {{ padding:15px; }} .taxHead {{ display:flex; justify-content:space-between; gap:10px; align-items:center; }} .taxonomy p {{ color:#dbeafe; line-height:1.55; min-height:74px; }} .taxonomy small {{ color:var(--muted); line-height:1.5; display:block; }} .taxNums {{ display:flex; gap:7px; flex-wrap:wrap; margin-top:12px; }} .taxNums i {{ font-style:normal; background:#0c1628; border:1px solid var(--line); border-radius:999px; padding:4px 8px; color:#cbd5e1; font-size:12px; }}
.findings {{ grid-template-columns:repeat(2,1fr); }} .card {{ padding:18px; }} .card.p0 {{ border-color:#f59e0b; }} .card.p1 {{ border-color:#60a5fa; }} .meta,.tags {{ display:flex; flex-wrap:wrap; gap:7px; }} .pill,.tags span {{ background:#0c1628; border:1px solid var(--line); color:#cbd5e1; border-radius:999px; padding:5px 9px; font-size:12px; }} .pill.p0 {{ background:rgba(251,191,36,.18); border-color:#f59e0b; color:#fde68a; }} .pill.p1 {{ background:rgba(96,165,250,.16); border-color:#3b82f6; color:#bfdbfe; }} .pill.p2 {{ background:rgba(52,211,153,.14); border-color:#10b981; color:#bbf7d0; }} .card p {{ line-height:1.65; color:#dbeafe; }} .sourceList {{ padding-left:22px; margin:8px 0 0; }} .sourceList li {{ margin:8px 0; line-height:1.45; }} .sourceList small {{ display:block; color:var(--muted); margin-top:3px; }}
.two {{ grid-template-columns:.8fr 1.2fr; }} .panel {{ padding:18px; overflow:auto; }} table {{ width:100%; border-collapse:collapse; }} th,td {{ text-align:left; vertical-align:top; padding:12px; border-bottom:1px solid var(--line); }} th {{ color:#dbeafe; font-size:13px; }} td {{ color:#cbd5e1; font-size:13px; line-height:1.55; }} td small {{ display:block; color:var(--muted); margin-top:5px; }} .keywords {{ display:flex; flex-wrap:wrap; gap:8px; }} .keywords span {{ background:#0c1628; border:1px solid var(--line); color:#cbd5e1; border-radius:999px; padding:6px 10px; font-size:12px; }} .empty {{ padding:22px; border:1px dashed #334155; border-radius:16px; color:#cbd5e1; background:#0c1628; }} .muted,.footer {{ color:var(--muted); }} .footer {{ font-size:12px; margin:28px 0 40px; line-height:1.7; }}
@media (max-width: 1100px) {{ .stats,.taxgrid {{ grid-template-columns:repeat(2,1fr); }} .findings,.two {{ grid-template-columns:1fr; }} }} @media (max-width: 680px) {{ .stats,.taxgrid {{ grid-template-columns:1fr; }} h1 {{ font-size:28px; }} }}
</style>
</head>
<body>
<header>
  <div class="kicker">Daily Source Intelligence Panel · v3</div>
  <h1>VPN 信息源观察日报</h1>
  <div class="lead">面板只展示最近 {esc(stats.get('freshness_hours', FRESHNESS_HOURS))} 小时内、能确认发布时间的信息；无法确认发布时间或超过窗口的信息不会进入重点卡。多个来源反映同一件事时，合并成一个统一要点，并在要点后附全部原始链接。</div>
  <div class="lead">更新时间：{generated_at}</div>
  <div class="notice">时效窗口：{esc(window_text)}。Reddit 状态：{esc(reddit_note)}。社媒状态：{esc(x_note)}</div>
</header>
<main>
  <section class="grid stats">{stat_cards}</section>

  <section class="section">
    <h2>5 类信息源分类</h2>
    <div class="grid taxgrid">{category_cards}</div>
  </section>

  <section class="section">
    <h2>当前有效信息要点</h2>
    <div class="grid findings">{finding_cards}</div>
  </section>

  <section class="section grid two">
    <div class="panel">
      <h2>高频关键词</h2>
      <div class="keywords">{keywords_html}</div>
    </div>
    <div class="panel">
      <h2>时效过滤说明</h2>
      <p class="lead">被过滤的信息不会出现在日报重点里，原因通常是发布时间超过窗口，或网页没有可验证发布时间。</p>
      <table><thead><tr><th>过滤原因</th><th>数量</th></tr></thead><tbody>{filtered_rows}</tbody></table>
    </div>
  </section>

  <section class="section panel">
    <h2>当前原始信息列表</h2>
    <table><thead><tr><th>信息</th><th>来源/方法</th><th>分类/子分类</th><th>时效</th><th>优先级</th></tr></thead><tbody>{item_rows}</tbody></table>
  </section>

  <section class="section panel">
    <h2>受限/失败来源与处理方式</h2>
    <table><thead><tr><th>来源</th><th>状态/方法</th><th>原因</th><th>处理</th></tr></thead><tbody>{limitation_rows}</tbody></table>
  </section>

  <section class="section panel">
    <h2>信息源池</h2>
    <table><thead><tr><th>来源</th><th>分类</th><th>平台</th><th>频率</th><th>层级</th><th>采集策略</th></tr></thead><tbody>{source_rows}</tbody></table>
  </section>

  <section class="section panel">
    <h2>证据来源索引</h2>
    <table><thead><tr><th>来源链接</th><th>分类</th><th>URL</th></tr></thead><tbody>{link_rows}</tbody></table>
  </section>

  <div class="footer">说明：日报面板按来源类型分类，而不是按观点分类。社媒平台、Reddit、应用商店和部分搜索页可能需要 API、登录或手工补充；系统默认不把无法确认日期的内容放入重点，以避免旧信息污染日报。</div>
</main>
</body>
</html>"""


def write_markdown_report(data: Dict[str, Any], path: Path) -> None:
    lines = [
        f"# VPN 信息源观察日报 - {data.get('date')}",
        "",
        f"生成时间：{data.get('generated_at')}",
        "",
        "## 时效规则",
        "- 只展示当前窗口内、能确认发布时间的信息。",
        "- 默认所有类别只展示最近 36 小时内且能确认发布时间的信息；可在 GitHub Actions 变量中覆盖 DASHBOARD_LOOKBACK_HOURS。",
        "",
        "## 当前有效信息要点",
    ]
    for f in data.get("findings", []):
        lines.extend([
            "",
            f"### {f.get('priority')}｜{f.get('category')}｜{f.get('subcategory')}",
            f"- 要点：{f.get('evidence')}",
            f"- 时效：{f.get('freshness')}",
            "- 原始来源：",
        ])
        for link in f.get("source_links", []):
            lines.append(f"  - {link.get('source')}｜{link.get('published_at')}｜{link.get('title')}：{link.get('url')}")
    if not data.get("findings"):
        lines.append("当前时效窗口内没有抓到可确认发布时间的新信息。")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    start = time.time()
    today_dt = now_sg()
    today = today_dt.date()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_DIR.mkdir(parents=True, exist_ok=True)

    sources = read_csv_dicts(SOURCES_CSV)
    source_lookup = {s.get("来源名称", ""): s for s in sources}
    monitored = [s for s in sources if include_source(s)][:MAX_SOURCES_PER_RUN]

    raw_items: List[Dict[str, Any]] = []
    statuses: List[Dict[str, Any]] = []

    def fetch_one(row: Dict[str, str]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        name = row.get("来源名称", "未知来源")
        try:
            fetched, status = fetch_web(row)
            status.update({"source_name": name, "platform": row.get("平台", ""), "url": row.get("URL/入口", ""), "intelligence_category": source_taxonomy(row)})
            for it in fetched:
                it.setdefault("source_name", name)
            return fetched, status
        except Exception as exc:
            return [], {"source_name": name, "status": "failed", "reason": str(exc)[:260], "trace": traceback.format_exc()[:1200], "intelligence_category": source_taxonomy(row)}

    if monitored:
        with ThreadPoolExecutor(max_workers=max(1, MAX_WORKERS)) as pool:
            future_map = {pool.submit(fetch_one, row): row for row in monitored}
            for future in as_completed(future_map):
                fetched, status = future.result()
                statuses.append(status)
                raw_items.extend(fetched)

    manual_items = read_manual_inputs(today)
    if manual_items:
        statuses.append({"source_name": "manual_inputs.csv", "status": "ok", "fetched": len(manual_items), "method": "manual"})
        raw_items.extend(manual_items)

    raw_items = dedupe_items(raw_items)
    enriched = [enrich_item(it, source_lookup) for it in raw_items]
    current_items, filtered_items = filter_current_items(enriched, today_dt)
    current_items.sort(key=lambda x: (TAXONOMY_ORDER.index(x.get("intelligence_category")) if x.get("intelligence_category") in TAXONOMY_ORDER else 99, -int(x.get("importance_score", 0))), reverse=False)

    findings = build_findings(current_items)
    category_panels = build_category_panels(current_items, sources)
    stats = build_stats(sources, current_items, enriched, filtered_items, statuses)
    limitations = build_limitations(statuses, filtered_items)
    filtered_items_public = sanitize_filtered_items(filtered_items)

    data = {
        "date": today.isoformat(),
        "generated_at": today_dt.strftime("%Y-%m-%d %H:%M:%S Asia/Singapore"),
        "stats": stats,
        "taxonomy": TAXONOMY,
        "category_panels": category_panels,
        "findings": findings,
        "items": current_items[:180],
        "filtered_items": filtered_items_public[:180],
        "limitations": limitations,
        "sources": sources,
        "source_statuses": statuses,
        "citation_links": citation_links(findings, current_items),
        "keyword_cloud": keyword_cloud(current_items),
        "runtime_seconds": round(time.time() - start, 2),
    }

    (DATA_DIR / "latest.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    (ARCHIVE_DIR / f"{today.isoformat()}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    (DOCS_DIR / "index.html").write_text(render_html(data), encoding="utf-8")
    write_markdown_report(data, REPORT_DIR / f"{today.isoformat()}.md")
    (STATUS_DIR / "last_run.json").write_text(
        json.dumps(
            {
                "date": today.isoformat(),
                "generated_at": data["generated_at"],
                "ok": True,
                "stats": stats,
                "runtime_seconds": data["runtime_seconds"],
                "reddit_oauth_ready": stats["reddit_oauth_ready"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Generated dashboard v3 with {len(current_items)} current items, {len(filtered_items)} filtered items, from {len(monitored)} monitored sources.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
