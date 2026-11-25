#!/usr/bin/env python3
"""KOL 红人邮件系统 - 跨平台 Web 版。

只用 Python 标准库，不需要 pip 安装任何东西。
Windows / macOS / Linux 都能直接跑，也可以直接部署到云端拿到一个公开网址。

运行：
    python server/app.py
然后浏览器打开 http://localhost:8787

部署时读取环境变量：
    PORT      监听端口（云平台一般会自动注入）
    KOL_HOST  监听地址，默认 0.0.0.0
"""

from __future__ import annotations

import base64
import binascii
import csv
import io
import json
import os
import re
import smtplib
import ssl
import sys
import threading
import time
import uuid
import zipfile
from datetime import datetime
from email.charset import QP, Charset
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = ROOT / "web"
DATA_ROOT = ROOT / "data"
STATE_PATH = DATA_ROOT / "state.json"

HOST = os.environ.get("KOL_HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8787") or "8787")

DEFAULT_TEMPLATE = {
    "subject": "合作邀约 - {{name}}",
    "body": "{{name}} 您好，\r\n\r\n我们想和您聊一下产品合作，形式可以是置换或付费合作。\r\n"
            "\r\n期待您的回复。\r\n\r\n祝好，\r\n{{fromName}}",
}

DEFAULT_SETTINGS = {
    "fromName": "",
    "fromEmail": "",
    "replyTo": "",
    "smtpHost": "",
    "smtpPort": 587,
    "username": "",
    "password": "",
    "useSsl": True,
    "dailyLimit": 40,
    "delaySeconds": 2,
}

_NAME_KEYS = ["name", "姓名", "红人名称", "红人姓名", "creator_name", "influencer_name", "nickname",
              "达人", "达人名称", "博主", "博主名称", "KOL", "KOL名称", "influencer", "creator"]
_EMAIL_KEYS = ["email", "邮箱", "mail", "e-mail", "邮件", "电子邮箱", "联系邮箱", "红人邮箱",
               "达人邮箱", "博主邮箱", "Email Address"]
_PROFILE_KEYS = ["profileUrl", "profile_url", "主页链接", "主页", "主页地址", "个人主页", "账号主页",
                 "url", "link", "profile", "profile link", "homepage"]
_PLATFORM_KEYS = ["platform", "平台", "site", "channel", "渠道", "来源", "社媒平台"]
_NOTE_KEYS = ["note", "备注", "comment", "notes", "说明", "补充"]
_CREATOR_TYPE_KEYS = ["creatorType", "creator_type", "博主类型", "红人类型", "达人类型",
                      "博主分类", "红人分类", "分类", "类型", "category"]

_EMAIL_PATTERN = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.IGNORECASE)
_URL_PATTERN = re.compile(r"https?://[^\s,;，；]+", re.IGNORECASE)

_STATE_LOCK = threading.RLock()
_STATE: dict | None = None


# --------------------------------------------------------------------------
# 状态读写
# --------------------------------------------------------------------------

def default_state() -> dict:
    return {
        "template": dict(DEFAULT_TEMPLATE),
        "templates": [],
        "settings": dict(DEFAULT_SETTINGS),
        "leads": [],
        "logs": [],
    }


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, dict):
        return []
    if isinstance(value, list):
        return value
    return [value]


def load_state_from(path: Path) -> dict | None:
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError:
        return None
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def load_state() -> dict:
    state = default_state()
    loaded = load_state_from(STATE_PATH) if STATE_PATH.exists() else None
    if isinstance(loaded, dict):
        state["settings"] = dict(DEFAULT_SETTINGS)
        saved_settings = loaded.get("settings")
        if isinstance(saved_settings, dict):
            for key, value in saved_settings.items():
                if key in state["settings"] and value is not None:
                    state["settings"][key] = value
        saved_template = loaded.get("template")
        if isinstance(saved_template, dict):
            state["template"] = {**DEFAULT_TEMPLATE, **saved_template}
        state["templates"] = _as_list(loaded.get("templates"))
        state["logs"] = _as_list(loaded.get("logs"))
    # 红人名单每次启动重新导入，模板和发件设置则保留。
    state["leads"] = []

    if not state["templates"]:
        best = best_backup_templates()
        if best:
            state["templates"] = best
    return state


def best_backup_templates() -> list:
    """当前 state.json 没有模板时，从 data 目录里的其它 state*.json 备份里捞一份回来。"""
    if not DATA_ROOT.exists():
        return []
    best: list = []
    for path in sorted(DATA_ROOT.glob("state*.json")):
        if path.resolve() == STATE_PATH.resolve():
            continue
        data = load_state_from(path)
        if not isinstance(data, dict):
            continue
        templates = _as_list(data.get("templates"))
        if len(templates) > len(best):
            best = templates
    return best


