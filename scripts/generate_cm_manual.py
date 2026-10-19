#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CM 手册生成器（Markdown 版）

排版参照 template/南山铝业600219.SH_CM 手册.pdf：
  · 顶部：代码 / 报告指标日期 / 当前股价 / 当前市值 / 静态 PE/PB / 动态 PE / 市净率
  · 主表：最近 11 个年度的盈利能力、资产负债、利润与现金流、股本
  · 右侧栏内容：股价走势、公司概况、年均增长率、季度营收/净利润、每股分红
  · 底部：口径说明与免责声明

数据源优先级（遵循 a-stock-data 的原则：能用腾讯 / 交易所 / 新浪，就不用东财）：
  · 腾讯财经  qt.gtimg.cn / web.ifzq.gtimg.cn —— 实时行情、日/周/月 K 线、年末收盘价
  · 新浪财经  quotes.sina.cn / vip.stock.finance.sina.com.cn
      - CompanyFinanceService.getFinanceReport2022  财务三表（fzb/lrb/llb）
      - vCI_CorpInfo                                公司资料
      - vCI_CorpOtherInfo                           申万行业
  · 东方财富（仅独有数据，全部走 em_get() 串行限流）
      - 扣非归母净利润（新浪三表没有该科目）
      - 按报告期归档的分红送转（新浪分红页只有公告日）
      - 北交所 K 线兜底（腾讯 K 线不支持北交所）
      - 新浪三表/公司资料失败时的整表兜底
  · mootdx：仅财务快照 / F10；其行情命令 2026-09 起失效（#52），本脚本不依赖

用法：
    python3 scripts/generate_cm_manual.py 600219.SH
    python3 scripts/generate_cm_manual.py 600219.SH --report-date 2026-05-06
    python3 scripts/generate_cm_manual.py 600219.SH --pdf        # Markdown + 一页 PDF
    python3 scripts/generate_cm_manual.py 600519 -o output/贵州茅台_CM手册.md --no-charts

