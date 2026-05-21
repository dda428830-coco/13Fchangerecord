#!/usr/bin/env python3
"""
Track 13F holding changes for selected institutional managers and push updates.

Data source: SEC EDGAR submissions and 13F information table XML files.
State storage: GitHub Gist when GIST_ID and GITHUB_TOKEN are configured.
Push channel: Discord when DISCORD_WEBHOOK_URL is configured; Telegram is also supported.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_ARCHIVES = "https://www.sec.gov/Archives/edgar/data/{cik_no_zero}/{accession_no_dashless}/"
GITHUB_GIST_API = "https://api.github.com/gists/{gist_id}"
DEFAULT_SEC_USER_AGENT = (
    "13Fchangerecord/1.0 "
    "(https://github.com/dda428830-coco/13Fchangerecord; set SEC_USER_AGENT with contact email)"
)
STATE_FILE_NAME = "state.json"
STATE_PATH = Path(os.getenv("STATE_PATH", "data/holdings_state.json"))
MIN_VALUE_CHANGE_USD = 5_000_000
SITUATIONAL_AWARENESS_MIN_VALUE_CHANGE_USD = 1_000_000
UNCHANGED_VALUE_DISPLAY_USD = 100_000_000

TRACKED_MANAGERS = [
    {"name": "Berkshire Hathaway", "short_name": "伯克希尔", "cik": "0001067983", "icon": "🟦"},
    {"name": "Bridgewater Associates", "short_name": "桥水", "cik": "0001350694", "icon": "🟩"},
    {"name": "Soros Fund Management", "short_name": "索罗斯", "cik": "0001029160", "icon": "🟨"},
    {"name": "Appaloosa", "short_name": "Appaloosa", "cik": "0001656456", "icon": "🟥"},
    {"name": "Situational Awareness LP", "short_name": "Situational Awareness", "cik": "0002045724", "icon": "🟪"},
]

CUSIP_SYMBOLS = {
    "02079K107": "GOOG",
    "02079K305": "GOOGL",
    "00215W100": "APG",
    "023135106": "AMZN",
    "025816109": "AXP",
    "037833100": "AAPL",
    "060505104": "BAC",
    "11135F101": "AVTR",
    "126408103": "CSX",
    "14040H105": "COF",
    "16119P108": "CHTR",
    "166764100": "CVX",
    "191216100": "KO",
    "21036P108": "STZ",
    "23918K108": "DVA",
    "247361702": "DAL",
    "25754A201": "DPZ",
    "422806208": "HEI",
    "457669307": "INTR",
    "46434G764": "IUSB",
    "500754106": "KHC",
    "501044101": "KR",
    "526057104": "LEN",
    "526057302": "LEN.B",
    "530909308": "LLYVK",
    "531229755": "FWONK",
    "55616P104": "M",
    "573874104": "MRVL",
    "57636Q104": "MA",
    "594918104": "MSFT",
    "595112103": "MU",
    "615369105": "MCO",
    "650111107": "NYT",
    "67066G104": "NVDA",
    "670346105": "NUE",
    "674599105": "OXY",
    "693718108": "PCVX",
    "73278L105": "POOL",
    "829933100": "SIRI",
    "858119100": "STLD",
    "871607107": "SNPS",
    "874039100": "TSM",
    "907818108": "UNP",
    "91324P102": "UNH",
    "922908363": "VTI",
    "92826C839": "V",
    "G0403H108": "AON",
    "H1467J104": "CB",
    "M87915274": "TEVA",
}


@dataclass(frozen=True)
class Filing:
    cik: str
    accession: str
    report_date: str
    filing_date: str
    primary_doc: str
    form: str

    @property
    def archive_base(self) -> str:
        return SEC_ARCHIVES.format(
            cik_no_zero=str(int(self.cik)),
            accession_no_dashless=self.accession.replace("-", ""),
        )

    @property
    def filing_url(self) -> str:
        return self.archive_base + self.primary_doc


def _user_agent() -> str:
    return os.getenv("SEC_USER_AGENT") or DEFAULT_SEC_USER_AGENT


def valid_sec_user_agent() -> bool:
    user_agent = os.getenv("SEC_USER_AGENT", "").strip()
    return bool(user_agent and "@" in user_agent and "example.com" not in user_agent and len(user_agent) >= 20)


def fetch_json(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    body = fetch_bytes(url, headers=headers)
    return json.loads(body.decode("utf-8"))


def fetch_bytes(url: str, headers: dict[str, str] | None = None) -> bytes:
    parsed = urllib.parse.urlparse(url)
    req_headers = {
        "User-Agent": _user_agent(),
        "Accept": "application/json, application/xml, text/xml, text/html, */*",
        "Accept-Encoding": "gzip, deflate",
    }
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            return gzip.decompress(body)
        return body


def request_json(
    url: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": _user_agent()}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_local_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return normalize_state(raw)


def save_local_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")


def normalize_state(raw: dict[str, Any]) -> dict[str, Any]:
    if "managers" in raw:
        normalized: dict[str, Any] = {}
        for cik, item in raw.get("managers", {}).items():
            normalized[cik.zfill(10)] = {
                "last_report_date": item.get("report_date", ""),
                "last_filing_date": item.get("filing_date", ""),
            }
        return normalized
    return raw if isinstance(raw, dict) else {}


def load_state() -> tuple[dict[str, Any], str]:
    gist_id = os.getenv("GIST_ID") or os.getenv("GITHUB_GIST_ID")
    token = os.getenv("GITHUB_TOKEN")
    if gist_id and token:
        try:
            gist = request_json(GITHUB_GIST_API.format(gist_id=gist_id), token=token)
            content = gist.get("files", {}).get(STATE_FILE_NAME, {}).get("content", "{}")
            state = normalize_state(json.loads(content or "{}"))
            print("[state] loaded from GitHub Gist")
            return state, "gist"
        except Exception as exc:  # noqa: BLE001 - fallback must not interrupt tracking
            print(f"[state] Gist unavailable ({exc}); using local file {STATE_PATH}")
    else:
        print("[state] Gist env not configured; using local file")
    return load_local_state(STATE_PATH), "local"


def save_state(state: dict[str, Any], source: str) -> None:
    if source == "gist":
        gist_id = os.getenv("GIST_ID") or os.getenv("GITHUB_GIST_ID")
        token = os.getenv("GITHUB_TOKEN")
        if gist_id and token:
            try:
                payload = {
                    "files": {
                        STATE_FILE_NAME: {
                            "content": json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False)
                        }
                    }
                }
                request_json(GITHUB_GIST_API.format(gist_id=gist_id), method="PATCH", payload=payload, token=token)
                print("[state] saved to GitHub Gist")
                return
            except Exception as exc:  # noqa: BLE001
                print(f"[state] failed to save Gist ({exc}); saving local fallback")
    save_local_state(STATE_PATH, state)
    print(f"[state] saved to local file {STATE_PATH}")


def recent_13f_filings(cik: str, limit: int = 2) -> list[Filing]:
    data = fetch_json(SEC_SUBMISSIONS.format(cik=cik.zfill(10)))
    recent = data.get("filings", {}).get("recent", {})
    filings: list[Filing] = []
    for idx, form in enumerate(recent.get("form", [])):
        if form in {"13F-HR", "13F-HR/A"}:
            filings.append(
                Filing(
                    cik=cik.zfill(10),
                    accession=recent["accessionNumber"][idx],
                    report_date=recent["reportDate"][idx],
                    filing_date=recent["filingDate"][idx],
                    primary_doc=recent["primaryDocument"][idx],
                    form=form,
                )
            )
            if len(filings) >= limit:
                break
    return filings


def find_info_table_url(filing: Filing) -> str:
    items = fetch_json(filing.archive_base + "index.json").get("directory", {}).get("item", [])
    candidates: list[str] = []
    for item in items:
        name = item.get("name", "")
        lower = name.lower()
        if lower.endswith(".xml") and "primary_doc" not in lower:
            candidates.append(name)
    if not candidates:
        raise RuntimeError(f"No information table XML found for {filing.accession}")
    preferred = [
        name
        for name in candidates
        if any(token in name.lower() for token in ("infotable", "form13f", "13f", "xml"))
    ]
    return filing.archive_base + (preferred[0] if preferred else candidates[0])


def _strip_namespace(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _child_text(node: ET.Element, child_name: str) -> str:
    for child in node:
        if _strip_namespace(child.tag) == child_name:
            return (child.text or "").strip()
    return ""


def _nested_text(node: ET.Element, parent_name: str, child_name: str) -> str:
    for child in node:
        if _strip_namespace(child.tag) == parent_name:
            return _child_text(child, child_name)
    return ""


def _to_int(text: str) -> int:
    digits = re.sub(r"[^0-9-]", "", text or "")
    if not digits or digits == "-":
        return 0
    return int(digits)


def parse_holdings(xml_bytes: bytes) -> dict[str, dict[str, Any]]:
    root = ET.fromstring(xml_bytes)
    holdings: dict[str, dict[str, Any]] = {}
    for node in root.iter():
        if _strip_namespace(node.tag) != "infoTable":
            continue
        issuer = _child_text(node, "nameOfIssuer")
        cusip = _child_text(node, "cusip")
        ticker = _child_text(node, "ticker")
        title = _child_text(node, "titleOfClass")
        value_usd = _to_int(_child_text(node, "value"))
        shares = _to_int(_nested_text(node, "shrsOrPrnAmt", "sshPrnamt"))
        put_call = _child_text(node, "putCall")
        key_id = ticker or cusip
        key = "|".join([key_id.upper(), cusip.upper(), title.upper(), put_call.upper()])
        if key in holdings:
            holdings[key]["value_usd"] += value_usd
            holdings[key]["shares"] += shares
            continue
        holdings[key] = {
            "issuer": issuer,
            "ticker": ticker,
            "cusip": cusip,
            "class": title,
            "put_call": put_call,
            "value_usd": value_usd,
            "shares": shares,
        }
    return holdings


def fetch_holdings(filing: Filing) -> dict[str, dict[str, Any]]:
    return parse_holdings(fetch_bytes(find_info_table_url(filing)))


def manager_min_change(manager: dict[str, str]) -> int:
    if manager["name"] == "Situational Awareness LP":
        return SITUATIONAL_AWARENESS_MIN_VALUE_CHANGE_USD
    return MIN_VALUE_CHANGE_USD


def compare_holdings(
    old: dict[str, dict[str, Any]],
    new: dict[str, dict[str, Any]],
    min_change_usd: int,
) -> dict[str, list[dict[str, Any]]]:
    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    increased: list[dict[str, Any]] = []
    decreased: list[dict[str, Any]] = []
    unchanged_value: list[dict[str, Any]] = []

    for key, holding in new.items():
        if key not in old:
            if holding.get("value_usd", 0) >= min_change_usd:
                added.append(holding)
            continue
        previous = old[key]
        share_delta = holding.get("shares", 0) - previous.get("shares", 0)
        value_delta = holding.get("value_usd", 0) - previous.get("value_usd", 0)
        if abs(value_delta) < min_change_usd:
            continue
        enriched = {
            **holding,
            "old_shares": previous.get("shares", 0),
            "old_value_usd": previous.get("value_usd", 0),
            "share_delta": share_delta,
            "value_delta_usd": value_delta,
        }
        if share_delta > 0:
            increased.append(enriched)
        elif share_delta < 0:
            decreased.append(enriched)
        elif abs(value_delta) >= UNCHANGED_VALUE_DISPLAY_USD:
            unchanged_value.append(enriched)

    for key, holding in old.items():
        if key not in new and holding.get("value_usd", 0) >= min_change_usd:
            removed.append(holding)

    by_value = lambda h: h.get("value_usd", 0)
    by_abs_delta = lambda h: abs(h.get("value_delta_usd", 0))
    return {
        "added": sorted(added, key=by_value, reverse=True),
        "removed": sorted(removed, key=by_value, reverse=True),
        "increased": sorted(increased, key=by_abs_delta, reverse=True),
        "decreased": sorted(decreased, key=by_abs_delta, reverse=True),
        "unchanged_value": sorted(unchanged_value, key=by_abs_delta, reverse=True),
    }


def total_value(holdings: dict[str, dict[str, Any]]) -> int:
    return sum(h.get("value_usd", 0) for h in holdings.values())


def money(value: int, signed: bool = False) -> str:
    sign = ""
    if value < 0:
        sign = "-"
    elif signed and value > 0:
        sign = "+"
    dollars = abs(value)
    if dollars >= 1_000_000_000:
        return f"{sign}${dollars / 1_000_000_000:.2f}B"
    if dollars >= 1_000_000:
        return f"{sign}${dollars / 1_000_000:.1f}M"
    return f"{sign}${dollars:,}"


def signed_int(value: int) -> str:
    return f"{value:+,}"


def pct_change(old: int, delta: int) -> str:
    if old == 0:
        return "n/a"
    return f"{delta / old * 100:+.1f}%"


def position_name(holding: dict[str, Any]) -> str:
    ticker = holding.get("ticker") or CUSIP_SYMBOLS.get(holding.get("cusip", "").upper())
    put_call = f" {holding['put_call']}" if holding.get("put_call") else ""
    if ticker:
        return f"{ticker}{put_call}"
    return f"{holding.get('issuer', 'UNKNOWN')}{put_call}"


def issuer_key(holding: dict[str, Any]) -> str:
    return (
        holding.get("ticker")
        or CUSIP_SYMBOLS.get(holding.get("cusip", "").upper())
        or holding.get("issuer")
        or "UNKNOWN"
    )


def weight(value_usd: int, new_total: int) -> str:
    if new_total <= 0:
        return "n/a"
    return f"{value_usd / new_total * 100:.1f}%"


def format_added(title: str, rows: list[dict[str, Any]], new_total: int) -> list[str]:
    if not rows:
        return []
    lines = [f"{title}（{len(rows)}支）"]
    for holding in rows[:10]:
        lines.append(
            f"- {position_name(holding)}：{holding.get('shares', 0):,} 股 | "
            f"{money(holding.get('value_usd', 0))} | 占新总仓 {weight(holding.get('value_usd', 0), new_total)}"
        )
    if len(rows) > 10:
        lines.append(f"- ...还有 {len(rows) - 10} 支")
    return lines


def format_removed(title: str, rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return []
    lines = [f"{title}（{len(rows)}支）"]
    for holding in rows[:10]:
        lines.append(
            f"- {position_name(holding)}：{holding.get('shares', 0):,} 股 | "
            f"{money(holding.get('value_usd', 0))}"
        )
    if len(rows) > 10:
        lines.append(f"- ...还有 {len(rows) - 10} 支")
    return lines


def format_changed(title: str, rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return []
    lines = [f"{title}（{len(rows)}支，按变化市值排序）"]
    for holding in rows[:10]:
        lines.append(
            f"- {position_name(holding)}：{signed_int(holding['share_delta'])} 股 "
            f"({pct_change(holding.get('old_shares', 0), holding['share_delta'])}) | "
            f"市值变化 {money(holding['value_delta_usd'], signed=True)}"
        )
    if len(rows) > 10:
        lines.append(f"- ...还有 {len(rows) - 10} 支")
    return lines


def format_unchanged(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return []
    lines = [f"⚪ 股数不变（{len(rows)}支，市值变化 > $100M）"]
    for holding in rows[:10]:
        lines.append(
            f"- {position_name(holding)}：{holding.get('shares', 0):,} 股 | "
            f"市值变化 {money(holding['value_delta_usd'], signed=True)}"
        )
    if len(rows) > 10:
        lines.append(f"- ...还有 {len(rows) - 10} 支")
    return lines


def quarter_label(report_date: str) -> str:
    year, month, _day = report_date.split("-")
    quarter = (int(month) - 1) // 3 + 1
    return f"Q{quarter} {year}"


def build_manager_message(result: dict[str, Any]) -> str:
    manager = result["manager"]
    filing = result["filing"]
    previous_filing = result["previous_filing"]
    diff = result["diff"]
    old_total = result["old_total"]
    new_total = result["new_total"]
    lines = [
        "━━━━━━━━━━━━━━━━━━━━",
        f"{manager.get('icon', '📊')} **【{manager['name']}】**",
        "📊 持仓变化",
        f"报告期：{filing.report_date} | 对比：{previous_filing.report_date}",
        f"总持仓：{money(old_total)} → {money(new_total)}（变化 {money(new_total - old_total, signed=True)}）",
        "",
    ]
    lines += format_added("🆕 新增", diff["added"], new_total)
    lines += format_changed("📈 加仓", diff["increased"])
    lines += format_changed("📉 减仓", diff["decreased"])
    lines += format_removed("❌ 清仓", diff["removed"])
    lines += format_unchanged(diff["unchanged_value"])
    lines += ["", f"SEC 文件：{filing.filing_url}"]
    return "\n".join(lines)


def biggest_name(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "-"
    return position_name(rows[0])


def build_quarterly_summary(report_date: str, results: list[dict[str, Any]]) -> str:
    included = [r for r in results if r["filing"].report_date == report_date]
    lines = [
        f"🗓 **{quarter_label(report_date)} 机构持仓季度汇总**",
        f"已收录：{len(included)}/{len(TRACKED_MANAGERS)} 家",
        "",
    ]
    for result in included:
        manager = result["manager"]
        new_total = result["new_total"]
        delta = new_total - result["old_total"]
        lines += [
            f"**{manager['short_name']}**",
            f"总仓位：{money(new_total)}（环比 {money(delta, signed=True)}）",
            f"最大加仓：{biggest_name(result['diff']['increased'])}",
            f"最大减仓：{biggest_name(result['diff']['decreased'])}",
            "",
        ]

    common_added = common_names(included, "added")
    common_removed = common_names(included, "removed")
    lines += [
        "共同新增：",
        format_common(common_added),
        "",
        "共同清仓：",
        format_common(common_removed),
    ]
    return "\n".join(lines)


def common_names(results: list[dict[str, Any]], bucket: str) -> list[str]:
    counts: dict[str, int] = {}
    display: dict[str, str] = {}
    for result in results:
        seen: set[str] = set()
        for holding in result["diff"][bucket]:
            key = issuer_key(holding)
            if key in seen:
                continue
            seen.add(key)
            counts[key] = counts.get(key, 0) + 1
            display[key] = position_name(holding)
    return [display[key] for key, count in sorted(counts.items()) if count >= 2]


def format_common(names: list[str]) -> str:
    if not names:
        return "无"
    shown = names[:8]
    suffix = f"\n...还有 {len(names) - len(shown)} 个" if len(names) > len(shown) else ""
    return "\n".join(f"- {name}" for name in shown) + suffix


def send_discord(message: str) -> bool:
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        return False
    for chunk in split_message(message, limit=1900):
        payload = json.dumps({"content": chunk}).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": _user_agent()},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
    return True


def split_message(message: str, limit: int) -> list[str]:
    if len(message) <= limit:
        return [message]
    chunks: list[str] = []
    current = ""
    for line in message.splitlines():
        addition = line if not current else "\n" + line
        if len(current) + len(addition) <= limit:
            current += addition
            continue
        if current:
            chunks.append(current)
        current = line
    if current:
        chunks.append(current)
    return chunks


def send_telegram(message: str) -> bool:
    token = os.getenv("TG_BOT_TOKEN")
    chat_id = os.getenv("TG_CHAT_ID")
    if not token or not chat_id:
        return False
    api_url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": message, "disable_web_page_preview": "true"}
    ).encode("utf-8")
    req = urllib.request.Request(api_url, data=payload, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()
    return True


def send_message(message: str) -> None:
    sent = send_discord(message)
    if send_telegram(message):
        sent = True
    if not sent:
        print(message)
        print()


def process_manager(manager: dict[str, str], state: dict[str, Any]) -> dict[str, Any] | None:
    filings = recent_13f_filings(manager["cik"], limit=2)
    if len(filings) < 2:
        raise RuntimeError(f"{manager['name']}: not enough 13F filings to compare")
    filing, previous_filing = filings[0], filings[1]
    current_holdings = fetch_holdings(filing)
    previous_holdings = fetch_holdings(previous_filing)
    diff = compare_holdings(previous_holdings, current_holdings, manager_min_change(manager))
    state_key = manager["cik"].zfill(10)
    saved = state.get(state_key, {})
    already_sent = (
        saved.get("last_report_date") == filing.report_date
        and saved.get("last_filing_date") == filing.filing_date
    )
    return {
        "manager": manager,
        "filing": filing,
        "previous_filing": previous_filing,
        "diff": diff,
        "old_total": total_value(previous_holdings),
        "new_total": total_value(current_holdings),
        "already_sent": already_sent,
    }


def update_state_for_result(state: dict[str, Any], result: dict[str, Any]) -> None:
    manager = result["manager"]
    filing = result["filing"]
    state[manager["cik"].zfill(10)] = {
        "last_report_date": filing.report_date,
        "last_filing_date": filing.filing_date,
    }


def maybe_quarterly_summary(state: dict[str, Any], results: list[dict[str, Any]]) -> str | None:
    changed_reports = {r["filing"].report_date for r in results if not r["already_sent"]}
    sent = state.setdefault("_quarterly_summary_sent", {})
    for report_date in sorted(changed_reports):
        same_period_count = sum(
            1
            for manager in TRACKED_MANAGERS
            if state.get(manager["cik"].zfill(10), {}).get("last_report_date") == report_date
        )
        if same_period_count >= 3 and not sent.get(report_date):
            sent[report_date] = datetime.now(timezone.utc).isoformat()
            return build_quarterly_summary(report_date, results)
    return None


def run(preview: int | None = None, status_only: bool = False, dry_run: bool = False) -> int:
    state, state_source = load_state()
    selected = TRACKED_MANAGERS[:preview] if preview else TRACKED_MANAGERS
    messages: list[str] = []
    results: list[dict[str, Any]] = []

    if os.getenv("GITHUB_ACTIONS") == "true" and not valid_sec_user_agent():
        message = (
            "SEC_USER_AGENT is missing or invalid, so SEC EDGAR returned 403 Forbidden.\n"
            "Please add a repo secret named SEC_USER_AGENT with a real contact email, for example:\n"
            "13Fchangerecord dda428830-coco your-email@example.com"
        )
        if dry_run:
            print(message)
            print(f"[state] dry-run: state source was {state_source}; no state saved")
        else:
            send_message(message)
        return 2

    for manager in selected:
        try:
            if status_only:
                filings = recent_13f_filings(manager["cik"], limit=1)
                if filings:
                    filing = filings[0]
                    print(f"{manager['name']}: latest {filing.form}, report {filing.report_date}, filed {filing.filing_date}")
                else:
                    print(f"{manager['name']}: no 13F-HR filing found")
                continue

            result = process_manager(manager, state)
            if not result:
                continue
            results.append(result)
            if dry_run or not result["already_sent"]:
                messages.append(build_manager_message(result))
            if not dry_run:
                update_state_for_result(state, result)
            time.sleep(0.2)
        except (urllib.error.URLError, RuntimeError, ET.ParseError, KeyError, json.JSONDecodeError) as exc:
            messages.append(f"{manager['name']}: failed - {exc}")

    if status_only:
        return 0

    if not dry_run and not preview:
        summary = maybe_quarterly_summary(state, results)
        if summary:
            messages.append(summary)

    if messages:
        for message in messages:
            if dry_run:
                print(message)
                print()
            else:
                send_message(message)
    else:
        print("No new 13F holding changes.")

    if dry_run:
        print(f"[state] dry-run: state source was {state_source}; no state saved")
    else:
        save_state(state, state_source)
    return 0


def show_schedule() -> int:
    print("Recommended schedule: run once every 6 hours on weekdays, or daily after market close.")
    print("13F filings are quarterly and can arrive up to 45 days after quarter-end.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Track 13F holding changes and push updates.")
    parser.add_argument("--preview", type=int, help="Run only the first N managers and print output.")
    parser.add_argument("--dry-run", action="store_true", help="Print messages without sending them.")
    parser.add_argument("--status", action="store_true", help="Show latest 13F filing status only.")
    parser.add_argument("--schedule", action="store_true", help="Show recommended run schedule.")
    args = parser.parse_args()
    if args.schedule:
        return show_schedule()
    return run(preview=args.preview, status_only=args.status, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