def save_state(state: dict) -> None:
    """写盘。发件人账号信息不落盘，避免授权码被写进文件。"""
    safe = json.loads(json.dumps(state, ensure_ascii=False, default=str))
    safe["template"] = dict(safe.get("template") or DEFAULT_TEMPLATE)
    safe["leads"] = []
    for name in ("templates", "leads", "logs"):
        safe[name] = _as_list(safe.get(name))
    settings = safe.get("settings")
    if isinstance(settings, dict):
        for field in ("fromName", "fromEmail", "replyTo", "username", "password"):
            if field in settings:
                settings[field] = ""
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(safe, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(STATE_PATH)


def get_state() -> dict:
    global _STATE
    if _STATE is None:
        _STATE = load_state()
    return _STATE


# --------------------------------------------------------------------------
# 通用小工具
# --------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def text_of(value) -> str:
    if value is None:
        return ""
    return str(value)


def find_ci(mapping: dict, names: list[str]) -> str:
    lowered = {str(key).strip().lower(): value for key, value in mapping.items()}
    for name in names:
        if name.lower() in lowered:
            return text_of(lowered[name.lower()]).strip()
    return ""


def first_field_matching(row: dict, pattern: re.Pattern) -> str:
    for value in row.values():
        candidate = text_of(value).strip()
        if not candidate:
            continue
        match = pattern.search(candidate)
        if match:
            return match.group(0).strip()
    return ""


_SKIP_NAME_FIELDS = {
    "国家", "country", "地区", "市场", "market", "语种", "语言", "language", "locale",
    "平台", "platform", "site", "channel", "渠道", "站点",
    "博主类型", "红人类型", "达人类型", "博主分类", "红人分类", "分类", "类型",
    "creatorType", "creator_type", "category",
    "note", "备注", "comment", "notes", "说明", "补充", "感受",
}


def first_non_empty_field(row: dict) -> str:
    for name, value in row.items():
        if text_of(name).strip() in _SKIP_NAME_FIELDS:
            continue
        candidate = text_of(value).strip()
        if not candidate or "@" in candidate or candidate.lower().startswith("http"):
            continue
        return candidate
    return ""


_METADATA_NAMES = {
    "日本", "美国", "英国", "德国", "法国", "意大利", "加拿大", "澳大利亚", "澳洲",
    "japan", "us", "usa", "united states", "america", "uk", "united kingdom", "britain",
    "germany", "france", "italy", "canada", "australia",
    "育儿博主", "科技测评博主", "车载/自驾类博主", "复古电子产品类博主", "介护博主", "退休生活分享博主",
}


def probably_not_a_name(name: str) -> bool:
    candidate = text_of(name).strip()
    if not candidate:
        return True
    if re.fullmatch(r"\d+", candidate):
        return True
    return candidate.lower() in _METADATA_NAMES


def lead_looks_importable(lead: dict) -> bool:
    if text_of(lead.get("email")).strip() or text_of(lead.get("profileUrl")).strip():
        return True
    return not probably_not_a_name(lead.get("name"))


def suspicious_note(value: str) -> bool:
    return bool(re.fullmatch(r"\d{1,3}", text_of(value).strip()))


def suspicious_creator_type(value: str) -> bool:
    return bool(re.fullmatch(r"\d+", text_of(value).strip()))


def row_fields(row: dict) -> dict:
    fields = {}
    for name, value in row.items():
        clean = text_of(name).strip()
        if not clean:
            continue
        fields[clean] = text_of(value)
    return fields


def new_lead(row: dict, source_file: str = "") -> dict:
    name = find_ci(row, _NAME_KEYS)
    email = find_ci(row, _EMAIL_KEYS)
    profile_url = find_ci(row, _PROFILE_KEYS)
    platform = find_ci(row, _PLATFORM_KEYS)
    note = find_ci(row, _NOTE_KEYS)
    fields = row_fields(row)

    if suspicious_note(note):
        note = ""
        for key in _NOTE_KEYS:
            if key in fields and suspicious_note(fields[key]):
                fields[key] = ""
    if not email:
        email = first_field_matching(row, _EMAIL_PATTERN)
    for key in _CREATOR_TYPE_KEYS:
        if key in fields and suspicious_creator_type(fields[key]):
            fields[key] = ""
    if not profile_url:
        profile_url = first_field_matching(row, _URL_PATTERN)
    if not name:
        name = first_non_empty_field(row)

    return {
        "id": uuid.uuid4().hex,
        "name": name,
        "email": email,
        "profileUrl": profile_url,
        "platform": platform,
        "note": note,
        "fields": fields,
        "sourceFile": source_file,
        "status": "new",
        "importedAt": now_iso(),
        "sentAt": "",
        "lastError": "",
    }


def lead_key(lead: dict) -> str:
    for field, prefix in (("email", "email:"), ("profileUrl", "url:"), ("name", "name:")):
        value = text_of(lead.get(field)).strip()
        if value:
            return prefix + value.lower()
    return "id:" + text_of(lead.get("id"))


def update_or_add_lead(state: dict, lead: dict) -> str:
    incoming = lead_key(lead)
    for existing in state["leads"]:
        if lead_key(existing) != incoming:
            continue
        for field in ("name", "email", "profileUrl", "platform", "note", "sourceFile"):
            if text_of(lead.get(field)).strip():
                existing[field] = lead[field]
        if not isinstance(existing.get("fields"), dict):
            existing["fields"] = {}
        for key, value in (lead.get("fields") or {}).items():
            existing["fields"][key] = text_of(value)
        if not text_of(existing.get("status")).strip():
            existing["status"] = "new"
        return "updated"
    state["leads"].append(lead)
    return "added"


# --------------------------------------------------------------------------
# 导入：CSV / TSV / XLSX
# --------------------------------------------------------------------------

def decode_bytes(data: bytes) -> str:
    if data[:3] == b"\xef\xbb\xbf":
        return data[3:].decode("utf-8", errors="replace")
    if data[:2] == b"\xff\xfe":
        return data[2:].decode("utf-16-le", errors="replace")
    if data[:2] == b"\xfe\xff":
        return data[2:].decode("utf-16-be", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    for encoding in ("gb18030", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def detect_delimiter(text: str) -> str:
    first_line = text.split("\n", 1)[0]
    best = "\t"
    best_score = first_line.count("\t")
    for candidate in (";", ","):
        score = first_line.count(candidate)
        if score > best_score:
            best_score = score
            best = candidate
    return best


def import_rows(state: dict, rows: list[dict], source_file: str) -> dict:
    added = 0
    updated = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        if not any(text_of(value).strip() for value in row.values()):
            continue
        lead = new_lead(row, source_file)
        if not lead_looks_importable(lead):
            continue
        if update_or_add_lead(state, lead) == "added":
            added += 1
        else:
            updated += 1
    return {"added": added, "updated": updated, "total": len(rows)}


def import_csv_text(state: dict, text: str, source_file: str) -> dict:
    if not text.strip():
        return {"added": 0, "updated": 0, "total": 0}
    delimiter = detect_delimiter(text)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    try:
        raw_rows = [row for row in reader]
    except csv.Error as exc:
        raise ValueError("表格文字读取失败。请确认第一行是表头，比如：姓名,邮箱,主页链接,平台,备注") from exc
    if not raw_rows:
        return {"added": 0, "updated": 0, "total": 0}
    headers = [text_of(cell).strip() for cell in raw_rows[0]]
    rows = []
    for raw in raw_rows[1:]:
        row = {}
        for index, header in enumerate(headers):
            if not header:
                continue
            row[header] = raw[index] if index < len(raw) else ""
        rows.append(row)
    return import_rows(state, rows, source_file)


_MAIN_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def _sheet_path(zip_file: zipfile.ZipFile, names: set[str]) -> str:
    try:
        workbook = ElementTree.fromstring(zip_file.read("xl/workbook.xml"))
        sheets = workbook.find(f"{_MAIN_NS}sheets")
        target = None
        if sheets is not None:
            first = sheets.find(f"{_MAIN_NS}sheet")
            if first is not None:
                target = first.get(f"{_REL_NS}id")
        if target and "xl/_rels/workbook.xml.rels" in names:
            rels = ElementTree.fromstring(zip_file.read("xl/_rels/workbook.xml.rels"))
            for rel in rels:
                if rel.get("Id") == target:
                    path = rel.get("Target") or ""
                    if path.startswith("/"):
                        return path.lstrip("/")
                    return "xl/" + path.lstrip("./")
    except (ElementTree.ParseError, KeyError):
        pass
    return "xl/worksheets/sheet1.xml"


def _shared_strings(zip_file: zipfile.ZipFile, names: set[str]) -> list[str]:
    if "xl/sharedStrings.xml" not in names:
        return []
    root = ElementTree.fromstring(zip_file.read("xl/sharedStrings.xml"))
    values = []
    for si in root.findall(f"{_MAIN_NS}si"):
        values.append("".join(node.text or "" for node in si.iter(f"{_MAIN_NS}t")))
    return values


def _column_index(cell_ref: str) -> int:
    letters = re.match(r"[A-Za-z]+", cell_ref or "")
    if not letters:
        return -1
    index = 0
    for char in letters.group(0).upper():
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def _cell_text(cell: ElementTree.Element, shared: list[str]) -> str:
    cell_type = cell.get("t") or ""
    if cell_type == "inlineStr":
        node = cell.find(f"{_MAIN_NS}is")
        if node is None:
            return ""
        return "".join(part.text or "" for part in node.iter(f"{_MAIN_NS}t"))
    value_node = cell.find(f"{_MAIN_NS}v")
    if value_node is None or value_node.text is None:
        return ""
    raw = value_node.text
    if cell_type == "s":
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return ""
    if raw.endswith(".0") and raw[:-2].isdigit():
        return raw[:-2]
    return raw


def xlsx_rows(data: bytes) -> list[dict]:
    with zipfile.ZipFile(io.BytesIO(data)) as zip_file:
        names = set(zip_file.namelist())
        shared = _shared_strings(zip_file, names)
        root = ElementTree.fromstring(zip_file.read(_sheet_path(zip_file, names)))

    grid: list[dict[int, str]] = []
    for row_node in root.iter(f"{_MAIN_NS}row"):
        row: dict[int, str] = {}
        for cell in row_node.findall(f"{_MAIN_NS}c"):
            index = _column_index(cell.get("r") or "")
            if index < 0:
                index = len(row)
            row[index] = _cell_text(cell, shared)
        grid.append(row)

    while grid and not any(text_of(value).strip() for value in grid[0].values()):
        grid.pop(0)
    if not grid:
        return []

    headers = {}
    for index, value in grid[0].items():
        name = text_of(value).strip()
        if name:
            headers[index] = name

    rows = []
    for raw in grid[1:]:
        row = {name: raw.get(index, "") for index, name in headers.items()}
        rows.append(row)
    return rows


def import_lead_file(state: dict, filename: str, content: bytes, replace: bool) -> dict:
    if replace:
        state["leads"] = []
    ext = Path(filename).suffix.lower()
    if ext in (".csv", ".tsv"):
        return import_csv_text(state, decode_bytes(content), filename)
    if ext in (".xlsx", ".xlsm"):
        try:
            return import_rows(state, xlsx_rows(content), filename)
        except (zipfile.BadZipFile, ElementTree.ParseError, KeyError, OSError) as exc:
            raise ValueError("这个 Excel 文件结构特殊，无法自动读取。请另存为 CSV 后重新导入。") from exc
    if ext == ".xls":
        raise ValueError("请把文件另存为 .xlsx 或 .csv 再导入。")
    raise ValueError(f"暂不支持这个文件类型：{ext}。请使用 CSV、TSV、XLSX 或 XLSM。")


# --------------------------------------------------------------------------
# 模板匹配与渲染
# --------------------------------------------------------------------------

def any_value(obj, name: str):
    if not isinstance(obj, dict) or not name:
        return None
    for key, value in obj.items():
        if text_of(key).lower() == name.lower():
            return value
    return None


def lead_context(state: dict, lead: dict) -> dict:
    name = text_of(any_value(lead, "name")).strip()
    email = text_of(any_value(lead, "email")).strip()
    profile_url = text_of(any_value(lead, "profileUrl")).strip()
    platform = text_of(any_value(lead, "platform")).strip()
    note = text_of(any_value(lead, "note")).strip()
    settings = state["settings"]
    today = datetime.now().strftime("%Y-%m-%d")
    from_name = text_of(settings.get("fromName")).strip()
    from_email = text_of(settings.get("fromEmail")).strip()

    context = {
        "name": name, "姓名": name, "红人名称": name, "红人姓名": name,
        "email": email, "邮箱": email,
        "profileUrl": profile_url, "profile_url": profile_url, "主页链接": profile_url,
        "platform": platform, "平台": platform,
        "note": note, "备注": note,
        "fromName": from_name, "发件人名称": from_name,
        "fromEmail": from_email, "发件邮箱": from_email,
        "today": today, "今天": today,
    }
    lead_fields = any_value(lead, "fields")
    if isinstance(lead_fields, dict):
        for field_name, value in lead_fields.items():
            if text_of(field_name).strip():
                context[text_of(field_name)] = text_of(value).strip()
    for field_name, value in lead.items():
        if field_name not in context:
            context[field_name] = text_of(value).strip()
    return context


def resolve_template_text(text: str, context: dict) -> str:
    result = text_of(text)
    for key in sorted(context.keys(), key=len, reverse=True):
        value = text_of(context[key])
        escaped = re.escape(str(key))
        result = result.replace("{{" + str(key) + "}}", value)
        result = result.replace("{" + str(key) + "}", value)
        result = re.sub(r"\{\{\s*" + escaped + r"\s*\}\}", lambda _m, v=value: v, result, flags=re.IGNORECASE)
        result = re.sub(r"\{\s*" + escaped + r"\s*\}", lambda _m, v=value: v, result, flags=re.IGNORECASE)
    return result


def unresolved_tokens(text: str) -> list[str]:
    scan = text_of(text)
    tokens: list[str] = []
    double = re.compile(r"\{\{\s*([^{}\r\n]+?)\s*\}\}")
    for match in double.finditer(scan):
        token = match.group(1).strip()
        if token:
            tokens.append(token)
    masked = double.sub(lambda m: " " * len(m.group(0)), scan)
    single = re.compile(r"(?<!\{)\{\s*([^{}\r\n]+?)\s*\}(?!\})")
    for match in single.finditer(masked):
        token = match.group(1).strip()
        if token:
            tokens.append(token)
    return sorted(set(tokens))


def strip_mailto_text(text: str) -> str:
    return re.sub(r"\s*<\s*mailto:[^>\r\n]+>", "", text_of(text), flags=re.IGNORECASE)


def strip_mailto_html(html: str) -> str:
    return re.sub(r"\s*(?:&lt;|<)\s*mailto:[^>&\r\n]+(?:&gt;|>)", "", text_of(html), flags=re.IGNORECASE)


def bold_markers_plain(text: str) -> str:
    result = re.sub(r"\*\*(.+?)\*\*", r"\1", text_of(text), flags=re.DOTALL)
    return re.sub(r"【粗】(.+?)【/粗】", r"\1", result, flags=re.DOTALL)


def bold_markers_html(html: str) -> str:
    result = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text_of(html), flags=re.DOTALL)
    return re.sub(r"【粗】(.+?)【/粗】", r"<strong>\1</strong>", result, flags=re.DOTALL)


def normalize_honorific(text: str) -> str:
    return re.sub(r"[\s\u3000]+(?=(様|さま|さん|先生|殿))", "", text_of(text))


def normalize_honorific_html(html: str) -> str:
    return re.sub(r"(?:\s|&nbsp;|&#160;|&#xA0;)+(?=(様|さま|さん|先生|殿))", "",
                  text_of(html), flags=re.IGNORECASE)


def plain_text_to_html(text: str) -> str:
    from html import escape
    encoded = escape(text_of(text))
    encoded = encoded.replace("\r\n", "\n").replace("\r", "\n")
    return ('<div style="font-family:Arial,\'Microsoft YaHei\',sans-serif;font-size:14px;line-height:1.6;">'
            + encoded.replace("\n", "<br>") + "</div>")


_COUNTRY_ALIASES = {
    "us": "美国", "usa": "美国", "america": "美国", "unitedstates": "美国",
    "unitedstatesofamerica": "美国",
    "uk": "英国", "britain": "英国", "england": "英国", "unitedkingdom": "英国",
    "japan": "日本", "jp": "日本",
    "germany": "德国", "de": "德国",
    "france": "法国", "fr": "法国",
    "italy": "意大利", "it": "意大利",
}


def condition_variants(text: str) -> list[str]:
    raw = text_of(text).strip()
    if not raw:
        return []
    normalized = re.sub(r"[\s\u3000,;，；、/|\\_\-]+", "", raw.lower())
    variants = [normalized]
    if normalized in _COUNTRY_ALIASES:
        variants.append(_COUNTRY_ALIASES[normalized])
    creator = re.sub(r"(红人|达人|博主|kol|creator|influencer)$", "", normalized)
    creator = re.sub(r"(类别|类型|分类|类)$", "", creator)
    if creator:
        variants.append(creator)
    seen = []
    for item in variants:
        if item and item not in seen:
            seen.append(item)
    return seen


def test_template_condition(expected: str, actual: str) -> bool:
    expected_text = text_of(expected).strip()
    if not expected_text:
        return True
    actual_text = text_of(actual).strip()
    if not actual_text:
        return False
    parts = [expected_text] + [piece.strip() for piece in re.split(r"[,;，；/|、]", expected_text) if piece.strip()]
    actual_variants = condition_variants(actual_text)
    actual_lower = actual_text.lower()
    for part in parts:
        part_lower = part.lower()
        if actual_lower == part_lower or part_lower in actual_lower or actual_lower in part_lower:
            return True
        for expected_variant in condition_variants(part):
            for actual_variant in actual_variants:
                if expected_variant == actual_variant:
                    return True
                if len(expected_variant) >= 2 and expected_variant in actual_variant:
                    return True
                if len(actual_variant) >= 2 and actual_variant in expected_variant:
                    return True
    return False


def context_first_value(context: dict, names: list[str]) -> str:
    for name in names:
        for key, value in context.items():
            if text_of(key).lower() == name.lower():
                candidate = text_of(value).strip()
                if candidate:
                    return candidate
    return ""


def template_condition_score(template: dict, context: dict) -> int:
    country = context_first_value(context, ["country", "国家", "地区", "市场", "market"])
    language = context_first_value(context, ["language", "lang", "语种", "语言"])
    platform = context_first_value(context, ["platform", "平台", "site", "站点", "渠道", "来源", "社媒平台"])
    creator_type = context_first_value(context, ["creatorType", "creator_type", "博主类型", "红人类型",
                                                 "达人类型", "博主分类", "红人分类", "分类", "类型", "category"])
    rules = [
        {"expected": template.get("country"), "actual": country, "weight": 3, "required": False},
        {"expected": template.get("creatorType"), "actual": creator_type, "weight": 5, "required": True},
        {"expected": template.get("language"), "actual": language, "weight": 2, "required": False},
        {"expected": template.get("platform"), "actual": platform, "weight": 1, "required": False},
    ]
    score = 0
    matched = False
    for rule in rules:
        expected = text_of(rule["expected"]).strip()
        actual = text_of(rule["actual"]).strip()
        if not expected:
            continue
        if not actual:
            if rule["required"]:
                return -1
            continue
        if not test_template_condition(expected, actual):
            return -1
        score += rule["weight"]
        matched = True
    return score if matched else -1


def template_display_name(template: dict) -> str:
    name = text_of(template.get("name")).strip() or "未命名模板"
    conditions = []
    for field, label in (("country", "国家"), ("creatorType", "博主类型"),
                         ("language", "语种"), ("platform", "平台")):
        value = text_of(template.get(field)).strip()
        if value:
            conditions.append(f"{label}={value}")
    if conditions:
        return f"{name}（{'，'.join(conditions)}）"
    return name


def matching_template(state: dict, lead: dict) -> dict:
    context = lead_context(state, lead)
    best_template = None
    best_score = -1
    for template in state["templates"]:
        if not isinstance(template, dict):
            continue
        score = template_condition_score(template, context)
        if score > best_score:
            best_score = score
            best_template = template
    if best_template is not None:
        return {"template": best_template, "name": template_display_name(best_template),
                "id": best_template.get("id"), "source": "library"}
    return {"template": state["template"], "name": "默认模板", "id": "__default", "source": "default"}


def render_lead_message(state: dict, lead: dict) -> dict:
    match = matching_template(state, lead)
    template = match["template"]
    subject = text_of(template.get("subject"))
    body = strip_mailto_text(text_of(template.get("body")))
    body_display = bold_markers_plain(body)
    saved_html = template.get("bodyHtml")
    if saved_html:
        body_html = strip_mailto_html(text_of(saved_html))
    else:
        body_html = plain_text_to_html(body)
    body_html = bold_markers_html(body_html)

    context = lead_context(state, lead)
    rendered_subject = normalize_honorific(resolve_template_text(subject, context))
    rendered_body = normalize_honorific(resolve_template_text(body_display, context))
    rendered_html = normalize_honorific_html(resolve_template_text(body_html, context))
    unresolved = sorted(set(unresolved_tokens(rendered_subject) + unresolved_tokens(rendered_body)
                            + unresolved_tokens(rendered_html)))
    return {
        "subject": rendered_subject,
        "body": rendered_body,
        "bodyHtml": rendered_html,
        "unresolved": unresolved,
        "templateId": match["id"],
        "templateName": match["name"],
        "templateSource": match["source"],
    }


def lead_template_summary(state: dict, lead: dict) -> dict:
    match = matching_template(state, lead)
    return {"templateName": match["name"], "templateId": match["id"], "templateSource": match["source"]}


def format_lead_extra_fields(lead: dict) -> str:
    lead_fields = any_value(lead, "fields")
    if not isinstance(lead_fields, dict):
        return ""
    hidden = set(_NAME_KEYS + _EMAIL_KEYS + _PROFILE_KEYS + _PLATFORM_KEYS + _NOTE_KEYS)
    parts = []
    for field_name, value in lead_fields.items():
        name = text_of(field_name).strip()
        if not name or name in hidden:
            continue
        candidate = text_of(value).strip()
        if candidate:
            parts.append(f"{name}: {candidate}")
    return " | ".join(parts)


# --------------------------------------------------------------------------
# 发送邮件
# --------------------------------------------------------------------------

class SendBlocked(Exception):
    pass


def normalized_gmail_password(password: str) -> str:
    return re.sub(r"[\s\-]+", "", text_of(password))


def assert_sender_settings(settings: dict) -> None:
    if not text_of(settings.get("smtpHost")).strip():
        raise SendBlocked("SMTP 服务器不能为空。Gmail 请填 smtp.gmail.com。")
    if not text_of(settings.get("fromEmail")).strip():
        raise SendBlocked("发件邮箱不能为空。")
    if not text_of(settings.get("username")).strip():
        raise SendBlocked("账号不能为空。")
    if not text_of(settings.get("password")).strip():
        raise SendBlocked("授权码/密码不能为空。Gmail 请填 16 位应用专用密码。")
    if text_of(settings.get("smtpHost")).strip().lower() == "smtp.gmail.com":
        if int(settings.get("smtpPort") or 587) != 587:
            raise SendBlocked("Gmail 的 SMTP 端口请填 587。")
        if not settings.get("useSsl"):
            raise SendBlocked("Gmail 发信必须勾选 SSL 加密。")
        if text_of(settings.get("fromEmail")).strip().lower() != text_of(settings.get("username")).strip().lower():
            raise SendBlocked("Gmail 发件邮箱和账号请填同一个 Gmail 地址。")
        clean = normalized_gmail_password(settings.get("password"))
        if not re.fullmatch(r"[A-Za-z0-9]+", clean or ""):
            raise SendBlocked("Gmail 授权码里出现了不是英文/数字的字符，请只粘贴 16 位应用专用密码。")
        if len(clean) != 16:
            raise SendBlocked(f"Gmail 授权码应该是 16 位应用专用密码，当前去掉空格/横杠后是 {len(clean)} 位。")


def email_error(email: str) -> str:
    if not text_of(email).strip():
        return "邮箱为空。"
    if re.search(r"[,;，；\s]+", email):
        return "这一行邮箱里像是写了多个地址。为了防止红人互相看到，系统要求每一行只能写一个邮箱。"
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email.strip()):
        return "邮箱格式不正确。"
    return ""


def friendly_smtp_error(exc: Exception) -> str:
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "账号或授权码不正确。Gmail 需要 16 位应用专用密码，不是登录密码。"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return f"收件人邮箱被服务器拒绝：{exc}"
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return f"连不上 SMTP 服务器：{exc}"
    if isinstance(exc, smtplib.SMTPException):
        return f"发送邮件失败：{exc}"
    return f"发送邮件失败：{exc}"


def is_connection_error(exc: Exception) -> bool:
    return isinstance(exc, (TimeoutError, ConnectionError, OSError, smtplib.SMTPServerDisconnected))


def base64_encoded_html(text: str) -> bool:
    compact = re.sub(r"\s+", "", text_of(text))
    if len(compact) < 80 or len(compact) % 4 != 0:
        return False
    if not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", compact):
        return False
    try:
        decoded = base64.b64decode(compact, validate=True).decode("utf-8", errors="replace").lstrip()
    except (binascii.Error, ValueError):
        return False
    return bool(re.match(r"^(<div|<html|<!doctype|<p|<span|<br|<strong)\b", decoded, re.IGNORECASE))


def mail_render_block_reason(render: dict) -> str:
    subject = text_of(render.get("subject"))
    if not subject.strip():
        return "邮件标题为空。为了避免发出去变成 no subject，系统已拦截这一封，请先检查匹配到的模板标题。"
    if re.search(r"[\r\n]", subject):
        return "邮件标题里包含换行，可能导致邮件格式损坏。系统已拦截这一封，请把标题改成一行。"
    if not text_of(render.get("bodyHtml")).strip():
        return "邮件正文为空。系统已拦截这一封，请先检查匹配到的模板正文。"
    if base64_encoded_html(text_of(render.get("bodyHtml"))):
        return "邮件正文疑似已经变成编码文字。系统已拦截这一封，请重新保存对应模板后再发送。"
    return ""


def build_message(settings: dict, lead: dict, render: dict) -> MIMEMultipart:
    message = MIMEMultipart("alternative")
    message["From"] = formataddr((text_of(settings.get("fromName")), text_of(settings.get("fromEmail"))))
    message["To"] = lead["email"].strip()
    message["Subject"] = render["subject"]
    if text_of(settings.get("replyTo")).strip():
        message["Reply-To"] = text_of(settings.get("replyTo")).strip()

    charset = Charset("utf-8")
    charset.header_encoding = QP
    charset.body_encoding = QP
    message.attach(MIMEText(render["body"], "plain", charset))
    message.attach(MIMEText(render["bodyHtml"], "html", charset))
    return message


def deliver(settings: dict, lead: dict, render: dict) -> None:
    assert_sender_settings(settings)
    host = text_of(settings.get("smtpHost")).strip()
    port = int(settings.get("smtpPort") or 587)
    username = text_of(settings.get("username")).strip()
    password = text_of(settings.get("password"))
    if host.lower() == "smtp.gmail.com":
        password = normalized_gmail_password(password)
    else:
        password = re.sub(r"\s+", "", password)

    message = build_message(settings, lead, render)
    with smtplib.SMTP(host, port, timeout=15) as client:
        client.ehlo()
        if settings.get("useSsl"):
            client.starttls(context=ssl.create_default_context())
            client.ehlo()
        client.login(username, password)
        client.send_message(message)


def add_log(state: dict, lead: dict, action: str, status: str, message: str = "") -> None:
    state["logs"].append({
        "at": now_iso(),
        "leadId": lead.get("id", ""),
        "email": lead.get("email", ""),
        "name": lead.get("name", ""),
        "action": action,
        "status": status,
        "message": message,
    })
    if len(state["logs"]) > 500:
        state["logs"] = state["logs"][-500:]


def today_send_count(state: dict) -> int:
    today = datetime.now().date()
    count = 0
    for entry in state["logs"]:
        if not isinstance(entry, dict):
            continue
        if entry.get("action") != "send" or entry.get("status") != "sent":
            continue
        try:
            when = datetime.fromisoformat(text_of(entry.get("at"))).date()
        except ValueError:
            continue
        if when == today:
            count += 1
    return count


def send_leads(state: dict, lead_ids: list[str], dry_run: bool) -> list[dict]:
    settings = state["settings"]
    leads = state["leads"]
    if lead_ids:
        wanted = set(lead_ids)
        selected = [lead for lead in leads if lead.get("id") in wanted]
    else:
        selected = [lead for lead in leads if lead.get("status") not in ("sent", "blocked")]

    if not dry_run:
        limit = int(settings.get("dailyLimit") or 0)
        if limit > 0 and (today_send_count(state) + len(selected)) > limit:
            raise SendBlocked(
                f"超过每日发送上限。今天已发送 {today_send_count(state)} 封，本次选择 {len(selected)} 封，上限是 {limit} 封。"
            )

    if not dry_run:
        assert_sender_settings(settings)

    results = []
    passed = [lead for lead in selected if not email_error(lead.get("email", ""))]
    if not dry_run and not passed:
        raise SendBlocked("这批红人里没有可直接发送的有效邮箱。")

    for index, lead in enumerate(selected, start=1):
        problem = email_error(lead.get("email", ""))
        if problem:
            lead["status"] = "blocked"
            lead["lastError"] = problem
            add_log(state, lead, "send", "blocked", problem)
            results.append({"id": lead["id"], "email": lead["email"], "name": lead["name"],
                            "status": "blocked", "message": problem})
            continue

        render = render_lead_message(state, lead)
        if render["unresolved"]:
            message = f"邮件里还有没替换的占位符：{', '.join(render['unresolved'])}"
            lead["status"] = "blocked"
            lead["lastError"] = message
            add_log(state, lead, "send", "blocked", message)
            results.append({"id": lead["id"], "email": lead["email"], "name": lead["name"],
                            "status": "blocked", "message": message})
            continue

        block_reason = mail_render_block_reason(render)
        if block_reason:
            lead["status"] = "blocked"
            lead["lastError"] = block_reason
            add_log(state, lead, "send", "blocked", block_reason)
            results.append({"id": lead["id"], "email": lead["email"], "name": lead["name"],
                            "status": "blocked", "message": block_reason})
            continue

        if dry_run:
            lead["status"] = "previewed"
            add_log(state, lead, "preview", "ok", "检查选中但不发送")
            results.append({"id": lead["id"], "email": lead["email"], "name": lead["name"],
                            "status": "previewed", "subject": render["subject"], "body": render["body"]})
        else:
            sent_ok = False
            last_error: Exception | None = None
            for attempt in range(1, 3):
                try:
                    deliver(settings, lead, render)
                    lead["status"] = "sent"
                    lead["sentAt"] = now_iso()
                    lead["lastError"] = ""
                    note = render["subject"] if attempt == 1 else f"{render['subject']}；自动重连后成功"
                    add_log(state, lead, "send", "sent", note)
                    results.append({"id": lead["id"], "email": lead["email"], "name": lead["name"],
                                    "status": "sent"})
                    sent_ok = True
                    break
                except Exception as exc:  # noqa: BLE001 - 单封失败不影响整批
                    last_error = exc
                    if is_connection_error(exc) and attempt < 2:
                        time.sleep(2)
                        continue
                    break
            if not sent_ok:
                message = friendly_smtp_error(last_error) if last_error else "发送邮件失败。"
                if last_error and is_connection_error(last_error):
                    message += " 系统已自动重连 2 次，这一封仍未成功，但不会中断整批发送。"
                lead["status"] = "failed"
                lead["lastError"] = message
                add_log(state, lead, "send", "failed", message)
                results.append({"id": lead["id"], "email": lead["email"], "name": lead["name"],
                                "status": "failed", "message": message})
            delay = int(settings.get("delaySeconds") or 0)
            if index < len(selected) and delay > 0:
                time.sleep(delay)

    return results


# --------------------------------------------------------------------------
# 接口数据
# --------------------------------------------------------------------------

def state_view(state: dict) -> dict:
    settings = state["settings"]
    leads = []
    for lead in state["leads"]:
        summary = lead_template_summary(state, lead)
        leads.append({
            "id": lead.get("id"),
            "name": lead.get("name"),
            "email": lead.get("email"),
            "profileUrl": lead.get("profileUrl"),
            "platform": lead.get("platform"),
            "note": lead.get("note"),
            "extraFields": format_lead_extra_fields(lead),
            "status": lead.get("status"),
            "sentAt": lead.get("sentAt"),
            "lastError": lead.get("lastError"),
            "sourceFile": lead.get("sourceFile"),
            "unresolved": [],
            "templateName": summary["templateName"],
            "templateId": summary["templateId"],
            "fields": lead.get("fields"),
        })
    return {
        "template": state["template"],
        "templates": state["templates"],
        "settings": {
            "fromName": settings.get("fromName"),
            "fromEmail": settings.get("fromEmail"),
            "replyTo": settings.get("replyTo"),
            "smtpHost": settings.get("smtpHost"),
            "smtpPort": settings.get("smtpPort"),
            "username": settings.get("username"),
            "useSsl": bool(settings.get("useSsl")),
            "dailyLimit": settings.get("dailyLimit"),
            "delaySeconds": settings.get("delaySeconds"),
            "passwordSet": bool(text_of(settings.get("password")).strip()),
        },
        "stats": {
            "total": len(state["leads"]),
            "sent": sum(1 for lead in state["leads"] if lead.get("status") == "sent"),
            "blocked": sum(1 for lead in state["leads"] if lead.get("status") == "blocked"),
            "ready": sum(1 for lead in state["leads"] if lead.get("status") not in ("sent", "blocked")),
            "todaySent": today_send_count(state),
        },
        "leads": leads,
        "logs": state["logs"][-100:],
    }


# --------------------------------------------------------------------------
# HTTP 服务
# --------------------------------------------------------------------------

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "KOLMail/2.0"

    def log_message(self, fmt, *args):  # 保持控制台干净
        return

    # ---------- 响应工具 ----------
    def send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, text: str, status: int = 200, content_type: str = "text/plain; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise ValueError("请求内容不是合法 JSON。") from exc
        return data if isinstance(data, dict) else {}

    # ---------- 静态文件 ----------
    def serve_static(self, path: str) -> None:
        relative = path.lstrip("/") or "index.html"
        target = (WEB_ROOT / relative).resolve()
        try:
            target.relative_to(WEB_ROOT.resolve())
        except ValueError:
            self.send_text("没有找到这个页面。", 404)
            return
        if not target.is_file():
            self.send_text("没有找到这个页面。", 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(target.stat().st_size))
        self.end_headers()
        self.wfile.write(target.read_bytes())

    # ---------- GET ----------
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path.lower() == "/api/state":
            with _STATE_LOCK:
                self.send_json(state_view(get_state()))
            return
        if path.lower().startswith("/api/"):
            self.send_json({"ok": False, "error": f"未知接口：GET {path}"}, 400)
            return
        if path.lower() == "/healthz":
            self.send_text("ok")
            return
        self.serve_static(path)

    # ---------- POST ----------
    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].lower()
        try:
            with _STATE_LOCK:
                state = get_state()
                if path == "/api/template":
                    body = self.read_json()
                    if "subject" in body:
                        state["template"]["subject"] = text_of(body["subject"])
                    if "body" in body:
                        state["template"]["body"] = text_of(body["body"])
                    save_state(state)
                    self.send_json({"ok": True})
                    return

                if path == "/api/templates/save":
                    body = self.read_json()
                    template_id = text_of(body.get("templateId")).strip()
                    if template_id == "__default":
                        target = state["template"]
                    else:
                        target = next((item for item in state["templates"]
                                       if text_of(item.get("id")) == template_id), None)
                    if target is None:
                        raise ValueError("没有找到要保存的模板。")
                    for field in ("name", "country", "creatorType", "language", "platform", "subject", "body"):
                        if field in body:
                            target[field] = text_of(body[field])
                    if template_id != "__default" and not text_of(target.get("name")).strip():
                        target["name"] = "未命名模板"
                    save_state(state)
                    self.send_json({"ok": True})
                    return

                if path == "/api/templates/new":
                    template = {
                        "id": uuid.uuid4().hex,
                        "name": "新建模板",
                        "country": "",
                        "creatorType": "",
                        "language": "",
                        "platform": "",
                        "subject": text_of(state["template"].get("subject")),
                        "body": text_of(state["template"].get("body")),
                    }
                    state["templates"].append(template)
                    save_state(state)
                    self.send_json({"ok": True, "template": template})
                    return

                if path == "/api/templates/delete":
                    body = self.read_json()
                    template_id = text_of(body.get("templateId")).strip()
                    if not template_id or template_id == "__default":
                        raise ValueError("请选择一个自定义模板。")
                    before = len(state["templates"])
                    state["templates"] = [item for item in state["templates"]
                                          if text_of(item.get("id")) != template_id]
                    if len(state["templates"]) == before:
                        raise ValueError("没有找到要删除的模板。")
                    save_state(state)
                    self.send_json({"ok": True})
                    return

                if path == "/api/settings":
                    body = self.read_json()
                    for field in ("fromName", "fromEmail", "replyTo", "smtpHost", "username", "password"):
                        if body.get(field) is not None:
                            state["settings"][field] = text_of(body[field])
                    for field in ("smtpPort", "dailyLimit", "delaySeconds"):
                        if body.get(field) is not None:
                            state["settings"][field] = int(body[field])
                    if body.get("useSsl") is not None:
                        state["settings"]["useSsl"] = bool(body["useSsl"])
                    save_state(state)
                    self.send_json({"ok": True})
                    return

                if path == "/api/import":
                    body = self.read_json()
                    filename = text_of(body.get("filename")).strip()
                    content = text_of(body.get("contentBase64")).strip()
                    if not filename or not content:
                        raise ValueError("缺少文件名或文件内容。")
                    try:
                        raw = base64.b64decode(content, validate=False)
                    except (binascii.Error, ValueError) as exc:
                        raise ValueError("文件内容不是合法的 Base64。") from exc
                    result = import_lead_file(state, filename, raw, bool(body.get("replace")))
                    save_state(state)
                    self.send_json(result)
                    return

                if path == "/api/preview":
                    body = self.read_json()
                    lead = None
                    if body.get("leadId"):
                        lead = next((item for item in state["leads"]
                                     if item.get("id") == text_of(body["leadId"])), None)
                    elif state["leads"]:
                        lead = state["leads"][0]
                    if lead is None:
                        raise ValueError("没有找到红人。")
                    self.send_json(render_lead_message(state, lead))
                    return

                if path == "/api/send":
                    body = self.read_json()
                    ids = [text_of(item) for item in _as_list(body.get("leadIds"))]
                    results = send_leads(state, ids, bool(body.get("dryRun")))
                    save_state(state)
                    self.send_json({"ok": True, "results": results})
                    return

                if path == "/api/lead/delete":
                    body = self.read_json()
                    lead_id = text_of(body.get("leadId")).strip()
                    if not lead_id:
                        raise ValueError("leadId is required.")
                    state["leads"] = [lead for lead in state["leads"] if lead.get("id") != lead_id]
                    save_state(state)
                    self.send_json({"ok": True})
                    return

                if path == "/api/lead/reset":
                    body = self.read_json()
                    lead_id = text_of(body.get("leadId")).strip()
                    if not lead_id:
                        raise ValueError("leadId is required.")
                    lead = next((item for item in state["leads"] if item.get("id") == lead_id), None)
                    if lead is None:
                        raise ValueError("没有找到红人。")
                    lead["status"] = "new"
                    lead["sentAt"] = ""
                    lead["lastError"] = ""
                    save_state(state)
                    self.send_json({"ok": True})
                    return

                raise ValueError(f"未知接口：POST {path}")
        except SendBlocked as exc:
            self.send_json({"ok": False, "error": str(exc)}, 400)
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            self.send_json({"ok": False, "error": f"{exc}"}, 400)


def main() -> int:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    WEB_ROOT.mkdir(parents=True, exist_ok=True)
    get_state()

    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as exc:
        print(f"启动失败：端口 {PORT} 用不了（{exc}）。换个端口：PORT=8788 python server/app.py")
        return 1

    url = f"http://127.0.0.1:{PORT}/"
    print(f"KOL 红人邮件系统已启动：{url}")
    if HOST not in ("127.0.0.1", "localhost"):
        print(f"同一局域网的同事/评委可以通过 http://<你的IP>:{PORT}/ 打开。")
    if not os.environ.get("KOL_NO_BROWSER"):
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