仅依赖 requests；图表用标准库生成 SVG，不依赖 matplotlib。
PDF 生成需要 xelatex、rsvg-convert（librsvg）与中文字体；
默认 PingFang SC，可用环境变量 CM_CJK_FONT 指定其他字体（如 Noto Sans CJK SC）。
"""

import argparse
import html
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests
except ImportError:
    sys.stderr.write("缺少依赖 requests，请先执行：pip install requests\n")
    sys.exit(1)


# ---------------------------------------------------------------------------
# 常量与通用工具
# ---------------------------------------------------------------------------

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")

TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="
TENCENT_KLINE_HOSTS = [
    "https://web.ifzq.gtimg.cn",
    "https://proxy.finance.qq.com/ifzqgtimg",
    "https://ifzq.gtimg.cn",
]
EM_F10_URL = "https://datacenter.eastmoney.com/securities/api/data/get"
EM_DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept": "*/*"})

EM_MIN_INTERVAL = 0.8
_em_last_call = [0.0]


class DataError(RuntimeError):
    """数据源返回异常，或必要数据缺失。"""


def log(msg: str) -> None:
    if not QUIET:
        sys.stderr.write(msg + "\n")


QUIET = False


# ---------------------------------------------------------------------------
# 代码归一化 / 市场前缀
# ---------------------------------------------------------------------------

SH_INDEX = {"000300", "000905", "000016", "000688", "000852", "000010"}


def norm_ticker(code: str) -> str:
    """任意受支持写法 → 纯 6 位代码。"""
    raw = str(code).strip()
    m = re.match(r"^(?:(sh|sz|bj)(\d{6})|(\d{6})(?:\.(sh|sz|bj|xshg|xshe))?)$",
                 raw, re.IGNORECASE)
    if not m:
        raise ValueError("无法识别股票代码 %r；支持 600519 / SH600519 / 600519.SH" % code)
    return m.group(2) or m.group(3)


def get_prefix(code: str) -> str:
    """6 位代码 → sh / sz / bj。支持显式前缀与后缀。"""
    c = str(code).lower().strip()
    if c.endswith((".sh", ".sz", ".bj")):
        return c[-2:]
    if c.endswith((".xshg", ".xshe")):
        return "sh" if c.endswith(".xshg") else "sz"
    if c.startswith(("sh", "sz", "bj")):
        return c[:2]
    d = norm_ticker(c)
    if d.startswith("92"):
        return "bj"
    if d.startswith(("5", "6", "9")):
        return "sh"
    if d.startswith(("4", "8")):
        return "bj"
    if d in SH_INDEX:
        return "sh"
    return "sz"


def full_code(code: str) -> str:
    return norm_ticker(code) + "." + get_prefix(code).upper()


def secucode(code: str) -> str:
    return full_code(code)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _sleep_between_em() -> None:
    wait = EM_MIN_INTERVAL - (time.time() - _em_last_call[0])
    if wait > 0:
        time.sleep(wait + 0.15)


def http_get(url: str, params: Optional[dict] = None, headers: Optional[dict] = None,
             timeout: int = 25, retries: int = 3) -> requests.Response:
    merged = {"User-Agent": UA}
    merged.update(headers or {})
    last_exc = None
    for attempt in range(retries):
        try:
            resp = SESSION.get(url, params=params, headers=merged, timeout=timeout)
            if resp.status_code >= 400:
                raise DataError("HTTP %s: %s" % (resp.status_code, resp.url))
            return resp
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt + 1 < retries:
                time.sleep(0.8 * (attempt + 1))
    raise DataError("请求失败 %s: %s" % (url, last_exc))


def em_get(url: str, params: dict, headers: Optional[dict] = None,
           timeout: int = 25) -> requests.Response:
    _sleep_between_em()
    h = {"Referer": "https://emweb.securities.eastmoney.com/"}
    h.update(headers or {})
    try:
        return http_get(url, params=params, headers=h, timeout=timeout)
    finally:
        _em_last_call[0] = time.time()


def em_f10(report: str, filter_str: str, page_size: int = 30,
           sort_column: Optional[str] = "REPORT_DATE",
           allow_empty: bool = False) -> List[dict]:
    """东方财富 F10 报表接口。失败时回退到 datacenter-web。

    sort_column=None 用于没有 REPORT_DATE 列的报表（如公司基本资料）。
    allow_empty=True 时，9201“返回数据为空”视为空表而不是异常。
    """
    params = {
        "type": report,
        "sty": "ALL",
        "filter": filter_str,
        "p": "1",
        "ps": str(page_size),
        "source": "HSF10",
        "client": "PC",
    }
    if sort_column:
        params["sr"] = "-1"
        params["st"] = sort_column
    try:
        resp = em_get(EM_F10_URL, params)
        payload = resp.json()
        if payload.get("code") == 0:
            return ((payload.get("result") or {}).get("data")) or []
        if allow_empty and payload.get("code") == 9201:
            return []
        raise DataError("东财 %s 返回错误：%s %s" %
                        (report, payload.get("code"), payload.get("message")))
    except Exception as first_exc:  # noqa: BLE001
        # datacenter-web 回退
        params2 = {
            "reportName": report,
            "columns": "ALL",
            "filter": filter_str,
            "pageNumber": "1",
            "pageSize": str(page_size),
            "source": "WEB",
            "client": "WEB",
        }
        if sort_column:
            params2["sortColumns"] = sort_column
            params2["sortTypes"] = "-1"
        try:
            resp = em_get(EM_DATACENTER_URL, params2)
            payload = resp.json()
            data = ((payload.get("result") or {}).get("data")) or []
            if data or allow_empty:
                return data
        except Exception:
            pass
        raise DataError("东方财富 %s 取数失败：%s" % (report, first_exc))


def curl_json(url: str, params: Optional[dict] = None, referer: str = "",
              timeout: int = 30) -> dict:
    """用 curl 直连取 JSON（绕过代理）。

    东方财富 push2his 会按 TLS 指纹拒绝 Python requests/urllib
    （RemoteDisconnected），curl 可以正常取到；北交所 K 线只能走这条路。
    """
    q = url + ("?" + urllib.parse.urlencode(params) if params else "")
    cmd = ["curl", "-sS", "--noproxy", "*", "-m", str(timeout),
           "-H", "User-Agent: " + UA]
    if referer:
        cmd += ["-H", "Referer: " + referer]
    cmd.append(q)
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
    except (OSError, subprocess.SubprocessError) as exc:
        raise DataError("curl 调用失败：%s" % exc)
    if result.returncode != 0 or not result.stdout.strip():
        err = result.stderr.decode("utf-8", "replace")[:200]
        raise DataError("curl 请求失败：%s" % err)
    try:
        return json.loads(result.stdout.decode("utf-8", "replace"))
    except ValueError as exc:
        raise DataError("curl 返回不是 JSON：%s" % exc)


def em_datacenter(report: str, filter_str: str, page_size: int = 50,
                  sort_columns: str = "REPORT_DATE",
                  sort_types: str = "-1") -> List[dict]:
    params = {
        "reportName": report,
        "columns": "ALL",
        "filter": filter_str,
        "pageNumber": "1",
        "pageSize": str(page_size),
        "sortColumns": sort_columns,
        "sortTypes": sort_types,
        "source": "WEB",
        "client": "WEB",
    }
    resp = em_get(EM_DATACENTER_URL, params)
    payload = resp.json()
    data = ((payload.get("result") or {}).get("data")) or []
    return data


# ---------------------------------------------------------------------------
# 腾讯行情 / K 线
# ---------------------------------------------------------------------------

def tencent_quote(code: str) -> dict:
    symbol = get_prefix(code) + norm_ticker(code)
    resp = http_get(TENCENT_QUOTE_URL + symbol, timeout=15)
    text = resp.content.decode("gbk", "replace")
    m = re.search(r'v_%s="([^"]*)"' % re.escape(symbol), text)
    if not m:
        raise DataError("腾讯行情没有返回 %s 的数据" % symbol)
    fields = m.group(1).split("~")
    if len(fields) < 50:
        raise DataError("腾讯行情 %s 字段数异常" % symbol)

    def f(idx: int) -> Optional[float]:
        try:
            v = fields[idx]
            return float(v) if v not in ("", "-", "None") else None
        except (ValueError, IndexError):
            return None

    return {
        "symbol": symbol,
        "name": fields[1],
        "price": f(3),
        "last_close": f(4),
        "change_pct": f(32),
        "pe_ttm": f(39),
        "float_mcap_yi": f(44),
        "mcap_yi": f(45),
        "pb": f(46),
    }


def tencent_kline(code: str, period: str = "day", start: str = "", end: str = "",
                  count: int = 640, adjust: str = "") -> List[dict]:
    """腾讯 K 线。period: day/week/month；adjust: ''/qfq/hfq。"""
    symbol = get_prefix(code) + norm_ticker(code)
    param = "%s,%s,%s,%s,%d,%s" % (symbol, period, start, end, count, adjust)
    errors = []
    for host in TENCENT_KLINE_HOSTS:
        try:
            resp = http_get(host + "/appstock/app/fqkline/get",
                            params={"param": param},
                            headers={"Referer": "https://gu.qq.com/"},
                            timeout=20)
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            errors.append("%s: %s" % (host, exc))
            continue
        if payload.get("msg") == "param error":
            raise DataError("腾讯 K 线参数错误：%s" % param)
        data = payload.get("data") or {}
        node = data.get(symbol)
        if not isinstance(node, dict):
            errors.append("%s: 无 data.%s" % (host, symbol))
            continue
        key = (adjust + period) if adjust else period
        items = node.get(key)
        if items is None and key != period:
            items = node.get(period)
        if not isinstance(items, list):
            errors.append("%s: 无 %s/%s" % (host, key, period))
            continue
        rows = []
        for item in items:
            if len(item) < 6:
                continue
            try:
                rows.append({
                    "date": str(item[0]),
                    "open": float(item[1]),
                    "close": float(item[2]),
                    "high": float(item[3]),
                    "low": float(item[4]),
                    "volume": float(item[5]),
                })
            except (ValueError, TypeError):
                continue
        if rows:
            return rows
        errors.append("%s: 空数据" % host)
    raise DataError("腾讯 K 线不可用：%s" % "; ".join(errors))


def em_kline(code: str, period: str = "day", start: str = "", end: str = "",
             adjust: str = "") -> List[dict]:
    """东方财富 K 线（含北交所）。作为腾讯 K 线的备胎，也是北交所的主源。"""
    secid = "%d.%s" % (1 if get_prefix(code) == "sh" else 0, norm_ticker(code))
    klt = {"day": 101, "week": 102, "month": 103}.get(period)
    fqt = {"": 0, "qfq": 1, "hfq": 2}.get(adjust)
    if klt is None or fqt is None:
        raise ValueError("不支持的 period/adjust 组合")
    params = {
        "secid": secid,
        "klt": str(klt),
        "fqt": str(fqt),
        "beg": start.replace("-", "") if start else "0",
        "end": end.replace("-", "") if end else "20500101",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
    }
    _sleep_between_em()
    try:
        try:
            payload = curl_json("https://push2his.eastmoney.com/api/qt/stock/kline/get",
                                params, referer="https://quote.eastmoney.com/")
        except DataError:
            # 没有 curl 或 curl 失败时退回 requests
            resp = em_get("https://push2his.eastmoney.com/api/qt/stock/kline/get", params,
                          headers={"Referer": "https://quote.eastmoney.com/"}, timeout=20)
            payload = resp.json()
    finally:
        _em_last_call[0] = time.time()
    if payload.get("rc") not in (0, None):
        raise DataError("东财 K 线返回错误：%s" % payload.get("msg"))
    data = payload.get("data") or {}
    klines = data.get("klines") or []
    rows = []
    for line in klines:
        parts = str(line).split(",")
        if len(parts) < 6:
            continue
        try:
            rows.append({
                "date": parts[0],
                "open": float(parts[1]),
                "close": float(parts[2]),
                "high": float(parts[3]),
                "low": float(parts[4]),
                "volume": float(parts[5]),
            })
        except (ValueError, TypeError):
            continue
    if not rows:
        raise DataError("东财 K 线没有返回 %s 的数据" % norm_ticker(code))
    return rows


def price_on_date(code: str, year: int) -> Tuple[Optional[float], str]:
    """取某年最后一个交易日的收盘价（不复权）。腾讯优先，北交所/失败时走东财。"""
    rows = []
    try:
        rows = tencent_kline(code, "day", "%d-01-01" % year, "%d-12-31" % year, 320, "")
    except DataError:
        try:
            rows = em_kline(code, "day", "%d-01-01" % year, "%d-12-31" % year, "")
        except DataError:
            rows = []
    if not rows:
        return None, ""
    # 只保留该年内的数据
    rows = [r for r in rows if r["date"].startswith(str(year))]
    if not rows:
        return None, ""
    return rows[-1]["close"], rows[-1]["date"]


def price_on_or_before(code: str, target_date: str) -> Tuple[Optional[float], str]:
    """取 target_date 当日或之前最近一个交易日的收盘价（不复权）。"""
    try:
        target = date.fromisoformat(str(target_date).replace("/", "-"))
    except ValueError:
        raise ValueError("价格日期格式应为 YYYY-MM-DD：%r" % target_date)
    start = target - timedelta(days=45)
    rows = []
    try:
        rows = tencent_kline(code, "day", start.isoformat(), target.isoformat(), 320, "")
    except DataError:
        try:
            rows = em_kline(code, "day", start.isoformat(), target.isoformat(), "")
        except DataError:
            rows = []
    if not rows:
        # 再放宽到 1 年，覆盖长期停牌前后
        start2 = target - timedelta(days=370)
        try:
            rows = tencent_kline(code, "day", start2.isoformat(), target.isoformat(), 320, "")
        except DataError:
            try:
                rows = em_kline(code, "day", start2.isoformat(), target.isoformat(), "")
            except DataError:
                rows = []
    rows = [r for r in rows if r["date"] <= target.isoformat()]
    if not rows:
        return None, ""
    return rows[-1]["close"], rows[-1]["date"]


# ---------------------------------------------------------------------------
# 财报 / 公司资料 / 分红
# ---------------------------------------------------------------------------

def screen_date(row: dict) -> str:
    return str(row.get("REPORT_DATE", ""))[:10]


def _map_annual(rows: List[dict]) -> Dict[int, dict]:
    out = {}
    for r in rows:
        d = screen_date(r)
        if len(d) >= 4 and d[5:7] == "12":
            out[int(d[:4])] = r
    return out


def fetch_main_annual(code: str) -> Dict[int, dict]:
    rows = em_f10("RPT_F10_FINANCE_MAINFINADATA",
                  '(SECUCODE="%s")(REPORT_TYPE="年报")' % secucode(code), 30,
                  allow_empty=True)
    return _map_annual(rows)


def _fetch_statement_annual(code: str, kind: str, preferred: str) -> Dict[int, dict]:
    """按公司类型选择报表模板：银行 B / 证券 S / 保险 I / 通用 G。

    kind = BALANCE / INCOME / CASHFLOW。首选模板无数据时依次尝试其它模板。
    """
    flt = '(SECUCODE="%s")(REPORT_TYPE="年报")' % secucode(code)
    order = [preferred] + [p for p in ("G", "B", "S", "I") if p != preferred]
    for pfx in order:
        report = "RPT_F10_FINANCE_%s%s" % (pfx, kind)
        try:
            rows = em_f10(report, flt, 30, allow_empty=True)
        except DataError:
            continue
        if rows:
            return _map_annual(rows)
    return {}


def fetch_balance_annual(code: str, prefix: str = "G") -> Dict[int, dict]:
    return _fetch_statement_annual(code, "BALANCE", prefix)


def fetch_income_annual(code: str, prefix: str = "G") -> Dict[int, dict]:
    return _fetch_statement_annual(code, "INCOME", prefix)


def fetch_cashflow_annual(code: str, prefix: str = "G") -> Dict[int, dict]:
    return _fetch_statement_annual(code, "CASHFLOW", prefix)


def fetch_main_quarters(code: str) -> Dict[int, Dict[int, dict]]:
    """近若干期主要指标，按年/季度归档（累计值）。"""
    rows = em_f10("RPT_F10_FINANCE_MAINFINADATA",
                  '(SECUCODE="%s")' % secucode(code), 60)
    out = {}
    for r in rows:
        d = screen_date(r)
        if len(d) < 10:
            continue
        y, m = int(d[:4]), int(d[5:7])
        q = {3: 1, 6: 2, 9: 3, 12: 4}.get(m)
        if not q:
            continue
        out.setdefault(y, {})[q] = r
    return out


def fetch_dividends(code: str) -> Dict[int, dict]:
    rows = em_datacenter("RPT_SHAREBONUS_DET",
                         '(SECURITY_CODE="%s")' % norm_ticker(code),
                         60, "REPORT_DATE", "-1")
    out = {}
    for r in rows:
        d = screen_date(r)
        if len(d) >= 10 and d[5:7] == "12":
            y = int(d[:4])
            # 同一年度可能有多条方案更新，优先“实施分配”，否则保留日期最新的一条
            old = out.get(y)
            if old is None:
                out[y] = r
            else:
                old_ok = old.get("ASSIGN_PROGRESS") == "实施分配"
                new_ok = r.get("ASSIGN_PROGRESS") == "实施分配"
                if new_ok and not old_ok:
                    out[y] = r
    return out


def fetch_company_info(code: str) -> dict:
    """东方财富公司基本资料 —— 仅作为新浪公司资料的兜底。"""
    rows = em_f10("RPT_F10_BASIC_ORGINFO",
                  '(SECUCODE="%s")' % secucode(code), 1, sort_column=None)
    if not rows:
        raise DataError("东方财富没有返回公司基本资料")
    return rows[0]


# ---------------------------------------------------------------------------
# 新浪：财务报表三表 / 公司资料 / 行业（优先数据源）
# ---------------------------------------------------------------------------

SINA_FINANCE_URL = ("https://quotes.sina.cn/cn/api/openapi.php/"
                    "CompanyFinanceService.getFinanceReport2022")
SINA_CORP_INFO_URL = ("https://vip.stock.finance.sina.com.cn/corp/go.php/"
                      "vCI_CorpInfo/stockid/%s.phtml")
SINA_CORP_INDUSTRY_URL = ("https://vip.stock.finance.sina.com.cn/corp/go.php/"
                          "vCI_CorpOtherInfo/stockid/%s/menu_num/2.phtml")


def _strip_tags(value: str) -> str:
    text = re.sub(r"<[^>]+>", "", value or "")
    return html.unescape(text).replace("\xa0", " ").strip()


def _pick(data: dict, keys: List[str]) -> Optional[float]:
    """按顺序取第一个非空数值。"""
    for key in keys:
        if key in data:
            value = as_float(data.get(key))
            if value is not None:
                return value
    return None


def _sina_label(text: str, label: str) -> str:
    """从新浪公司资料表格里按标签取值。"""
    pattern = (r"<td[^>]*>\s*%s\s*[:：]?\s*</td>\s*"
               r"<td[^>]*>(.*?)</td>" % re.escape(label))
    match = re.search(pattern, text, re.S)
    return _strip_tags(match.group(1)) if match else ""


def _sina_html(url: str) -> str:
    resp = http_get(url, headers={"Referer": "https://finance.sina.com.cn/"}, timeout=30)
    raw = resp.content
    for enc in ("gbk", "gb18030", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def sina_company_info(code: str) -> dict:
    """新浪公司资料 + 申万行业。取不到时返回空 dict，由东财兜底。"""
    ticker = norm_ticker(code)
    info = {}
    try:
        text = _sina_html(SINA_CORP_INFO_URL % ticker)
        info["ORG_NAME"] = _sina_label(text, "公司名称")
        info["FOUND_DATE"] = _sina_label(text, "成立日期")
        info["LISTING_DATE"] = _sina_label(text, "上市日期")
        info["ADDRESS"] = _sina_label(text, "注册地址")
        info["OFFICE_ADDRESS"] = _sina_label(text, "办公地址")
        info["MAIN_BUSINESS"] = _sina_label(text, "主营业务")
        info["ORG_PROFILE"] = _sina_label(text, "公司简介")
    except Exception as exc:  # noqa: BLE001
        log("  新浪公司资料获取失败：%s" % exc)
    try:
        text = _sina_html(SINA_CORP_INDUSTRY_URL % ticker)
        match = re.search(r"所属行业板块.*?<td[^>]*>(.*?)</td>", text, re.S)
        if match:
            info["INDUSTRY"] = _strip_tags(match.group(1)).replace("点击查看", "").strip()
    except Exception as exc:  # noqa: BLE001
        log("  新浪行业分类获取失败：%s" % exc)
    return info


def sina_statement_raw(code: str, source: str, num: int = 48) -> Dict[str, dict]:
    """新浪财报三表原始数据。source: fzb(资产负债表) / lrb(利润表) / llb(现金流量表)。

    返回 {报告期(YYYYMMDD): {科目: 值}}，按报告期倒序。num=48 约覆盖 12 年季度。
    """
    symbol = get_prefix(code) + norm_ticker(code)
    params = {
        "paperCode": symbol,
        "source": source,
        "type": "0",
        "page": "1",
        "num": str(num),
    }
    resp = http_get(SINA_FINANCE_URL, params=params,
                    headers={"Referer": "https://finance.sina.com.cn/"}, timeout=45)
    payload = resp.json()
    report_list = (((payload.get("result") or {}).get("data") or {})
                   .get("report_list") or {})
    if not report_list:
        raise DataError("新浪 %s 没有返回 %s 的财报数据" % (source, symbol))
    out = {}
    for period, obj in report_list.items():
        items = {}
        for item in obj.get("data", []) or []:
            title = item.get("item_title")
            if title:
                items[title] = item.get("item_value")
        out[str(period)] = items
    return out


def _sina_annual_map(raw: Dict[str, dict]) -> Dict[int, dict]:
    return {int(p[:4]): items for p, items in raw.items()
            if len(p) == 8 and p.endswith("1231")}


def fetch_sina_financials(code: str) -> Tuple[Dict[int, dict],
                                              Dict[int, Dict[int, dict]],
                                              Dict[str, Dict[str, dict]]]:
    """新浪三表 → 年度扁平指标 + 季度累计指标 + 原始分期数据。

    年度字段：assets / parent_eq / total_liab / money / current_assets /
    noncurrent_assets / current_liab / noncurrent_liab / inventory / fixed_assets /
    goodwill / short_loan / long_loan / shares / revenue / op_cost / op_profit /
    total_profit / net_profit / parent_np / interest_expense / interest_income /
    operate_cf / cce_add。

    原始分期数据用于按报告指标日期计算 TTM 与最新一期净资产。
    """
    fzb_raw = sina_statement_raw(code, "fzb")
    lrb_raw = sina_statement_raw(code, "lrb")
    llb_raw = sina_statement_raw(code, "llb")
    fzb = _sina_annual_map(fzb_raw)
    lrb = _sina_annual_map(lrb_raw)
    llb = _sina_annual_map(llb_raw)

    years = sorted(set(fzb) | set(lrb) | set(llb))
    annual = {}
    for year in years:
        bal = fzb.get(year, {})
        inc = lrb.get(year, {})
        cf = llb.get(year, {})
        op_cost = _pick(inc, ["营业成本"])
        is_financial = op_cost is None   # 银行/保险等没有「营业成本」科目
        annual[year] = {
            "assets": _pick(bal, ["资产总计"]),
            "parent_eq": _pick(bal, ["归属于母公司股东权益合计",
                                     "归属于母公司的股东权益合计",
                                     "归属于母公司所有者权益合计",
                                     "归属于母公司股东的权益",
                                     "归属于母公司股东权益",
                                     "股东权益"]),
            "total_liab": _pick(bal, ["负债合计"]),
            "money": _pick(bal, ["货币资金", "现金及存放中央银行款项",
                                 "存放中央银行款项"]),
            "current_assets": _pick(bal, ["流动资产合计"]),
            "noncurrent_assets": _pick(bal, ["非流动资产合计"]),
            "current_liab": _pick(bal, ["流动负债合计"]),
            "noncurrent_liab": _pick(bal, ["非流动负债合计"]),
            "inventory": _pick(bal, ["存货", "存货净额"]),
            "fixed_assets": _pick(bal, ["固定资产净额", "固定资产合计", "固定资产"]),
            "goodwill": _pick(bal, ["商誉"]),
            "short_loan": _pick(bal, ["短期借款"]),
            "long_loan": _pick(bal, ["长期借款"]),
            "shares": _pick(bal, ["实收资本(或股本)", "股本"]),
            "revenue": _pick(inc, ["营业总收入", "营业收入"]),
            "op_cost": op_cost,
            "op_profit": _pick(inc, ["营业利润"]),
            "total_profit": _pick(inc, ["利润总额"]),
            "net_profit": _pick(inc, ["净利润"]),
            "parent_np": _pick(inc, ["归属于母公司所有者的净利润",
                                     "归属于母公司的净利润"]),
            "deduct_np": None,   # 新浪不提供扣非，后面用东财独有字段补齐
            # 只对非金融企业做 EBIT 的利息调整；银行的「利息支出/收入」是营业收支，
            # 保险的利息收支也不应重复扣减。以「营业成本」是否存在区分通用/金融模板。
            "interest_expense": None if is_financial else _pick(inc, ["利息费用"]),
            "interest_income": None if is_financial else _pick(inc, ["利息收入"]),
            "operate_cf": _pick(cf, ["经营活动产生的现金流量净额"]),
            "cce_add": _pick(cf, ["现金及现金等价物净增加额"]),
        }

    # 季度累计值：营业总收入 / 归母净利润
    quarters = {}
    for period, items in lrb_raw.items():
        if len(period) != 8:
            continue
        year, month = int(period[:4]), period[4:6]
        q = {"03": 1, "06": 2, "09": 3, "12": 4}.get(month)
        if not q:
            continue
        quarters.setdefault(year, {})[q] = {
            "revenue": _pick(items, ["营业总收入", "营业收入"]),
            "parent_np": _pick(items, ["归属于母公司所有者的净利润",
                                       "归属于母公司的净利润"]),
        }
    return annual, quarters, {"fzb": fzb_raw, "lrb": lrb_raw, "llb": llb_raw}


def build_valuation_snapshot(raw: Optional[dict], as_of: str,
                             annual: Optional[Dict[int, dict]] = None) -> dict:
    """按报告指标日期取最新一期股本/归母净资产，并计算 TTM 归母净利润。

    raw 为新浪三表原始分期数据；raw=None 时退回最新年报口径。
    """
    as_of_compact = str(as_of).replace("-", "").strip()
    if not raw:
        latest_year = max(annual) if annual else None
        d = annual.get(latest_year, {}) if annual else {}
        return {
            "fiscal_date": ("%d-12-31" % latest_year) if latest_year else "",
            "shares": d.get("shares"),
            "parent_eq": d.get("parent_eq"),
            "ttm_np": d.get("parent_np"),
        }

    fzb_all = raw.get("fzb") or {}
    lrb_all = raw.get("lrb") or {}
    fzb_periods = sorted(p for p in fzb_all
                         if len(p) == 8 and p.isdigit() and p <= as_of_compact)
    lrb_periods = sorted(p for p in lrb_all
                         if len(p) == 8 and p.isdigit() and p <= as_of_compact)
    latest_b = fzb_periods[-1] if fzb_periods else None
    latest_i = lrb_periods[-1] if lrb_periods else None

    shares = parent_eq = None
    if latest_b:
        bal = fzb_all.get(latest_b, {})
        shares = _pick(bal, ["实收资本(或股本)", "股本"])
        parent_eq = _pick(bal, ["归属于母公司股东权益合计",
                                "归属于母公司的股东权益合计",
                                "归属于母公司所有者权益合计",
                                "归属于母公司股东的权益",
                                "归属于母公司股东权益",
                                "股东权益"])

    ttm_np = None
    if latest_i:
        inc = lrb_all.get(latest_i, {})
        cum_now = _pick(inc, ["归属于母公司所有者的净利润", "归属于母公司的净利润"])
        month = latest_i[4:6]
        if month == "12":
            ttm_np = cum_now
        else:
            year = int(latest_i[:4])
            prev_fy = lrb_all.get("%d1231" % (year - 1), {})
            prev_same = lrb_all.get("%d%s" % (year - 1, latest_i[4:8]), {})
            cum_fy = _pick(prev_fy, ["归属于母公司所有者的净利润", "归属于母公司的净利润"])
            cum_same = _pick(prev_same, ["归属于母公司所有者的净利润", "归属于母公司的净利润"])
            if None not in (cum_now, cum_fy, cum_same):
                ttm_np = cum_fy + cum_now - cum_same
    if ttm_np is None and annual:
        ttm_np = annual.get(max(annual), {}).get("parent_np")

    # fiscal_date 取最新一期资产负债表日期，用于说明 PB 口径
    fiscal_date = ""
    if latest_b:
        fiscal_date = "%s-%s-%s" % (latest_b[:4], latest_b[4:6], latest_b[6:8])
    elif latest_i:
        fiscal_date = "%s-%s-%s" % (latest_i[:4], latest_i[4:6], latest_i[6:8])

    return {
        "fiscal_date": fiscal_date,
        "shares": shares,
        "parent_eq": parent_eq,
        "ttm_np": ttm_np,
    }


def fetch_eastmoney_deduct(code: str) -> Dict[int, float]:
    """东财独有：扣非归母净利润（新浪三表不含该科目）。"""
    out = {}
    try:
        rows = em_f10("RPT_F10_FINANCE_GINCOME",
                      '(SECUCODE="%s")(REPORT_TYPE="年报")' % secucode(code),
                      30, allow_empty=True)
        for row in rows:
            d = screen_date(row)
            value = as_float(row.get("DEDUCT_PARENT_NETPROFIT"))
            if len(d) >= 4 and value is not None:
                out[int(d[:4])] = value
    except DataError:
        pass
    if not out:
        try:
            rows = em_f10("RPT_F10_FINANCE_MAINFINADATA",
                          '(SECUCODE="%s")(REPORT_TYPE="年报")' % secucode(code),
                          30, allow_empty=True)
            for row in rows:
                d = screen_date(row)
                value = as_float(row.get("KCFJCXSYJLR"))
                if len(d) >= 4 and value is not None:
                    out[int(d[:4])] = value
        except DataError:
            pass
    return out


def fetch_eastmoney_cashflow_supplement(code: str) -> Dict[int, dict]:
    """东财独有：补新浪缺失的现金及等价物净增 / 经营净现金流（保险等模板差异）。"""
    flt = '(SECUCODE="%s")(REPORT_TYPE="年报")' % secucode(code)
    for prefix in ("G", "B", "S", "I"):
        report = "RPT_F10_FINANCE_%sCASHFLOW" % prefix
        try:
            rows = em_f10(report, flt, 30, allow_empty=True)
        except DataError:
            continue
        out = {}
        for row in rows:
            d = screen_date(row)
            if len(d) < 4:
                continue
            year = int(d[:4])
            cce = as_float(row.get("CCE_ADD"))
            ocf = as_float(row.get("NETCASH_OPERATE"))
            if cce is not None or ocf is not None:
                out[year] = {"cce_add": cce, "operate_cf": ocf}
        if out:
            return out
    return {}


# ---------------------------------------------------------------------------
# 数值格式化
# ---------------------------------------------------------------------------

def as_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(float(v)) else None
    text = str(v).strip().replace(",", "")
    if text in ("", "-", "--", "None", "null", "nan"):
        return None
    try:
        x = float(text)
        return x if math.isfinite(x) else None
    except ValueError:
        return None


def div(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None or b == 0:
        return None
    return a / b


def fmt_num(v: Optional[float], decimals: int = 0) -> str:
    if v is None:
        return "NA"
    return ("{:,.%df}" % decimals).format(v)


def fmt_pct(v: Optional[float], decimals: int = 2) -> str:
    if v is None:
        return "NA"
    return ("{:.%df}%%" % decimals).format(v)


def fmt_ratio(v: Optional[float], decimals: int = 2) -> str:
    if v is None:
        return "NA"
    return ("{:.%df}" % decimals).format(v)


def fmt_pe(v: Optional[float]) -> str:
    """市盈率：亏损时显示“亏损”，而不是负数。"""
    if v is None:
        return "NA"
    if v <= 0:
        return "亏损"
    return "%.2f" % v


def fmt_pb(v: Optional[float]) -> str:
    """市净率：净资产为负时无经济意义，显示 NA。"""
    if v is None or v <= 0:
        return "NA"
    return "%.2f" % v


def safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip()


def report_window(report_date_text: str) -> Tuple[str, str]:
    """报告日 → (报告日前推 10 年的起始日, 报告日)，ISO 格式。"""
    text = str(report_date_text).strip().replace("/", "-")
    try:
        end = date.fromisoformat(text)
    except ValueError:
        raise ValueError("报告日期格式应为 YYYY/MM/DD 或 YYYY-MM-DD：%r" % report_date_text)
    try:
        start = end.replace(year=end.year - 10)
    except ValueError:      # 2 月 29 日向前推 10 年落到非闰年
        start = end.replace(year=end.year - 10, day=28)
    return start.isoformat(), end.isoformat()


_QUARTER_END = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}


def quarter_visible(year: int, quarter: int, as_of: Optional[str]) -> bool:
    """报告指标日期之前已结束的季度才展示，避免使用未来报告期数据。"""
    if not as_of:
        return True
    text = str(as_of).strip().replace("/", "-")[:10]
    try:
        as_of_date = date.fromisoformat(text)
    except ValueError:
        return True
    month, day = _QUARTER_END.get(quarter, (12, 31))
    try:
        return date(int(year), month, day) <= as_of_date
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# 指标计算
# ---------------------------------------------------------------------------

def build_annual_from_eastmoney(code: str) -> Tuple[Dict[int, dict], Dict[int, Dict[int, dict]]]:
    """东财兜底：把三表 + 主指标整理成与新浪路径相同的扁平结构。"""
    main = fetch_main_annual(code)
    org_type = str(main[max(main)].get("ORG_TYPE") or "") if main else ""
    prefix = {"银行": "B", "证券": "S", "保险": "I"}.get(org_type, "G")
    balance = fetch_balance_annual(code, prefix)
    income = fetch_income_annual(code, prefix)
    cashflow = fetch_cashflow_annual(code, prefix)

    years = sorted(set(main) | set(balance) | set(income) | set(cashflow))
    annual = {}
    for y in years:
        b = balance.get(y, {})
        inc = income.get(y, {})
        cf = cashflow.get(y, {})
        m = main.get(y, {})
        shares = as_float(b.get("SHARE_CAPITAL")) or as_float(m.get("TOTAL_SHARE"))
        parent_eq = as_float(b.get("TOTAL_PARENT_EQUITY"))
        if parent_eq is None:
            bps, sh = as_float(m.get("BPS")), as_float(m.get("TOTAL_SHARE"))
            if bps is not None and sh:
                parent_eq = bps * sh
        annual[y] = {
            "assets": as_float(b.get("TOTAL_ASSETS")) or as_float(m.get("TOTAL_ASSETS_PK")),
            "parent_eq": parent_eq,
            "total_liab": as_float(b.get("TOTAL_LIABILITIES")),
            "money": as_float(b.get("MONETARYFUNDS")) or as_float(b.get("CASH_DEPOSIT_PBC")),
            "current_assets": as_float(b.get("TOTAL_CURRENT_ASSETS")),
            "noncurrent_assets": as_float(b.get("TOTAL_NONCURRENT_ASSETS")),
            "current_liab": as_float(b.get("TOTAL_CURRENT_LIAB")),
            "noncurrent_liab": as_float(b.get("TOTAL_NONCURRENT_LIAB")),
            "inventory": as_float(b.get("INVENTORY")),
            "fixed_assets": as_float(b.get("FIXED_ASSET")),
            "goodwill": as_float(b.get("GOODWILL")),
            "short_loan": as_float(b.get("SHORT_LOAN")),
            "long_loan": as_float(b.get("LONG_LOAN")),
            "shares": shares,
            "revenue": (as_float(inc.get("TOTAL_OPERATE_INCOME"))
                        or as_float(inc.get("OPERATE_INCOME"))
                        or as_float(m.get("TOTALOPERATEREVE"))),
            "op_cost": as_float(inc.get("OPERATE_COST")),
            "op_profit": (as_float(inc.get("OPERATE_PROFIT"))
                          or as_float(m.get("OPERATE_PROFIT_PK"))),
            "total_profit": as_float(inc.get("TOTAL_PROFIT")),
            "net_profit": as_float(inc.get("NETPROFIT")) or as_float(m.get("NETPROFIT")),
            "parent_np": (as_float(inc.get("PARENT_NETPROFIT"))
                          or as_float(m.get("PARENTNETPROFIT"))),
            "deduct_np": (as_float(inc.get("DEDUCT_PARENT_NETPROFIT"))
                          or as_float(m.get("KCFJCXSYJLR"))),
            "interest_expense": as_float(inc.get("FE_INTEREST_EXPENSE")),
            "interest_income": as_float(inc.get("FE_INTEREST_INCOME")),
            "operate_cf": (as_float(cf.get("NETCASH_OPERATE"))
                           or as_float(m.get("NETCASH_OPERATE_PK"))),
            "cce_add": as_float(cf.get("CCE_ADD")),
        }

    quarters = {}
    try:
        qrows = fetch_main_quarters(code)
        for y, qs in qrows.items():
            for q, row in qs.items():
                quarters.setdefault(y, {})[q] = {
                    "revenue": as_float(row.get("TOTALOPERATEREVE")),
                    "parent_np": as_float(row.get("PARENTNETPROFIT")),
                }
    except Exception:  # noqa: BLE001
        pass
    return annual, quarters


def build_metrics(annual: Dict[int, dict], years: List[int]) -> Dict[int, dict]:
    """为每个年度计算模板所需的全部指标。"""
    out = {}
    for y in years:
        d = annual.get(y, {})
        prev = annual.get(y - 1, {})
        assets = d.get("assets")
        parent_eq = d.get("parent_eq")
        total_liab = d.get("total_liab")
        revenue = d.get("revenue")
        op_cost = d.get("op_cost")
        net_profit = d.get("net_profit")
        parent_np = d.get("parent_np")
        deduct_np = d.get("deduct_np")
        op_profit = d.get("op_profit")
        operate_cf = d.get("operate_cf")
        cce_add = d.get("cce_add")
        shares = d.get("shares")
        total_profit = d.get("total_profit")
        interest_expense = d.get("interest_expense") or 0.0
        interest_income = d.get("interest_income") or 0.0

        ebit = None
        if total_profit is not None:
            ebit = total_profit + interest_expense - interest_income
        elif op_profit is not None:
            ebit = op_profit

        prev_assets = prev.get("assets")
        prev_parent_eq = prev.get("parent_eq")
        avg_assets = None
        if assets is not None and prev_assets is not None:
            avg_assets = (assets + prev_assets) / 2.0
        elif assets is not None:
            avg_assets = assets
        avg_parent_eq = None
        if parent_eq is not None and prev_parent_eq is not None:
            avg_parent_eq = (parent_eq + prev_parent_eq) / 2.0
        elif parent_eq is not None:
            avg_parent_eq = parent_eq

        roe = div(parent_np, avg_parent_eq)
        roa = div(ebit, avg_assets)
        debt_ratio = div(total_liab, assets)
        gross_margin = div((revenue - op_cost)
                           if (revenue is not None and op_cost is not None) else None,
                           revenue)
        net_margin = div(net_profit, revenue)
        current_ratio = div(d.get("current_assets"), d.get("current_liab"))
        turnover = div(revenue, avg_assets)

        out[y] = {
            "assets": assets,
            "parent_eq": parent_eq,
            "money": d.get("money"),
            "current_assets": d.get("current_assets"),
            "noncurrent_assets": d.get("noncurrent_assets"),
            "current_liab": d.get("current_liab"),
            "noncurrent_liab": d.get("noncurrent_liab"),
            "inventory": d.get("inventory"),
            "fixed_assets": d.get("fixed_assets"),
            "goodwill": d.get("goodwill"),
            "short_loan": d.get("short_loan"),
            "long_loan": d.get("long_loan"),
            "revenue": revenue,
            "op_profit": op_profit,
            "ebit": ebit,
            "deduct_np": deduct_np,
            "parent_np": parent_np,
            "net_profit": net_profit,
            "operate_cf": operate_cf,
            "cce_add": cce_add,
            "shares": shares,
            "roe": roe * 100 if roe is not None else None,
            "roa": roa * 100 if roa is not None else None,
            "debt_ratio": debt_ratio * 100 if debt_ratio is not None else None,
            "gross_margin": gross_margin * 100 if gross_margin is not None else None,
            "net_margin": net_margin * 100 if net_margin is not None else None,
            "current_ratio": current_ratio,
            "asset_turnover": turnover,
        }
    return out


def cagr(start: Optional[float], end: Optional[float], periods: int) -> Optional[float]:
    if start is None or end is None or start <= 0 or end <= 0 or periods <= 0:
        return None
    try:
        return (end / start) ** (1.0 / periods) - 1.0
    except (ValueError, ZeroDivisionError, OverflowError):
        return None


# ---------------------------------------------------------------------------
# SVG 折线图
# ---------------------------------------------------------------------------

# Okabe-Ito 色盲友好配色：蓝 / 橙 / 紫 / 青绿 / 浅蓝 / 朱红
# 避免红绿同时出现，红绿色盲也能区分。
PALETTE = ["#0072B2", "#E69F00", "#CC79A7", "#009E73", "#56B4E9", "#D55E00"]


def _esc(s: Any) -> str:
    return html.escape(str(s), quote=True)


def _nice_ticks(lo: float, hi: float, target: int = 5) -> Tuple[float, float, float]:
    """把坐标范围扩成 1/2/5 × 10^n 的整齐刻度。"""
    if hi <= lo:
        hi = lo + 1.0
    raw = (hi - lo) / float(target)
    mag = 10.0 ** math.floor(math.log10(abs(raw))) if raw else 1.0
    norm = raw / mag
    if norm <= 1.0:
        step = 1.0 * mag
    elif norm <= 2.0:
        step = 2.0 * mag
    elif norm <= 5.0:
        step = 5.0 * mag
    else:
        step = 10.0 * mag
    start = math.floor(lo / step) * step
    end = math.ceil(hi / step) * step
    return start, end, step


def render_line_svg(path: str, title: str, x_labels: List[str],
                    series: List[Tuple[str, List[Optional[float]]]],
                    y_label: str = "", width: int = 920, height: int = 300,
                    y_fmt: str = "{:.0f}", bottom_margin: int = 58,
                    line_width: float = 2.0) -> None:
    """生成一张简洁的折线图 SVG（无外部依赖）。"""
    has_title = bool(str(title).strip())
    left, right, bottom = 78, 52, bottom_margin
    top = 44 if has_title else 28
    plot_w = width - left - right
    plot_h = height - top - bottom

    all_vals = [v for _, vals in series for v in vals if v is not None]
    if not all_vals:
        all_vals = [0.0, 1.0]
    ymin, ymax = min(all_vals), max(all_vals)
    if ymax == ymin:
        pad = abs(ymax) * 0.1 or 1.0
        ymin, ymax = ymin - pad, ymax + pad
    else:
        pad = (ymax - ymin) * 0.08
        ymin, ymax = ymin - pad, ymax + pad
    if ymin > 0 and ymin < (ymax - ymin) * 0.15:
        ymin = 0.0
    if ymax < 0 and -ymax < (ymax - ymin) * 0.15:
        ymax = 0.0
    ymin, ymax, ystep = _nice_ticks(ymin, ymax, 5)

    n = max(len(x_labels), 1)
    def px(i: int) -> float:
        if n <= 1:
            return left + plot_w / 2.0
        return left + plot_w * i / float(n - 1)

    def py(v: float) -> float:
        if ymax == ymin:
            return top + plot_h / 2.0
        return top + plot_h * (ymax - v) / (ymax - ymin)

    parts = []
    parts.append('<?xml version="1.0" encoding="UTF-8"?>')
    parts.append('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
                 'viewBox="0 0 %d %d" font-family="Noto Sans CJK SC, Microsoft YaHei, '
                 'PingFang SC, Arial, sans-serif">' % (width, height, width, height))
    parts.append('<rect width="100%%" height="100%%" fill="#ffffff"/>')
    if has_title:
        parts.append('<text x="%d" y="26" font-size="16" font-weight="bold" fill="#222">%s</text>'
                     % (left, _esc(title)))

    # 图例：有标题时在标题下方，无标题时贴近顶部
    legend_y = 38 if has_title else 16
    legend_text_y = 43 if has_title else 21
    lx = left
    for idx, (name, _vals) in enumerate(series):
        color = PALETTE[idx % len(PALETTE)]
        parts.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="%s" stroke-width="%g"/>'
                     % (lx, legend_y, lx + 18, legend_y, color, line_width))
        parts.append('<text x="%d" y="%d" font-size="12" font-weight="bold" fill="#444">%s</text>'
                     % (lx + 23, legend_text_y, _esc(name)))
        lx += 23 + 14 * max(1, len(name)) + 22

    # 横向网格与 y 轴刻度
    tick = ymin
    while tick <= ymax + ystep * 1e-9:
        y = py(tick)
        parts.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#e8e8e8" stroke-width="1"/>'
                     % (left, y, left + plot_w, y))
        try:
            label = y_fmt.format(tick)
        except Exception:  # noqa: BLE001
            label = "%.2f" % tick
        parts.append('<text x="%d" y="%.1f" font-size="11" font-weight="bold" fill="#666" text-anchor="end">%s</text>'
                     % (left - 8, y + 4, _esc(label)))
        tick += ystep

    # 零线
    if ymin < 0 < ymax:
        y0 = py(0)
        parts.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#999" '
                     'stroke-width="1" stroke-dasharray="4,3"/>' % (left, y0, left + plot_w, y0))

    # x 轴标签：空字符串表示该点不标注（股价图按年标注）
    for i, lab in enumerate(x_labels):
        if not lab:
            continue
        anchor = "middle"
        if i == 0:
            anchor = "start"
        elif i == n - 1:
            anchor = "end"
        parts.append('<text x="%.1f" y="%d" font-size="11" font-weight="bold" fill="#666" text-anchor="%s">%s</text>'
                     % (px(i), top + plot_h + 20, anchor, _esc(lab)))
    parts.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#bbb" stroke-width="1"/>'
                 % (left, top + plot_h, left + plot_w, top + plot_h))
    parts.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#bbb" stroke-width="1"/>'
                 % (left, top, left, top + plot_h))
    if y_label:
        parts.append('<text x="18" y="%d" font-size="12" font-weight="bold" fill="#666" '
                     'transform="rotate(-90 18 %d)" text-anchor="middle">%s</text>'
                     % (top + plot_h // 2, top + plot_h // 2, _esc(y_label)))

    # 折线
    for idx, (name, vals) in enumerate(series):
        color = PALETTE[idx % len(PALETTE)]
        segment = []
        for i, v in enumerate(vals):
            if v is None:
                if len(segment) >= 2:
                    parts.append('<polyline points="%s" fill="none" stroke="%s" '
                                 'stroke-width="%g" stroke-linejoin="round" stroke-linecap="round"/>'
                                 % (" ".join(segment), color, line_width))
                segment = []
                continue
            segment.append("%.1f,%.1f" % (px(i), py(v)))
        if len(segment) >= 2:
            parts.append('<polyline points="%s" fill="none" stroke="%s" '
                         'stroke-width="%g" stroke-linejoin="round" stroke-linecap="round"/>'
                         % (" ".join(segment), color, line_width))
        # 数据点（点数少时才画）
        if n <= 16:
            for i, v in enumerate(vals):
                if v is not None:
                    parts.append('<circle cx="%.1f" cy="%.1f" r="2.4" fill="%s"/>'
                                 % (px(i), py(v), color))

    parts.append('</svg>')
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(parts))


# ---------------------------------------------------------------------------
# Markdown 渲染
# ---------------------------------------------------------------------------

def md_table(headers: List[str], rows: List[List[str]],
             aligns: Optional[List[str]] = None) -> str:
    if aligns is None:
        aligns = [":--"] + ["--:"] * (len(headers) - 1)
    lines = ["| " + " | ".join(headers) + " |",
             "| " + " | ".join(aligns) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def annual_table(metrics: Dict[int, dict], years: List[int], fields: List[Tuple[str, str, str]]) -> str:
    """fields: [(key, label, fmt)]，fmt 为 pct/num/ratio。"""
    headers = ["指标"] + [str(y) for y in years]
    rows = []
    for key, label, fmt in fields:
        vals = []
        for y in years:
            v = metrics.get(y, {}).get(key)
            if fmt == "pct":
                vals.append(fmt_pct(v))
            elif fmt == "num":
                # 百万元
                vals.append(fmt_num(v / 1e6 if v is not None else None, 0))
            elif fmt == "shares":
                vals.append(fmt_num(v / 1e6 if v is not None else None, 0))
            else:
                vals.append(fmt_ratio(v))
        rows.append([label] + vals)
    return md_table(headers, rows)


def company_display_fields(info: dict, name: str) -> List[Tuple[str, str]]:
    """公司概况的 6 个展示字段（Markdown 与 LaTeX 共用）。"""
    city = info.get("CITY") or ""
    if not city:
        addr = str(info.get("ADDRESS") or info.get("REG_ADDRESS") or "")
        match = (re.search(r"(?:省|自治区|特别行政区)([一-龥]{2,6}市)", addr)
                 or re.search(r"([一-龥]{2,6}市)", addr))
        city = match.group(1) if match else str(info.get("PROVINCE") or "")
    industry = str(info.get("INDUSTRY") or "")
    if not industry:
        for key in ("EM2016", "BOARD_NAME_LEVEL"):
            if info.get(key):
                industry = str(info[key]).split("-")[-1]
                break
    main_business = str(info.get("MAIN_BUSINESS") or "").strip()
    if not main_business:
        main_business = str(info.get("BUSINESS_SCOPE") or "").strip()
    main_business = re.sub(r"\s+", " ", main_business)
    if len(main_business) > 220:
        main_business = main_business[:217] + "..."

    def d8(value: Any) -> str:
        text = str(value or "")[:10]
        return text.replace("-", "") if text else "NA"

    return [
        ("公司全称", str(info.get("ORG_NAME") or name)),
        ("所在城市", city or "NA"),
        ("成立日期", d8(info.get("FOUND_DATE"))),
        ("上市日期", d8(info.get("LISTING_DATE"))),
        ("所属行业", industry or "NA"),
        ("主营业务", main_business or "NA"),
    ]


def build_markdown(code: str, info: dict, quote: dict, metrics: Dict[int, dict],
                   years: List[int], latest_year: int,
                   valuation: dict, quotes_quarters: Dict[int, Dict[int, dict]],
                   dividends: Dict[int, dict], report_date: str,
                   charts: Dict[str, str], with_charts: bool,
                   price_window: str = "", price_adjust: str = "前复权") -> str:
    name = info.get("SECURITY_NAME_ABBR") or quote.get("name") or norm_ticker(code)
    full = full_code(code)

    md = []
    md.append("# %s（%s）CM 手册" % (name, full))
    md.append("")
    md.append("> 制表日期：%s　|　报告指标日期：%s（报告日最近交易日）　|　财务指标截至：%d 年报"
              % (report_date, valuation.get("price_date") or "NA", latest_year))
    md.append("")
    md.append(md_table(
        ["报告指标日期", "当前股价（元）", "当前市值（亿元）", "静态市盈率",
         "静态市净率", "动态市盈率", "市净率"],
        [[valuation.get("price_date") or "NA",
          fmt_ratio(valuation.get("price")),
          fmt_num(valuation.get("mcap_yi"), 0),
          fmt_pe(valuation.get("static_pe")),
          fmt_pb(valuation.get("static_pb")),
          fmt_pe(valuation.get("dynamic_pe")),
          fmt_pb(valuation.get("current_pb"))]],
        [":--", "--:", "--:", "--:", "--:", "--:", "--:"]))
    md.append("")
    snap_date = valuation.get("snapshot_date") or ("%d-12-31" % latest_year)
    md.append("> 静态 PE/PB 采用 %d 年报归母净利润/归母股东权益；动态 PE 采用 TTM 归母净利润；"
              "市净率采用 %s 归母股东权益。当前股价/市值取报告指标日期当日收盘价与最新披露股本。"
              % (latest_year, snap_date))
    md.append("")

    # ---- 1. 盈利能力与偿债能力 ----
    md.append("## 一、盈利能力与偿债能力")
    md.append("")
    fields1 = [
        ("roe", "净资产收益率", "pct"),
        ("roa", "总资产收益率", "pct"),
        ("debt_ratio", "资产负债率", "pct"),
        ("gross_margin", "销售毛利率", "pct"),
        ("net_margin", "销售净利率", "pct"),
        ("current_ratio", "流动比率", "ratio"),
        ("asset_turnover", "总资产周转率", "ratio"),
    ]
    md.append(annual_table(metrics, years, fields1))
    md.append("")
    if with_charts and charts.get("profitability"):
        md.append("![盈利能力趋势](%s)" % charts["profitability"])
        md.append("")

    # ---- 2. 资产负债结构 ----
    md.append("## 二、资产负债结构（百万元）")
    md.append("")
    fields2 = [
        ("assets", "资产总计（百万元）", "num"),
        ("parent_eq", "股东权益（百万元）", "num"),
        ("money", "货币资金（百万元）", "num"),
        ("current_assets", "流动资产（百万元）", "num"),
        ("noncurrent_assets", "非流动资产（百万元）", "num"),
        ("current_liab", "流动负债（百万元）", "num"),
        ("noncurrent_liab", "非流动负债（百万元）", "num"),
        ("inventory", "存货（百万元）", "num"),
        ("fixed_assets", "固定资产（百万元）", "num"),
        ("goodwill", "商誉（百万元）", "num"),
        ("short_loan", "短期贷款（百万元）", "num"),
        ("long_loan", "长期贷款（百万元）", "num"),
    ]
    md.append(annual_table(metrics, years, fields2))
    md.append("")
    if with_charts and charts.get("assets"):
        md.append("![资产规模趋势](%s)" % charts["assets"])
        md.append("")

    # ---- 3. 利润与现金流 ----
    md.append("## 三、利润与现金流（百万元）")
    md.append("")
    fields3 = [
        ("revenue", "主营业务收入（百万元）", "num"),
        ("op_profit", "营业利润（百万元）", "num"),
        ("ebit", "息税前利润（百万元）", "num"),
        ("deduct_np", "扣非净利润（百万元）", "num"),
        ("parent_np", "归母净利润（百万元）", "num"),
        ("operate_cf", "经营净现金流（百万元）", "num"),
        ("cce_add", "现金及等价物净增（百万元）", "num"),
    ]
    md.append(annual_table(metrics, years, fields3))
    md.append("")
    if with_charts and charts.get("cashflow"):
        md.append("![利润与现金流趋势](%s)" % charts["cashflow"])
        md.append("")

    # ---- 4. 股本 ----
    md.append("## 四、股本")
    md.append("")
    md.append(annual_table(metrics, years, [("shares", "总股本（百万股）", "shares")]))
    md.append("")

    # ---- 5. 股价走势 ----
    md.append("## 五、股价走势")
    md.append("")
    if with_charts and charts.get("price"):
        md.append("![股价走势](%s)" % charts["price"])
        md.append("")
        if price_adjust == "前复权":
            md.append("> 股价图为月线、前复权收盘价；区间为报告日前推 10 年%s。"
                      "前复权价格已按分红送转调整。"
                      % ("（%s）" % price_window if price_window else ""))
        else:
            md.append("> 股价图为月线、不复权收盘价；区间为报告日前推 10 年%s。"
                      "前复权数据不可用，图中为实际成交价。"
                      % ("（%s）" % price_window if price_window else ""))
        md.append("")

    # ---- 6. 公司概况 ----
    md.append("## 六、公司概况")
    md.append("")
    md.append(md_table(
        ["项目", "内容"],
        [[label, value] for label, value in company_display_fields(info, name)],
        [":--", ":--"]))
    md.append("")

    # ---- 7. 年均增长率 ----
    md.append("## 七、年均增长率")
    md.append("")
    cagr_rows = []
    for label, key in (("主营营收", "revenue"), ("净利润", "parent_np"),
                       ("总资产", "assets"), ("股东权益", "parent_eq")):
        row = [label]
        for span in (10, 8, 5, 3):
            start_y = latest_year - span
            g = cagr(metrics.get(start_y, {}).get(key),
                     metrics.get(latest_year, {}).get(key), span)
            row.append(fmt_pct(g * 100 if g is not None else None))
        cagr_rows.append(row)
    md.append(md_table(["时间段", "10年", "8年", "5年", "3年"], cagr_rows))
    md.append("")
    md.append("> 年均增长率为复合增长率（CAGR），区间终点为 %d 年；净利润采用归母净利润。" % latest_year)
    md.append("")

    # ---- 8. 季度数据 ----
    # 在最近 4 个完整年度之外，补上报告指标日期所在年份（只展示已结束季度）
    q_asof = valuation.get("price_date") or report_date
    try:
        report_year = int(str(q_asof)[:4])
    except (ValueError, TypeError):
        report_year = latest_year
    q_years = list(range(latest_year, latest_year - 4, -1))
    if report_year > latest_year:
        q_years = [report_year] + q_years

    md.append("## 八、季度营收（百万元，累计值）")
    md.append("")
    q_rows = []
    for y in q_years:
        row = [str(y)]
        for q in (1, 2, 3, 4):
            if not quarter_visible(y, q, q_asof):
                row.append("NA")
                continue
            r = quotes_quarters.get(y, {}).get(q, {})
            v = as_float(r.get("revenue"))
            row.append(fmt_num(v / 1e6 if v is not None else None, 0))
        q_rows.append(row)
    md.append(md_table(["年度", "一季度", "二季度", "三季度", "四季度"], q_rows))
    md.append("")

    md.append("## 九、季度净利润（百万元，累计值）")
    md.append("")
    q_rows = []
    for y in q_years:
        row = [str(y)]
        for q in (1, 2, 3, 4):
            if not quarter_visible(y, q, q_asof):
                row.append("NA")
                continue
            r = quotes_quarters.get(y, {}).get(q, {})
            v = as_float(r.get("parent_np"))
            row.append(fmt_num(v / 1e6 if v is not None else None, 0))
        q_rows.append(row)
    md.append(md_table(["年度", "一季度", "二季度", "三季度", "四季度"], q_rows))
    md.append("")

    # ---- 10. 每股分红 ----
    md.append("## 十、每股分红（元）")
    md.append("")
    dividend_years = list(range(latest_year, latest_year - 4, -1))
    d_rows = []
    for y in dividend_years:
        r = dividends.get(y, {})
        cash10 = as_float(r.get("PRETAX_BONUS_RMB"))
        bonus10 = as_float(r.get("BONUS_RATIO"))
        transfer10 = as_float(r.get("IT_RATIO"))
        cash = cash10 / 10.0 if cash10 is not None else None
        bonus = bonus10 / 10.0 if bonus10 is not None else None
        transfer = transfer10 / 10.0 if transfer10 is not None else None
        d_rows.append([
            "%d1231" % y,
            fmt_ratio(cash),
            fmt_ratio(bonus) if bonus not in (None, 0) else "NA",
            fmt_ratio(transfer) if transfer not in (None, 0) else "NA",
        ])
    md.append(md_table(["年度", "现金", "送股", "转增"], d_rows))
    md.append("")

    # ---- 脚注 ----
    md.append("---")
    md.append("")
    md.append("**口径与免责声明**")
    md.append("")
    md.append("- 报告指标日期为 %s（报告日最近交易日）；当前股价/市值取该日收盘价与最新披露股本。"
              % (valuation.get("price_date") or "NA"))
    md.append("- 静态 PE/PB 按 %d 年报计算；动态 PE 按 TTM 归母净利润计算；"
              "市净率按 %s 归母股东权益计算。"
              % (latest_year, valuation.get("snapshot_date") or ("%d-12-31" % latest_year)))
    md.append("- 财务三表取自新浪财经按最新披露口径整理的报表，东方财富仅补充扣非净利润、"
              "分红报告期等独有字段；历史年度可能包含追溯调整，与当年原始年报披露值可能存在差异。")
    md.append("- 数据来源：腾讯财经（行情、K 线）；新浪财经（财务报表三表、公司资料、行业）；"
              "东方财富（扣非净利润、分红报告期、北交所 K 线兜底）。")
    md.append("- 银行、证券、保险等金融企业的报表科目与通用模板不同，"
              "流动比率、存货、短期/长期贷款等不适用科目显示 NA。")
    md.append("- 本资料所有数据均来源于可靠的公开资料，但无法确保完全准确。如有错讹，概不负责。")
    md.append("- 本资料仅适用于个人投资者训练投资能力，未获授权，禁止一切形式的复制、转载和交易。")
    md.append("")
    return "\n".join(md)


# ---------------------------------------------------------------------------
# LaTeX 一页 PDF
# ---------------------------------------------------------------------------

def _tex_escape(value: Any) -> str:
    """把数据字符串转义成可放进 LaTeX 的文本（不用于已经是 LaTeX 命令的片段）。"""
    text = str(value)
    replacements = [
        ("\\", r"\textbackslash{}"),
        ("&", r"\&"),
        ("%", r"\%"),
        ("$", r"\$"),
        ("#", r"\#"),
        ("_", r"\_"),
        ("{", r"\{"),
        ("}", r"\}"),
        ("~", r"\textasciitilde{}"),
        ("^", r"\textasciicircum{}"),
    ]
    for old, new in replacements:
        text = text.replace(old, new)
    return text


def _fit_width(table_tex: str) -> str:
    """把右侧表格统一缩放到栏宽，保证所有表格宽度一致。"""
    return "\\resizebox{\\righttablewidth}{!}{%\n" + table_tex + "\n}"


def _latex_tabular(colspec: str, headers: List[str], rows: List[List[str]],
                   font_size: float = 6.0, line_height: float = 7.0,
                   tabcolsep: Optional[str] = None) -> str:
    lines = ["{\\fontsize{%g}{%g}\\selectfont" % (font_size, line_height)]
    if tabcolsep:
        lines.append("\\setlength{\\tabcolsep}{%s}" % tabcolsep)
    lines += ["\\begin{tabular}{%s}" % colspec,
             "\\hline",
             "\\rowcolor{headbg}" + " & ".join(headers) + " \\\\",
             "\\hline"]
    for row in rows:
        lines.append(" & ".join(row) + " \\\\")
        lines.append("\\hline")
    lines.append("\\end{tabular}}")
    return "\n".join(lines)


def _latex_annual_table(metrics: Dict[int, dict], years: List[int],
                        fields: List[Tuple[str, str, str]],
                        unit_divisor: float = 1e6, col_width: float = 8.5,
                        target_width_mm: float = 130.0) -> str:
    headers = ["\\textbf{指标}"] + ["\\textbf{%d}" % y for y in years]
    rows = []
    for key, label, fmt in fields:
        row = [_tex_escape(label)]
        for year in years:
            value = metrics.get(year, {}).get(key)
            if fmt == "pct":
                row.append(_tex_escape(fmt_pct(value)))
            elif fmt == "num":
                row.append(_tex_escape(fmt_num(value / unit_divisor if value is not None else None, 0)))
            elif fmt == "shares":
                row.append(_tex_escape(fmt_num(value / 1e6 if value is not None else None, 0)))
            else:
                row.append(_tex_escape(fmt_ratio(value)))
        rows.append(row)
    # 年份列固定 10mm，整表统一缩放到 target_width_mm：
    # 既避免大数字溢出到右栏，也保证三张表宽度完全一致。
    colspec = ("|>{\\raggedright\\arraybackslash}p{21mm}|"
               + "*{%d}{>{\\raggedleft\\arraybackslash}p{10mm}|}" % len(years))
    table = _latex_tabular(colspec, headers, rows, 6.0, 7.0, tabcolsep="1pt")
    return (("\\resizebox{%.1fmm}{!}{" % target_width_mm)
            + "\n" + table + "\n}")


def _latex_company_table(info: dict, name: str) -> str:
    lines = ["{\\fontsize{6.3}{7.6}\\selectfont",
             "\\begin{tabularx}{\\righttablewidth}{|>{\\raggedright\\arraybackslash}p{15mm}|"
             ">{\\raggedright\\arraybackslash}X|}",
             "\\hline"]
    for label, value in company_display_fields(info, name):
        lines.append("%s & %s \\\\" % (_tex_escape(label), _tex_escape(value)))
        lines.append("\\hline")
    lines.append("\\end{tabularx}}")
    return "\n".join(lines)


def _latex_cagr_table(metrics: Dict[int, dict], latest_year: int) -> str:
    headers = ["\\textbf{时间段}", "\\textbf{10年}", "\\textbf{8年}",
               "\\textbf{5年}", "\\textbf{3年}"]
    rows = []
    for label, key in (("主营营收", "revenue"), ("净利润", "parent_np"),
                       ("总资产", "assets"), ("股东权益", "parent_eq")):
        row = [_tex_escape(label)]
        for span in (10, 8, 5, 3):
            growth = cagr(metrics.get(latest_year - span, {}).get(key),
                          metrics.get(latest_year, {}).get(key), span)
            row.append(_tex_escape(fmt_pct(growth * 100 if growth is not None else None)))
        rows.append(row)
    colspec = ("|>{\\raggedright\\arraybackslash}p{16mm}|"
               "*{4}{>{\\raggedleft\\arraybackslash}p{11mm}|}")
    return _latex_tabular(colspec, headers, rows, 6.3, 7.3)


def _latex_quarter_table(quarters: Dict[int, Dict[int, dict]], q_years: List[int],
                         key: str, as_of: Optional[str] = None) -> str:
    headers = ["\\textbf{年度}", "\\textbf{一季度}", "\\textbf{二季度}",
               "\\textbf{三季度}", "\\textbf{四季度}"]
    rows = []
    for year in q_years:
        row = [str(year)]
        for quarter in (1, 2, 3, 4):
            if not quarter_visible(year, quarter, as_of):
                row.append("NA")
                continue
            data = quarters.get(year, {}).get(quarter, {})
            value = as_float(data.get(key))
            row.append(_tex_escape(fmt_num(value / 1e6 if value is not None else None, 0)))
        rows.append(row)
    colspec = ("|>{\\centering\\arraybackslash}p{9mm}|"
               "*{4}{>{\\raggedleft\\arraybackslash}p{12.5mm}|}")
    return _latex_tabular(colspec, headers, rows, 6.0, 7.0)


def _latex_dividend_table(dividends: Dict[int, dict], q_years: List[int]) -> str:
    headers = ["\\textbf{年度}", "\\textbf{现金}", "\\textbf{送股}", "\\textbf{转增}"]
    rows = []
    for year in q_years:
        data = dividends.get(year, {})
        cash10 = as_float(data.get("PRETAX_BONUS_RMB"))
        bonus10 = as_float(data.get("BONUS_RATIO"))
        transfer10 = as_float(data.get("IT_RATIO"))
        cash = cash10 / 10.0 if cash10 is not None else None
        bonus = bonus10 / 10.0 if bonus10 is not None else None
        transfer = transfer10 / 10.0 if transfer10 is not None else None
        rows.append([
            "%d1231" % year,
            _tex_escape(fmt_ratio(cash)),
            _tex_escape(fmt_ratio(bonus) if bonus not in (None, 0) else "NA"),
            _tex_escape(fmt_ratio(transfer) if transfer not in (None, 0) else "NA"),
        ])
    colspec = ("|>{\\centering\\arraybackslash}p{17mm}|"
               "*{3}{>{\\raggedleft\\arraybackslash}p{13mm}|}")
    return _latex_tabular(colspec, headers, rows, 6.3, 7.3)


def crop_pdf_to_content(pdf_path: str, margin_mm: float = 6.0) -> str:
    """把固定 A4 页面裁成内容实际高度 + 边距，保持约 A4 宽度。"""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        log("  未安装 PyMuPDF，跳过页面自适应裁剪")
        return pdf_path
    try:
        src = fitz.open(pdf_path)
        if src.page_count != 1:
            src.close()
            return pdf_path
        page = src[0]
        # 72dpi 渲染后扫描非白色像素，得到内容包围盒
        pix = page.get_pixmap(dpi=72, alpha=False)
        w, h, n = pix.width, pix.height, pix.n
        samples = pix.samples
        min_x, min_y, max_x, max_y = w, h, -1, -1
        for y in range(h):
            base = y * w * n
            for x in range(w):
                i = base + x * n
                if samples[i] < 250 or samples[i + 1] < 250 or samples[i + 2] < 250:
                    if x < min_x:
                        min_x = x
                    if x > max_x:
                        max_x = x
                    if y < min_y:
                        min_y = y
                    if y > max_y:
                        max_y = y
        if max_x <= min_x or max_y <= min_y:
            src.close()
            return pdf_path
        pw, ph = page.rect.width, page.rect.height
        sx, sy = pw / float(w), ph / float(h)
        margin = margin_mm * 72.0 / 25.4
        rect = fitz.Rect(
            max(0.0, min_x * sx - margin),
            max(0.0, min_y * sy - margin),
            min(pw, (max_x + 1) * sx + margin),
            min(ph, (max_y + 1) * sy + margin),
        )
        new_doc = fitz.open()
        new_page = new_doc.new_page(width=rect.width, height=rect.height)
        new_page.show_pdf_page(new_page.rect, src, 0, clip=rect)
        tmp_path = pdf_path + ".tmp.pdf"
        new_doc.save(tmp_path, garbage=4, deflate=True)
        new_doc.close()
        src.close()
        os.replace(tmp_path, pdf_path)
        return pdf_path
    except Exception as exc:  # noqa: BLE001
        log("  PDF 页面自适应裁剪失败（保留 A4 页面）：%s" % exc)
        return pdf_path


def _svg_to_pdf(svg_path: str, timeout: int = 60) -> str:
    pdf_path = os.path.splitext(svg_path)[0] + ".pdf"
    try:
        result = subprocess.run(
            ["rsvg-convert", "-f", "pdf", "-o", pdf_path, svg_path],
            capture_output=True, timeout=timeout)
    except FileNotFoundError:
        raise DataError("未找到 rsvg-convert；请先安装 librsvg（macOS: brew install librsvg）")
    except subprocess.SubprocessError as exc:
        raise DataError("rsvg-convert 执行失败：%s" % exc)
    if result.returncode != 0:
        raise DataError("rsvg-convert 转换失败：%s"
                        % result.stderr.decode("utf-8", "replace")[:300])
    return pdf_path


def render_latex_pdf(tex_path: str, name: str, full: str, info: dict,
                     valuation: dict, metrics: Dict[int, dict], years: List[int],
                     latest_year: int, quarters: Dict[int, Dict[int, dict]],
                     dividends: Dict[int, dict], report_date: str,
                     charts: Dict[str, str], price_window: str,
                     price_adjust: str) -> str:
    """生成一页 LaTeX PDF。Markdown 由调用方先落盘，这里只负责 TeX/PDF。"""
    out_dir = os.path.dirname(os.path.abspath(tex_path))
    os.makedirs(out_dir, exist_ok=True)

    # 图表：优先用 PDF 专用尺寸的 print 版；没有则回退到网页版 SVG
    chart_pdfs = {}
    for key in ("profitability_print", "assets_print", "cashflow_print", "price_print"):
        svg_path = charts.get(key) or charts.get(key.replace("_print", ""))
        if not svg_path or not os.path.exists(svg_path):
            continue
        pdf_path = _svg_to_pdf(svg_path)
        chart_pdfs[key] = os.path.relpath(pdf_path, out_dir).replace(os.sep, "/")

    # 左侧三张表
    fields1 = [
        ("roe", "净资产收益率", "pct"),
        ("roa", "总资产收益率", "pct"),
        ("debt_ratio", "资产负债率", "pct"),
        ("gross_margin", "销售毛利率", "pct"),
        ("net_margin", "销售净利率", "pct"),
        ("current_ratio", "流动比率", "ratio"),
        ("asset_turnover", "总资产周转率", "ratio"),
    ]
    fields2 = [
        ("assets", "资产总计", "num"),
        ("parent_eq", "股东权益", "num"),
        ("money", "货币资金", "num"),
        ("current_assets", "流动资产", "num"),
        ("noncurrent_assets", "非流动资产", "num"),
        ("current_liab", "流动负债", "num"),
        ("noncurrent_liab", "非流动负债", "num"),
        ("inventory", "存货", "num"),
        ("fixed_assets", "固定资产", "num"),
        ("goodwill", "商誉", "num"),
        ("short_loan", "短期贷款", "num"),
        ("long_loan", "长期贷款", "num"),
    ]
    fields3 = [
        ("revenue", "主营业务收入", "num"),
        ("op_profit", "营业利润", "num"),
        ("ebit", "息税前利润", "num"),
        ("deduct_np", "扣非净利润", "num"),
        ("parent_np", "归母净利润", "num"),
        ("operate_cf", "经营净现金流", "num"),
        ("cce_add", "现金及等价物净增", "num"),
        ("shares", "总股本（百万股）", "shares"),
    ]
    dividend_years = list(range(latest_year, latest_year - 4, -1))
    q_asof = valuation.get("price_date") or report_date
    try:
        report_year = int(str(q_asof)[:4])
    except (ValueError, TypeError):
        report_year = latest_year
    q_years = list(dividend_years)
    if report_year > latest_year:
        q_years = [report_year] + q_years

    # 金额单位自适应：特大公司（如中国平安）用亿元，普通公司沿用模板的百万元
    max_num = 0.0
    for key, _label, fmt in fields2 + fields3:
        if fmt != "num":
            continue
        for year in years:
            value = metrics.get(year, {}).get(key)
            if value is not None:
                max_num = max(max_num, abs(value))
    if max_num >= 1e12:
        unit_divisor, unit_label, col_width = 1e8, "亿元", 9.0
    else:
        unit_divisor, unit_label, col_width = 1e6, "百万元", 8.5

    def include_chart(key: str) -> str:
        path = chart_pdfs.get(key)
        if not path:
            return ""
        # 左栏宽度为年度表宽度的 105%，图表占满左栏
        return ("\\vspace{0mm}\\includegraphics[width=\\linewidth]{%s}\\par\\vspace{0mm}"
                % path)

    # 顶部摘要
    top_headers = ["\\textbf{报告指标日期}", "\\textbf{当前股价（元）}",
                   "\\textbf{当前市值（亿元）}", "\\textbf{静态PE}",
                   "\\textbf{静态PB}", "\\textbf{动态PE}", "\\textbf{市净率}"]
    top_values = [
        _tex_escape(valuation.get("price_date") or "NA"),
        _tex_escape(fmt_ratio(valuation.get("price"))),
        _tex_escape(fmt_num(valuation.get("mcap_yi"), 0)),
        _tex_escape(fmt_pe(valuation.get("static_pe"))),
        _tex_escape(fmt_pb(valuation.get("static_pb"))),
        _tex_escape(fmt_pe(valuation.get("dynamic_pe"))),
        _tex_escape(fmt_pb(valuation.get("current_pb"))),
    ]
    name_tex = _tex_escape(name)
    full_tex = _tex_escape(full)

    snap_date = valuation.get("snapshot_date") or ("%d-12-31" % latest_year)
    footer_mid = _tex_escape(
        "报告指标日期为 %s（报告日最近交易日）；当前股价/市值取该日收盘价与最新披露股本。"
        "静态 PE/PB 按 %d 年报计算；动态 PE 按 TTM 归母净利润计算；"
        "市净率按 %s 归母股东权益计算。"
        % (valuation.get("price_date") or "NA", latest_year, snap_date))
    footer_right = _tex_escape(
        "数据来源：腾讯财经（行情、K 线）；新浪财经（财报三表、公司资料、行业）；"
        "东方财富（扣非净利润、分红报告期、北交所 K 线兜底）。"
        "本资料所有数据均来源于可靠的公开资料，但无法确保完全准确。如有错讹，概不负责。"
        "本资料仅适用于个人投资者训练投资能力，未获授权，禁止一切形式的复制、转载和交易。")

    cjk_font = os.environ.get("CM_CJK_FONT", "PingFang SC").strip() or "PingFang SC"
    log("  PDF 中文字体：%s" % cjk_font)
    preamble = r"""\documentclass[fontset=none,10pt]{ctexart}
