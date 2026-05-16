"""tixcraft HTML 解析器 — 用於 API 模式，直接解析 HTTP 回應的 HTML"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser


def split_match_keywords(raw: str) -> list[str]:
    """將以 |、逗號、分號、換行分隔的關鍵字拆開。"""
    if not raw:
        return []
    return [part.strip() for part in re.split(r"[|,\n;，；]+", raw) if part.strip()]


def _parse_quoted_keyword_groups(raw: str) -> list[str] | None:
    """嘗試用 ticket_hunter 式 JSON 引號格式解析，例如:
        '"NT$7,880"'                → ['NT$7,880']
        '"VIP","NT$7,880"'          → ['VIP', 'NT$7,880']
        '"VIP 1F"'                  → ['VIP 1F']  (空白 = AND)
    解析失敗回傳 None（讓呼叫端 fallback）。
    """
    s = raw.strip()
    if not (s.startswith('"') and s.endswith('"')):
        return None
    try:
        groups = json.loads("[" + s + "]")
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(groups, list) or not all(isinstance(g, str) for g in groups):
        return None
    return groups


def matches_any_keyword(text: str, raw_keywords: str) -> bool:
    """檢查文字是否命中任一關鍵字。

    支援兩種語法：
    1. JSON 引號格式（推薦，逗號可安全使用於 keyword 內）：
       `"NT$7,880","NT$8,800"` — OR 跨字串、AND 同字串內以空白分隔
    2. 傳統分隔符格式（向後相容）：
       `VIP|搖滾|A區` — `|` / `,` / `;` / 換行皆為 OR 分隔符
    """
    if not raw_keywords:
        return False

    groups = _parse_quoted_keyword_groups(raw_keywords)
    if groups is not None:
        for group in groups:
            if not group:
                return True  # 空字串 = match all（與 ticket_hunter 一致）
            tokens = group.split()
            if tokens and all(t in text for t in tokens):
                return True
        return False

    keywords = split_match_keywords(raw_keywords)
    if not keywords:
        return False
    return any(keyword in text for keyword in keywords)


# ── 場次解析 (/activity/game/) ────────────────────────────────

class _GameParser(HTMLParser):
    """解析場次頁面，提取 button[data-href] 和場次文字"""

    def __init__(self):
        super().__init__()
        self.games: list[dict] = []
        self._in_row = False
        self._is_data_row = False
        self._current_row_text = ""
        self._current_href = ""
        self._row_has_button = False

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if tag == "tr":
            self._in_row = True
            self._is_data_row = False
            self._current_row_text = ""
            self._current_href = ""
            self._row_has_button = False
        elif tag == "td" and self._in_row:
            self._is_data_row = True
        elif tag == "button" and "data-href" in attr:
            self._current_href = attr["data-href"]
            self._row_has_button = True

    def handle_endtag(self, tag):
        if tag == "tr" and self._in_row:
            if self._is_data_row:
                text = re.sub(r"\s+", " ", self._current_row_text).strip()[:60]
                if self._row_has_button and self._current_href:
                    self.games.append({"text": text, "href": self._current_href, "available": True})
                elif text:
                    self.games.append({"text": text, "href": "", "available": False})
            self._in_row = False

    def handle_data(self, data):
        if self._in_row:
            self._current_row_text += data


def parse_game_list(html: str) -> dict:
    """解析場次頁 HTML，回傳 {available: [...], sold_out: [...], total: N}"""
    parser = _GameParser()
    parser.feed(html)
    available = [g for g in parser.games if g["available"]]
    sold_out = [g["text"] for g in parser.games if not g["available"]]
    return {"available": available, "sold_out": sold_out, "total": len(parser.games)}


# ── 驗證頁解析 (/activity/verify/, /ticket/verify/) ──────────

def parse_verify_page(html: str) -> dict:
    """解析驗證頁 HTML，回傳 {answer: str|None, csrf: str|None, form_action: str|None}"""
    result: dict = {"answer": None, "csrf": None, "form_action": None}

    # 提取 CSRF token
    csrf_match = re.search(r'name=["\']_csrf["\']\s+value=["\']([^"\']+)', html)
    if csrf_match:
        result["csrf"] = csrf_match.group(1)

    # 提取 form action
    action_match = re.search(r'id=["\']form-ticket-verify["\'].*?action=["\']([^"\']+)', html, re.DOTALL)
    if not action_match:
        action_match = re.search(r'action=["\']([^"\']*verify[^"\']*)', html)
    if action_match:
        result["form_action"] = action_match.group(1)

    # 提取【】間的答案
    # 先嘗試 zone-verify 區域
    zone_match = re.search(r'class=["\']zone-verify["\'][^>]*>(.{0,500})', html, re.DOTALL)
    if zone_match:
        zone_text = zone_match.group(1)
        zone_text = zone_text.replace("「", "【").replace("」", "】")
        answer_match = re.search(r"【(.+?)】", zone_text)
        if answer_match:
            result["answer"] = answer_match.group(1)

    return result


# ── 區域解析 (/ticket/area/) ─────────────────────────────────

_SOLD_OUT_PATTERN = re.compile(
    r"選購一空|已售完|sold out|no tickets|空席なし|完売|暫無", re.IGNORECASE
)


class _AreaParser(HTMLParser):
    """解析區域頁面，提取可用區域連結"""

    def __init__(self):
        super().__init__()
        self.areas: list[dict] = []
        self._in_zone = False
        self._zone_depth = 0
        self._in_a = False
        self._a_text = ""
        self._a_href = ""
        self._a_id = ""
        self._a_disabled = False

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        cls = attr.get("class", "")
        if tag == "div":
            if "zone" in cls.split() and not self._in_zone:
                self._in_zone = True
                self._zone_depth = 1
            elif self._in_zone:
                self._zone_depth += 1
        elif tag == "a" and self._in_zone:
            self._in_a = True
            self._a_text = ""
            self._a_href = attr.get("href", "")
            self._a_id = attr.get("id", "")
            self._a_disabled = "disabled" in cls

    def handle_endtag(self, tag):
        if tag == "div" and self._in_zone:
            self._zone_depth -= 1
            if self._zone_depth == 0:
                self._in_zone = False
        elif tag == "a" and self._in_a:
            self._in_a = False
            text = re.sub(r"\s+", " ", self._a_text).strip()[:50]
            # 只有當 a 標籤有 id 或是 href 時，我們才認為它是區域按鈕
            if self._a_id or self._a_href:
                self.areas.append({
                    "text": text,
                    "href": self._a_href,
                    "id": self._a_id,
                    "disabled": self._a_disabled
                })

    def handle_data(self, data):
        if self._in_a:
            self._a_text += data


def parse_area_list(html: str) -> dict:
    """解析區域頁 HTML，回傳 {available: [...], sold_out: [...], total: N}"""
    import json
    area_urls = {}
    
    # 解析隱藏在 Script 標籤裡的 areaUrlList
    m = re.search(r'areaUrlList\s*=\s*(\{.*?\});', html, re.DOTALL)
    if m:
        try:
            # 替換單引號為雙引號以符合 JSON 格式
            js_obj = m.group(1).replace("'", '"')
            area_urls = json.loads(js_obj)
        except Exception:
            pass

    parser = _AreaParser()
    parser.feed(html)
    
    available = []
    sold_out = []
    
    for a in parser.areas:
        href = a["href"]
        if not href and a["id"] in area_urls:
            href = area_urls[a["id"]]
            
        is_sold = _SOLD_OUT_PATTERN.search(a["text"]) or a["disabled"] or not href
        a["href"] = href
        a["available"] = not is_sold
        
        if a["available"]:
            available.append(a)
        else:
            sold_out.append(a["text"])

    return {"available": available, "sold_out": sold_out, "total": len(parser.areas)}


# ── 訂票表單解析 (/ticket/ticket/) ───────────────────────────

def parse_ticket_form(html: str) -> dict:
    """解析訂票頁 HTML，提取隱藏欄位、票數下拉選單及同意勾選框"""
    result: dict = {"fields": {}, "select_name": None}
    
    # 1. 提取所有隱藏欄位 (含 CSRF)
    for m in re.finditer(r'<input[^>]+type=["\']hidden["\'][^>]*>', html):
        tag = m.group(0)
        name_m = re.search(r'name=["\']([^"\']+)', tag)
        value_m = re.search(r'value=["\']([^"\']*)', tag)
        if name_m:
            result["fields"][name_m.group(1)] = value_m.group(1) if value_m else ""
            
    # 2. 提取票數下拉選單的 name (通常是 TicketForm[ticketCount][...])
    # 優先找 mobile-select 類別
    select_match = re.search(r'<select[^>]+name=["\']([^"\']+)["\'][^>]*class=["\'][^"\']*mobile-select', html)
    if not select_match:
        select_match = re.search(r'<select[^>]+name=["\']([^"\']+)["\']', html)
    if select_match:
        result["select_name"] = select_match.group(1)
        
    # 3. 強制加入同意條款 (如果是 hidden 或 checkbox)
    # 拓元通常是 TicketForm[agree]
    if 'TicketForm[agree]' not in result["fields"]:
        result["fields"]['TicketForm[agree]'] = '1'
        
    return result


# ── 頁面狀態偵測 ────────────────────────────────────────────

def detect_coming_soon(html: str) -> bool:
    """偵測「即將開賣」頁面"""
    return bool(re.search(
        r"coming soon|即將開賣|尚未開賣|即将开卖|まもなく販売開始",
        html, re.IGNORECASE
    ))


def detect_login_required(html: str, url: str = "") -> bool:
    """偵測是否需要登入"""
    if "login" in url or "facebook.com" in url or "accounts.google.com" in url:
        return True
    return bool(re.search(r'login|sign.?in|登入', html, re.IGNORECASE) and
                re.search(r'<form[^>]*login', html, re.IGNORECASE))