\usepackage[a4paper,margin=6mm]{geometry}
\usepackage[table]{xcolor}
\usepackage{graphicx,tabularx,array,multirow,makecell}
\setCJKmainfont{__CM_CJK_FONT__}
\setCJKsansfont{__CM_CJK_FONT__}
\setCJKmonofont{__CM_CJK_FONT__}
\pagestyle{empty}
\setlength{\parindent}{0pt}
\setlength{\tabcolsep}{0.5pt}
\renewcommand{\arraystretch}{1.0}
\definecolor{headbg}{RGB}{238,238,238}
\newsavebox{\cmtable}
\newlength{\cmleftwidth}
\newlength{\righttablewidth}
\begin{document}
""".replace("__CM_CJK_FONT__", cjk_font)
    tex = [preamble]
    # 顶部摘要栏：单行双行单元格，压缩高度
    tex.append(r"\noindent{\fontsize{6.2}{7.2}\selectfont\renewcommand{\arraystretch}{1.0}")
    tex.append(r"\begin{tabularx}{\textwidth}{|>{\centering\arraybackslash}p{34mm}|"
               r"*{7}{>{\centering\arraybackslash}X|}}")
    tex.append(r"\hline")
    top_cells = [r"\makecell{\textbf{%s}\\\textbf{%s}}" % (full_tex, name_tex)]
    for head, value in zip(top_headers, top_values):
        top_cells.append(r"\makecell{%s\\%s}" % (head, value))
    tex.append(" & ".join(top_cells) + r" \\")
    tex.append(r"\hline")
    tex.append(r"\end{tabularx}}")
    tex.append(r"\vspace{0.8mm}")

    # 左栏：先量出第一张表的精确宽度，再据此设定左栏宽度和右栏起点
    tex.append(r"\sbox{\cmtable}{"
               + _latex_annual_table(metrics, years, fields1, unit_divisor, col_width)
               + "}")
    tex.append(r"\setlength{\cmleftwidth}{\wd\cmtable}")
    tex.append(r"\noindent\begin{minipage}[t]{\dimexpr1.05\cmleftwidth\relax}\vspace{0pt}")
    tex.append(r"\makebox[\linewidth][c]{\usebox{\cmtable}}")
    tex.append(include_chart("profitability_print"))
    tex.append(r"\sbox{\cmtable}{"
               + _latex_annual_table(metrics, years, fields2, unit_divisor, col_width)
               + "}")
    tex.append(r"\makebox[\linewidth][c]{\usebox{\cmtable}}")
    tex.append(include_chart("assets_print"))
    tex.append(r"\sbox{\cmtable}{"
               + _latex_annual_table(metrics, years, fields3, unit_divisor, col_width)
               + "}")
    tex.append(r"\makebox[\linewidth][c]{\usebox{\cmtable}}")
    tex.append(include_chart("cashflow_print"))
    tex.append(r"\end{minipage}%")
    tex.append(r"\hspace{0.2mm}%")

    # 右栏
    tex.append(r"\begin{minipage}[t]{\dimexpr\textwidth-1.05\cmleftwidth-0.2mm\relax}"
               r"\vspace{0pt}\setlength{\righttablewidth}{\linewidth}")
    if chart_pdfs.get("price_print"):
        tex.append(r"\includegraphics[width=\linewidth]{%s}\par" % chart_pdfs["price_print"])
        tex.append(r"\vspace{-1.4mm}{\centering\fontsize{5.5}{6.5}\selectfont "
                   r"股价走势（月线，%s）\\[0.1mm] %s\par}"
                   % (_tex_escape(price_adjust), _tex_escape(price_window)))
        tex.append(r"\vspace{0.2mm}")
    tex.append(r"\vspace{0.4mm}{\centering\textbf{\fontsize{10}{12}\selectfont %s}\par}"
               % name_tex)
    tex.append(_latex_company_table(info, name))
    tex.append(r"\vspace{0.7mm}\textbf{\fontsize{6.5}{7.5}\selectfont 年均增长率}\par\vspace{0.2mm}")
    tex.append(_fit_width(_latex_cagr_table(metrics, latest_year)))
    tex.append(r"\vspace{0.7mm}\textbf{\fontsize{6.5}{7.5}\selectfont 季度营收（百万元，累计值）}\par\vspace{0.2mm}")
    tex.append(_fit_width(_latex_quarter_table(quarters, q_years, "revenue", q_asof)))
    tex.append(r"\vspace{0.7mm}\textbf{\fontsize{6.5}{7.5}\selectfont 季度净利润（百万元，累计值）}\par\vspace{0.2mm}")
    tex.append(_fit_width(_latex_quarter_table(quarters, q_years, "parent_np", q_asof)))
    tex.append(r"\vspace{0.7mm}\textbf{\fontsize{6.5}{7.5}\selectfont 每股分红（元）}\par\vspace{0.2mm}")
    tex.append(_fit_width(_latex_dividend_table(dividends, dividend_years)))
    tex.append(r"\end{minipage}")

    # 说明信息：紧跟页面内容，不再固定在页面底部
    tex.append(r"\vspace{1.5mm}\hrule\vspace{1mm}")
    tex.append(r"{\fontsize{5}{6}\selectfont")
    tex.append(r"\noindent\begin{minipage}[t]{0.46\textwidth}%s\end{minipage}\hfill" % footer_mid)
    tex.append(r"\begin{minipage}[t]{0.52\textwidth}%s\end{minipage}" % footer_right)
    tex.append(r"}")
    tex.append(r"\end{document}")

    with open(tex_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(tex))

    # 编译（在输出目录内执行，保证相对图片路径可用）
    tex_name = os.path.basename(tex_path)
    try:
        result = subprocess.run(
            ["xelatex", "-interaction=nonstopmode", "-halt-on-error", tex_name],
            cwd=out_dir, capture_output=True, timeout=180)
    except FileNotFoundError:
        raise DataError("未找到 xelatex；请安装 TeX Live / TinyTeX")
    except subprocess.SubprocessError as exc:
        raise DataError("xelatex 执行失败：%s" % exc)
    if result.returncode != 0:
        log_text = result.stdout.decode("utf-8", "replace")
        raise DataError("xelatex 编译失败：\n%s" % log_text[-1500:])

    pdf_path = os.path.splitext(tex_path)[0] + ".pdf"
    pdf_path = crop_pdf_to_content(pdf_path)
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(pdf_path)
        if doc.page_count != 1:
            log("  警告：PDF 共 %d 页，未达到一页要求" % doc.page_count)
        else:
            w_mm = doc[0].rect.width / 72.0 * 25.4
            h_mm = doc[0].rect.height / 72.0 * 25.4
            log("  PDF 页面尺寸：%.1f x %.1f mm（高度按内容自适应）" % (w_mm, h_mm))
    except Exception:
        pass
    return pdf_path


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def generate(code: str, out_path: Optional[str], report_date: str,
             with_charts: bool, current_year: Optional[int] = None,
             price_date: Optional[str] = None, make_pdf: bool = False) -> str:
    ticker = norm_ticker(code)
    full = full_code(code)
    log("== 正在生成 %s CM 手册 ==" % full)

    log("[1/6] 获取行情与公司资料 ...")
    try:
        quote = tencent_quote(ticker)
    except Exception as exc:  # noqa: BLE001
        log("  腾讯行情失败（继续使用财报数据）：%s" % exc)
        quote = {"name": ticker, "price": None}

    # 公司资料：新浪优先，东财只补新浪没有的字段（唯一性分类等）
    info = {"SECURITY_NAME_ABBR": quote.get("name") or ticker}
    info.update(sina_company_info(ticker))
    if not info.get("ORG_NAME") or not info.get("INDUSTRY"):
        try:
            em_info = fetch_company_info(ticker)
            if not info.get("ORG_NAME"):
                info["ORG_NAME"] = em_info.get("ORG_NAME")
            if not info.get("INDUSTRY"):
                em2016 = em_info.get("EM2016") or em_info.get("BOARD_NAME_LEVEL") or ""
                info["INDUSTRY"] = str(em2016).split("-")[-1]
            if not info.get("MAIN_BUSINESS"):
                info["MAIN_BUSINESS"] = (em_info.get("MAIN_BUSINESS")
                                         or em_info.get("BUSINESS_SCOPE") or "")
            if not info.get("FOUND_DATE"):
                info["FOUND_DATE"] = em_info.get("FOUND_DATE")
            if not info.get("LISTING_DATE"):
                info["LISTING_DATE"] = em_info.get("LISTING_DATE")
            if not info.get("ADDRESS"):
                info["ADDRESS"] = em_info.get("ADDRESS") or em_info.get("REG_ADDRESS")
            if not info.get("PROVINCE"):
                info["PROVINCE"] = em_info.get("PROVINCE")
            info["SECURITY_NAME_ABBR"] = (em_info.get("SECURITY_NAME_ABBR")
                                          or info["SECURITY_NAME_ABBR"])
        except Exception as exc:  # noqa: BLE001
            log("  东财公司资料兜底失败：%s" % exc)
    info["SECURITY_NAME_ABBR"] = info.get("SECURITY_NAME_ABBR") or quote.get("name") or ticker

    log("[2/6] 获取年度财务报表（新浪优先） ...")
    sina_raw = None
    try:
        annual, quarters, sina_raw = fetch_sina_financials(ticker)
        if not annual:
            raise DataError("新浪三表没有年度数据")
        log("  新浪三表：%d 个年度" % len(annual))
    except Exception as exc:  # noqa: BLE001
        log("  新浪三表获取失败，改用东方财富：%s" % exc)
        annual, quarters = build_annual_from_eastmoney(ticker)
        sina_raw = None

    # 东财独有：扣非归母净利润（新浪三表不含该科目）
    try:
        deduct = fetch_eastmoney_deduct(ticker)
        for y, value in deduct.items():
            if y in annual and annual[y].get("deduct_np") is None:
                annual[y]["deduct_np"] = value
    except Exception as exc:  # noqa: BLE001
        log("  扣非净利润（东财独有）获取失败：%s" % exc)

    # 东财独有：补新浪缺失的现金及等价物净增 / 经营净现金流（保险等模板差异）
    try:
        cf_supp = fetch_eastmoney_cashflow_supplement(ticker)
        for y, values in cf_supp.items():
            if y not in annual:
                continue
            if annual[y].get("cce_add") is None and values.get("cce_add") is not None:
                annual[y]["cce_add"] = values["cce_add"]
            if annual[y].get("operate_cf") is None and values.get("operate_cf") is not None:
                annual[y]["operate_cf"] = values["operate_cf"]
    except Exception as exc:  # noqa: BLE001
        log("  现金流补充字段（东财独有）获取失败：%s" % exc)

    all_years = sorted(annual.keys())
    if not all_years:
        raise DataError("没有取到任何年度财务数据，无法生成手册")
    latest_year = max(all_years)
    if current_year is not None:
        latest_year = current_year
    display_years = list(range(latest_year - 10, latest_year + 1))
    fetch_years = list(range(latest_year - 11, latest_year + 1))
    metrics = build_metrics(annual, fetch_years)

    # 报告指标日期股价：报告日当天或之前最近一个交易日
    price_asof = price_date or report_date
    log("[3/6] 获取报告指标日期 %s 的收盘价 ..." % price_asof)
    current_price, current_date = price_on_or_before(ticker, price_asof)
    if current_price is None:
        log("  未取到 %s 当日或之前的价格，退回到 %d 年末收盘价"
            % (price_asof, latest_year))
        current_price, current_date = price_on_date(ticker, latest_year)
    if current_price is None:
        current_price = quote.get("price")
        current_date = "最新交易日"
        log("  仍未取到 K 线，改用腾讯最新行情价：%s" % (current_price or "NA"))

    # 当前估值：股价×最新披露股本；动态 PE 用 TTM，PB 用最新一期归母净资产
    latest_metrics = metrics.get(latest_year, {})
    snapshot = build_valuation_snapshot(sina_raw, current_date or price_asof, annual)
    shares_now = snapshot.get("shares") or latest_metrics.get("shares")
    parent_eq_now = snapshot.get("parent_eq") or latest_metrics.get("parent_eq")
    ttm_np = snapshot.get("ttm_np") or latest_metrics.get("parent_np")
    mcap_yi = None
    if current_price is not None and shares_now:
        mcap_yi = current_price * shares_now / 1e8
    mcap_yuan = mcap_yi * 1e8 if mcap_yi is not None else None

    def _value_ratio(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
        if numerator is None or denominator is None or denominator == 0:
            return None
        return numerator / denominator

    valuation = {
        "price_date": current_date or price_asof,
        "price": current_price,
        "mcap_yi": mcap_yi,
        "shares": shares_now,
        "snapshot_date": snapshot.get("fiscal_date", ""),
        "static_pe": _value_ratio(mcap_yuan, latest_metrics.get("parent_np")),
        "static_pb": _value_ratio(mcap_yuan, latest_metrics.get("parent_eq")),
        "dynamic_pe": _value_ratio(mcap_yuan, ttm_np),
        "current_pb": _value_ratio(mcap_yuan, parent_eq_now),
        "ttm_np": ttm_np,
    }

    log("[4/6] 获取季度与分红数据 ...")
    if not quarters:
        try:
            quarters = {}
            qrows = fetch_main_quarters(ticker)
            for y, qs in qrows.items():
                for q, row in qs.items():
                    quarters.setdefault(y, {})[q] = {
                        "revenue": as_float(row.get("TOTALOPERATEREVE")),
                        "parent_np": as_float(row.get("PARENTNETPROFIT")),
                    }
        except Exception as exc:  # noqa: BLE001
            log("  季度数据获取失败：%s" % exc)
            quarters = {}
    # 东财独有：按报告期归档的分红送转（新浪分红页只有公告日，无法稳定对应年度报告期）
    try:
        dividends = fetch_dividends(ticker)
    except Exception as exc:  # noqa: BLE001
        log("  分红数据获取失败：%s" % exc)
        dividends = {}

    # 输出路径
    name = info.get("SECURITY_NAME_ABBR") or quote.get("name") or ticker
    if not out_path:
        out_path = os.path.join("output", "%s%s_CM 手册.md" % (safe_name(name), full))
    out_dir = os.path.dirname(os.path.abspath(out_path))
    charts_dir = os.path.join(out_dir, "charts")
    os.makedirs(out_dir, exist_ok=True)

    charts = {}
    price_window = ""
    price_adjust = "前复权"
    if make_pdf:
        with_charts = True      # PDF 必须包含图表
    if with_charts:
        log("[5/6] 生成图表 ...")
        os.makedirs(charts_dir, exist_ok=True)
        base = norm_ticker(ticker)

        # 1) 盈利能力
        p = os.path.join(charts_dir, "%s_profitability.svg" % base)
        prof_labels = [str(y) for y in display_years]
        prof_series = [
            ("净资产收益率", [metrics.get(y, {}).get("roe") for y in display_years]),
            ("总资产收益率", [metrics.get(y, {}).get("roa") for y in display_years]),
            ("销售净利率", [metrics.get(y, {}).get("net_margin") for y in display_years]),
        ]
        render_line_svg(p, "盈利能力趋势（%）", prof_labels, prof_series,
                        "百分比（%）", y_fmt="{:.1f}%")
        charts["profitability"] = os.path.relpath(p, out_dir).replace(os.sep, "/")
        if make_pdf:
            pp = os.path.join(charts_dir, "%s_profitability_print.svg" % base)
            render_line_svg(pp, "", prof_labels, prof_series,
                            "百分比（%）", width=900, height=170, y_fmt="{:.1f}%", bottom_margin=34)
            charts["profitability_print"] = pp

        # 2) 资产规模
        p = os.path.join(charts_dir, "%s_assets.svg" % base)
        asset_labels = [str(y) for y in display_years]
        asset_series = [
            ("资产总计", [((metrics.get(y, {}).get("assets") or 0) / 1e6)
                      if metrics.get(y, {}).get("assets") is not None else None
                      for y in display_years]),
            ("股东权益", [((metrics.get(y, {}).get("parent_eq") or 0) / 1e6)
                      if metrics.get(y, {}).get("parent_eq") is not None else None
                      for y in display_years]),
            ("流动资产", [((metrics.get(y, {}).get("current_assets") or 0) / 1e6)
                      if metrics.get(y, {}).get("current_assets") is not None else None
                      for y in display_years]),
        ]
        render_line_svg(p, "资产规模趋势（百万元）", asset_labels, asset_series,
                        "百万元", y_fmt="{:.0f}")
        charts["assets"] = os.path.relpath(p, out_dir).replace(os.sep, "/")
        if make_pdf:
            pp = os.path.join(charts_dir, "%s_assets_print.svg" % base)
            render_line_svg(pp, "", asset_labels, asset_series,
                            "百万元", width=900, height=170, y_fmt="{:.0f}", bottom_margin=34)
            charts["assets_print"] = pp

        # 3) 利润与现金流
        p = os.path.join(charts_dir, "%s_cashflow.svg" % base)
        cf_labels = [str(y) for y in display_years]
        cf_series = [
            ("息税前利润", [((metrics.get(y, {}).get("ebit") or 0) / 1e6)
                      if metrics.get(y, {}).get("ebit") is not None else None
                      for y in display_years]),
            ("扣非净利润", [((metrics.get(y, {}).get("deduct_np") or 0) / 1e6)
                      if metrics.get(y, {}).get("deduct_np") is not None else None
                      for y in display_years]),
            ("经营净现金流", [((metrics.get(y, {}).get("operate_cf") or 0) / 1e6)
                       if metrics.get(y, {}).get("operate_cf") is not None else None
                       for y in display_years]),
        ]
        render_line_svg(p, "利润与现金流趋势（百万元）", cf_labels, cf_series,
                        "百万元", y_fmt="{:.0f}")
        charts["cashflow"] = os.path.relpath(p, out_dir).replace(os.sep, "/")
        if make_pdf:
            pp = os.path.join(charts_dir, "%s_cashflow_print.svg" % base)
            render_line_svg(pp, "", cf_labels, cf_series,
                            "百万元", width=900, height=170, y_fmt="{:.0f}", bottom_margin=34)
            charts["cashflow_print"] = pp

        # 4) 股价（月线，前复权；报告日前推 10 年）
        try:
            chart_start, chart_end = report_window(report_date)
            krows = []
            for adjust in ("qfq", ""):
                for source in ("tencent", "eastmoney"):
                    try:
                        if source == "tencent":
                            rows = tencent_kline(ticker, "month", chart_start, chart_end,
                                                 640, adjust)
                        else:
                            rows = em_kline(ticker, "month", chart_start, chart_end, adjust)
                    except DataError:
                        rows = []
                    # 腾讯前复权是等差口径，高分红老股可能被减成负数；
                    # 出现 ≤0 时换东财前复权，再不行才退回不复权。
                    if rows and all(r["close"] > 0 for r in rows):
                        krows = rows
                        price_adjust = "前复权" if adjust == "qfq" else "不复权（前复权数据不可用）"
                        break
                if krows:
                    break
            if krows:
                p = os.path.join(charts_dir, "%s_price.svg" % base)
                # 每个年度只标一次，避免月度标签堆叠
                x_labels = []
                for i, r in enumerate(krows):
                    year = r["date"][:4]
                    if i == 0 or year != krows[i - 1]["date"][:4]:
                        x_labels.append(year)
                    else:
                        x_labels.append("")
                vals = [r["close"] for r in krows]
                price_series = [("前复权收盘价" if price_adjust == "前复权" else "收盘价", vals)]
                price_title = ("股价走势（%s ~ %s，%s）"
                               % (krows[0]["date"][:7], krows[-1]["date"][:7], price_adjust))
                # 说明里写请求的 10 年窗口；图标题写月线实际覆盖的月份范围
                price_window = "%s ~ %s" % (chart_start, chart_end)
                render_line_svg(p, price_title, x_labels, price_series,
                                "股价（元）", y_fmt="{:.2f}", line_width=3.5)
                charts["price"] = os.path.relpath(p, out_dir).replace(os.sep, "/")
                if make_pdf:
                    # 窄幅打印图每隔一年标注一次，避免加粗后年份标签互相重叠
                    non_empty = [i for i, label in enumerate(x_labels) if label]
                    print_x_labels = [""] * len(x_labels)
                    for pos, idx in enumerate(non_empty):
                        if pos % 2 == 0 or idx == non_empty[-1]:
                            print_x_labels[idx] = x_labels[idx]
                    pp = os.path.join(charts_dir, "%s_price_print.svg" % base)
                    render_line_svg(pp, "", print_x_labels, price_series,
                                    "股价（元）", width=480, height=290,
                                    y_fmt="{:.2f}", bottom_margin=32, line_width=3.5)
                    charts["price_print"] = pp
        except Exception as exc:  # noqa: BLE001
            log("  股价图生成失败：%s" % exc)

    log("[6/6] 写入 Markdown ...")
    md = build_markdown(ticker, info, quote, metrics, display_years, latest_year,
                        valuation, quarters, dividends,
                        report_date, charts, with_charts, price_window, price_adjust)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(md)
    log("已生成 Markdown：%s" % out_path)

    if make_pdf:
        log("[7/7] 生成一页 LaTeX PDF ...")
        tex_path = os.path.splitext(out_path)[0] + ".tex"
        pdf_path = render_latex_pdf(
            tex_path, name, full, info, valuation, metrics, display_years,
            latest_year, quarters, dividends, report_date, charts,
            price_window, price_adjust)
        log("已生成 PDF：%s" % pdf_path)
    return out_path


def main() -> None:
    global QUIET
    parser = argparse.ArgumentParser(
        description="CM 手册生成器（Markdown）：腾讯行情/K线 + 新浪财报/公司资料 "
                    "+ 东方财富独有数据（扣非/分红/北交所 K 线兜底）")
    parser.add_argument("code", help="股票代码，如 600219.SH / 600219 / sh600219")
    parser.add_argument("-o", "--out", help="输出 Markdown 路径（默认 output/<名称><代码>_CM 手册.md）")
    parser.add_argument("--report-date", default=date.today().strftime("%Y/%m/%d"),
                        help="制表日期，默认今天，格式 YYYY/MM/DD")
    parser.add_argument("--price-date", default=None,
                        help="报告指标日期（当前股价/市值/PE/PB 的快照日），默认同 --report-date")
    parser.add_argument("--current-year", type=int, default=None,
                        help="指定最新年报年度（默认取数据源最新年度）")
    parser.add_argument("--no-charts", action="store_true", help="不生成 SVG 图表")
    parser.add_argument("--pdf", action="store_true",
                        help="在 Markdown 之后生成一页 LaTeX PDF（需要 xelatex + rsvg-convert）")
    parser.add_argument("-q", "--quiet", action="store_true", help="静默模式")
    args = parser.parse_args()

    QUIET = args.quiet
    try:
        generate(args.code, args.out, args.report_date, not args.no_charts,
                 args.current_year, args.price_date, args.pdf)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write("生成失败：%s\n" % exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
