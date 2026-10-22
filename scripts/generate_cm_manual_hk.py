#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CM 手册生成器（港股版）

版式与 A 股版保持一致：
  · 顶部：代码 / 报告指标日期 / 当前股价 / 当前市值 / 静态 PE/PB / 动态 PE / 市净率
  · 主表：最近 11 个年度的盈利能力、资产负债、利润与现金流、股本
  · 右侧栏：股价走势、公司概况、年均增长率、半年度营收/利润、每股分红

与 A 股版的主要差异：
  · 港股财报通常只有年度 + 半年度，季度数据仅部分公司披露，因此季度表改为半年度表。
  · 港股没有「扣非归母净利润」，利润表改用「股东应占溢利」；毛利率直接采用披露的「毛利」。
  · 港股财报币种可能是人民币、港元或美元，报表金额按披露币种列示，不做汇率折算；
    股价与市值统一为港元。估值比率用数据源报告期 PE/PB 结合当前市值推算。
  · 分红按「每股派息（港元）」展示，并给出按当前股价计算的股息率。

数据源：
  · 腾讯财经  qt.gtimg.cn / web.ifzq.gtimg.cn —— 港股行情、月/日 K 线、汇率
  · 东方财富  datacenter.eastmoney.com —— 港股三表（长表）、主要指标、公司资料、分红明细
      - RPT_HKF10_FN_{BALANCE,INCOME,CASHFLOW}_PC
      - RPT_HKF10_FN_MAININDICATOR
      - RPT_HKF10_INFO_ORGPROFILE
      - RPT_HKF10_INFO_DIVIDEND
  · 东方财富 push2his —— 腾讯 K 线不可用时的港股 K 线兜底

用法：
    python3 scripts/generate_cm_manual_hk.py 00700
    python3 scripts/generate_cm_manual_hk.py 00700.HK --report-date 2026-05-06
    python3 scripts/generate_cm_manual_hk.py 00700 --pdf
    python3 scripts/generate_cm_manual_hk.py 09988 -o output/阿里巴巴_CM手册.md --no-charts

依赖与 A 股版相同：requests；PDF 需要 xelatex、rsvg-convert 与中文字体。
"""

import argparse
import os
import re
import sys
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

# 复用 A 股版的通用工具（HTTP、格式化、SVG、Markdown 表格、LaTeX 底层函数）
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import generate_cm_manual as base  # noqa: E402

# 基础模块的日志默认关闭，统一使用本文件的 log()，避免重复输出
base.QUIET = True

DataError = base.DataError

QUIET = False


def log(msg: str) -> None:
    if not QUIET:
        sys.stderr.write(msg + "\n")


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

HK_QUOTE_URL = "https://qt.gtimg.cn/q="
TENCENT_HK_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/hkfqkline/get"
TENCENT_HK_KLINE_PLAIN_URL = "https://web.ifzq.gtimg.cn/appstock/app/kline/kline"
EM_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"


# ---------------------------------------------------------------------------
# 代码归一化
# ---------------------------------------------------------------------------

def norm_hk_ticker(code: str) -> str:
    """任意受支持写法 → 5 位港股代码（如 700 / HK00700 / 00700.HK → 00700）。"""
    raw = str(code).strip().upper().replace(" ", "")
    raw = re.sub(r"\.HK$", "", raw)
    raw = re.sub(r"^HK", "", raw)
    if not re.fullmatch(r"\d{1,5}", raw):
        raise ValueError("无法识别港股代码 %r；支持 00700 / 700 / HK00700 / 00700.HK" % code)
    num = int(raw)
    if num <= 0:
        raise ValueError("港股代码必须为正整数：%r" % code)
    return "%05d" % num


def full_hk_code(code: str) -> str:
    return norm_hk_ticker(code) + ".HK"


def hk_secucode(code: str) -> str:
    return full_hk_code(code)


# ---------------------------------------------------------------------------
# 腾讯港股行情 / K 线 / 汇率
# ---------------------------------------------------------------------------

def tencent_hk_quote(code: str) -> dict:
    """腾讯港股实时行情。

    字段位置与 A 股不同：39=市盈率，43=振幅，44=流通市值(亿)，45=总市值(亿)，
    46=英文名，47=股息率，48/49=52 周高/低，69=总股本，75=交易币种。
    """
    ticker = norm_hk_ticker(code)
    symbol = "hk" + ticker
    resp = base.http_get(HK_QUOTE_URL + symbol, timeout=15)
    text = resp.content.decode("gbk", "replace")
    match = re.search(r'v_%s="([^"]*)"' % re.escape(symbol), text)
    if not match:
        raise DataError("腾讯港股行情没有返回 %s 的数据" % symbol)
    fields = match.group(1).split("~")
    if len(fields) < 76:
        raise DataError("腾讯港股行情 %s 字段数异常" % symbol)

    def f(idx: int) -> Optional[float]:
        try:
            value = fields[idx]
            return float(value) if value not in ("", "-", "None") else None
        except (ValueError, IndexError):
            return None

    return {
        "symbol": symbol,
        "name": fields[1],
        "price": f(3),
        "last_close": f(4),
        "change_pct": f(32),
        "pe": f(39),
        "mcap_yi": f(45),
        "float_mcap_yi": f(44),
        "dividend_yield": f(47),
        "shares": f(69),
        "currency": fields[75] if len(fields) > 75 else "HKD",
    }


def tencent_hk_kline(code: str, period: str = "day", count: int = 200,
                     adjust: str = "qfq") -> List[dict]:
    """腾讯港股 K 线。period: day/week/month；adjust: ''/qfq/hfq。

    返回的是最近 count 根 K 线（与 A 股接口一致），由调用方按日期过滤。
    港股前复权价格对高分红老股可能为负，调用方需检查。
    """
    symbol = "hk" + norm_hk_ticker(code)
    if adjust:
        # 前复权/后复权走 hkfqkline
        url = TENCENT_HK_KLINE_URL
        param = "%s,%s,,,%d,%s" % (symbol, period, count, adjust)
        key = adjust + period
    else:
        # 港股不复权 K 线走 kline/kline；hkfqkline 不接受空复权参数
        url = TENCENT_HK_KLINE_PLAIN_URL
        param = "%s,%s,,,%d" % (symbol, period, count)
        key = period
    resp = base.http_get(url, params={"param": param},
                         headers={"Referer": "https://gu.qq.com/"}, timeout=20)
    payload = resp.json()
    node = (payload.get("data") or {}).get(symbol) or {}
    items = node.get(key)
    if not isinstance(items, list):
        for fallback_key in (period, "qfq" + period, "hfq" + period):
            if isinstance(node.get(fallback_key), list):
                items = node[fallback_key]
                break
    if not isinstance(items, list):
        raise DataError("腾讯港股 K 线没有返回 %s 的 %s 数据" % (symbol, period))

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
    if not rows:
        raise DataError("腾讯港股 K 线 %s 返回空数据" % symbol)
    rows.sort(key=lambda r: r["date"])
    return rows


def em_hk_kline(code: str, period: str = "day", start: str = "", end: str = "",
                adjust: str = "qfq") -> List[dict]:
    """东方财富港股 K 线（腾讯 K 线兜底）。secid 市场号为 116。"""
    secid = "116." + norm_hk_ticker(code)
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
    base._sleep_between_em()
    try:
        try:
            payload = base.curl_json(EM_KLINE_URL, params,
                                     referer="https://quote.eastmoney.com/")
        except DataError:
            resp = base.em_get(EM_KLINE_URL, params,
                               headers={"Referer": "https://quote.eastmoney.com/"},
                               timeout=20)
            payload = resp.json()
    finally:
        base._em_last_call[0] = base.time.time()

    if payload.get("rc") not in (0, None):
        raise DataError("东财港股 K 线返回错误：%s" % payload.get("msg"))
    klines = ((payload.get("data") or {}).get("klines")) or []
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
        raise DataError("东财港股 K 线没有返回 %s 的数据" % norm_hk_ticker(code))
    return rows


def hk_price_on_or_before(code: str, target_date: str) -> Tuple[Optional[float], str]:
    """取 target_date 当日或之前最近一个交易日的收盘价（港元，不复权）。"""
    try:
        target = date.fromisoformat(str(target_date).replace("/", "-"))
    except ValueError:
        raise ValueError("价格日期格式应为 YYYY-MM-DD：%r" % target_date)

    # 腾讯接口只按 count 取最近 N 根，按目标日期回溯所需交易日粗略放大
    days = max(30, (date.today() - target).days)
    count = min(4000, int(days * 1.6) + 80)
    rows: List[dict] = []
    try:
        rows = tencent_hk_kline(code, "day", count, "")
    except DataError:
        rows = []
    rows = [r for r in rows if r["date"] <= target.isoformat() and r["close"] > 0]

    # 目标日期早于腾讯返回窗口时，改用东财按日期区间取数
    if not rows:
        try:
            rows = em_hk_kline(code, "day",
                               (target - timedelta(days=370)).isoformat(),
                               target.isoformat(), "")
        except DataError:
            rows = []
    if not rows:
        return None, ""
    return rows[-1]["close"], rows[-1]["date"]


def hk_price_on_year_end(code: str, year: int) -> Tuple[Optional[float], str]:
    """取某年最后一个交易日的收盘价（不复权）。"""
    try:
        rows = tencent_hk_kline(code, "day", 4000, "")
        rows = [r for r in rows if r["date"].startswith(str(year))]
    except DataError:
        rows = []
    if not rows:
        try:
            rows = em_hk_kline(code, "day", "%d-01-01" % year,
                               "%d-12-31" % year, "")
        except DataError:
            rows = []
    if not rows:
        return None, ""
    return rows[-1]["close"], rows[-1]["date"]


def _tencent_fx(symbol: str) -> Optional[float]:
    """腾讯汇率中间价。whHKDCNY 返回「1 港元 = x 人民币」。"""
    try:
        resp = base.http_get(HK_QUOTE_URL + symbol, timeout=10)
        text = resp.content.decode("gbk", "replace")
        match = re.search(r'v_%s="([^"]*)"' % re.escape(symbol), text)
        if not match:
            return None
        fields = match.group(1).split("~")
        return base.as_float(fields[3]) if len(fields) > 3 else None
    except Exception:  # noqa: BLE001
        return None


def fetch_hk_fx() -> dict:
    """分红币种换算所需的港元汇率。

    hkd_per_cny：1 人民币折港元；hkd_per_usd：1 美元折港元。
    取不到时使用近似值并记录日志。
    """
    hkd_cny = _tencent_fx("whHKDCNY")   # CNY per HKD
    usd_cny = _tencent_fx("whUSDCNY")   # CNY per USD
    out = {"hkd_per_cny": None, "hkd_per_usd": None}
    if hkd_cny and hkd_cny > 0:
        out["hkd_per_cny"] = 1.0 / hkd_cny
        if usd_cny and usd_cny > 0:
            out["hkd_per_usd"] = usd_cny / hkd_cny
    if not out["hkd_per_cny"]:
        out["hkd_per_cny"] = 1.176
        log("  港元/人民币汇率获取失败，按 1 CNY ≈ 1.176 HKD 估算")
    if not out["hkd_per_usd"]:
        out["hkd_per_usd"] = 7.80
        log("  港元/美元汇率获取失败，按 1 USD ≈ 7.80 HKD 估算")
    return out


# ---------------------------------------------------------------------------
# 东方财富港股 F10
# ---------------------------------------------------------------------------

def fetch_hk_main(code: str) -> Tuple[List[dict], Dict[int, dict]]:
    """港股主要指标。DATE_TYPE_CODE=001 为年报。

    注意：PE_TTM / PB_TTM / TOTAL_MARKET_CAP / ISSUED_COMMON_SHARES 是数据源
    当前快照，会重复出现在每个年度行中；只能取最新年报行使用。
    """
    rows = base.em_f10("RPT_HKF10_FN_MAININDICATOR",
                       '(SECUCODE="%s")' % hk_secucode(code), 200,
                       allow_empty=True)
    rows.sort(key=lambda r: str(r.get("REPORT_DATE") or ""))
    annual = {}
    for row in rows:
        if str(row.get("DATE_TYPE_CODE")) != "001":
            continue
        d = str(row.get("REPORT_DATE") or "")[:10]
        if len(d) == 10:
            annual[int(d[:4])] = row
    return rows, annual


def fetch_hk_statement(code: str, kind: str) -> Tuple[Dict[str, Dict[str, Optional[float]]],
                                                      Dict[str, str]]:
    """港股三表长表 → {报告日: {科目: 金额}}，同时返回报告日 → DATE_TYPE_CODE。

    kind: BALANCE / INCOME / CASHFLOW。东财港股报表一行一个科目，
    因此需要先拉全量再按报告日聚合。
    """
    report = "RPT_HKF10_FN_%s_PC" % kind
    rows = base.em_f10(report, '(SECUCODE="%s")' % hk_secucode(code), 5000,
                       allow_empty=True)
    if len(rows) >= 5000:
        log("  警告：%s 返回 %d 行，可能被截断" % (report, len(rows)))
    by_date: Dict[str, Dict[str, Optional[float]]] = {}
    types: Dict[str, str] = {}
    for row in rows:
        d = str(row.get("REPORT_DATE") or "")[:10]
        name = row.get("STD_ITEM_NAME")
        if len(d) != 10 or not name:
            continue
        by_date.setdefault(d, {})[str(name)] = base.as_float(row.get("AMOUNT"))
        types[d] = str(row.get("DATE_TYPE_CODE") or "")
    return by_date, types


def fetch_hk_profile(code: str) -> dict:
    rows = base.em_f10("RPT_HKF10_INFO_ORGPROFILE",
                       '(SECUCODE="%s")' % hk_secucode(code), 1,
                       sort_column=None, allow_empty=True)
    if not rows:
        raise DataError("东方财富没有返回港股公司资料")
    return rows[0]


def fetch_hk_dividend_rows(code: str) -> List[dict]:
    return base.em_f10("RPT_HKF10_INFO_DIVIDEND",
                       '(SECUCODE="%s")' % hk_secucode(code), 200,
                       sort_column="NOTICE_DATE", allow_empty=True)


# ---------------------------------------------------------------------------
# 分红解析
# ---------------------------------------------------------------------------

_DIV_PERIOD_RE = re.compile(
    r"^(\d{4})\s*年?\s*(末期|年度|中期|半年度|第?[一二三四]季度?)$")


def _dividend_amount(text: str) -> Tuple[Optional[float], str]:
    """从分红方案文本解析「每股金额」与币种。

    例：
      「每股派港币5.3元」       → (5.3, HKD)
      「每10股派人民币2.5元」   → (0.25, CNY)
      「每10股分派1股美团B类普通股股份...」 → (None, '')  实物分派跳过
    """
    if not text:
        return None, ""
    # 实物分派（送股/分派其他公司股票）无法折算为每股现金，跳过
    if "普通股" in text or ("分派" in text and "股" in text
                          and "元" not in text and "港元" not in text):
        return None, ""

    match = re.search(
        r"每\s*(\d+)\s*股[^0-9]{0,16}([0-9]+(?:\.[0-9]+)?)\s*(?:元|港元|港币|人民币|美元)?",
        text)
    if match:
        lot = int(match.group(1))
        amount = float(match.group(2))
    else:
        match = re.search(
            r"每股[^0-9]{0,16}([0-9]+(?:\.[0-9]+)?)\s*(?:元|港元|港币|人民币|美元)?",
            text)
        if not match:
            return None, ""
        lot = 1
        amount = float(match.group(1))
    if lot <= 0:
        return None, ""

    # 方案里若已给出港元等值，直接采用，避免汇率误差
    hkd_match = re.search(
        r"相当于(?:港币|港元)\s*([0-9]+(?:\.[0-9]+)?)\s*元", text)
    if hkd_match:
        return float(hkd_match.group(1)) / lot, "HKD"

    upper = text.upper()
    if "人民币" in text or "RMB" in upper or "CNY" in upper:
        ccy = "CNY"
    elif "美元" in text or "USD" in upper:
        ccy = "USD"
    else:
        ccy = "HKD"
    return amount / lot, ccy


def fetch_hk_dividends(code: str, fx: dict) -> Dict[int, dict]:
    """按财政年度汇总每股现金分红（港元）。

    分红明细的 ASSIGN_PERIOD 形如「2025末期」「2025中期」「特别分配」；
    特别分配（实物派发）跳过。
    """
    rows = fetch_hk_dividend_rows(code)
    out: Dict[int, dict] = {}
    for row in rows:
        period = str(row.get("ASSIGN_PERIOD") or "").strip()
        match = _DIV_PERIOD_RE.match(period)
        if not match:
            continue
        year = int(match.group(1))
        amount, ccy = _dividend_amount(str(row.get("PLAN_EXPLAIN") or ""))
        if amount is None:
            continue
        hkd = amount
        if ccy == "CNY":
            hkd = amount * (fx.get("hkd_per_cny") or 1.176)
        elif ccy == "USD":
            hkd = amount * (fx.get("hkd_per_usd") or 7.80)
        item = out.setdefault(year, {"dps_hkd": 0.0, "items": []})
        item["dps_hkd"] += hkd
        item["items"].append({
            "period": period,
            "amount": amount,
            "currency": ccy,
            "hkd": hkd,
            "plan": str(row.get("PLAN_EXPLAIN") or ""),
        })
    return out


# ---------------------------------------------------------------------------
# 年度数据整理
# ---------------------------------------------------------------------------

def _num(*values: Any) -> Optional[float]:
    """返回第一个非 None 的数值。"""
    for value in values:
        if value is not None:
            return value
    return None


def _annual_map(by_date: Dict[str, dict], types: Dict[str, str],
                fy_end: str) -> Dict[int, dict]:
    """按 DATE_TYPE_CODE=001 归集年度报表；缺失时按财政年结日兜底。"""
    out = {}
    for d, items in by_date.items():
        if types.get(d) == "001" or (len(d) == 10 and d[5:10] == fy_end):
            out[int(d[:4])] = items
    return out


def build_hk_annual(balance: Dict[int, dict], income: Dict[int, dict],
                    cashflow: Dict[int, dict],
                    main_annual: Dict[int, dict]) -> Dict[int, dict]:
    """把港股三表 + 主要指标整理成与 A 股版相近的扁平结构。"""
    years = sorted(set(balance) | set(income) | set(cashflow) | set(main_annual))
    annual = {}
    for year in years:
        bal = balance.get(year, {})
        inc = income.get(year, {})
        cf = cashflow.get(year, {})
        main = main_annual.get(year, {})

        revenue = _num(
            base._pick(inc, ["营业额", "经营收入总额", "营运收入", "总收入", "营业收入"]),
            base.as_float(main.get("OPERATE_INCOME")))
        gross_profit = base._pick(inc, ["毛利"])
        op_cost = base._pick(inc, ["营运支出", "经营支出总额", "营业成本"])
        op_profit = _num(base._pick(inc, ["经营溢利", "营业利润"]),
                         base.as_float(main.get("OPERATE_PROFIT")))
        total_profit = _num(base._pick(inc, ["除税前溢利", "利润总额"]),
                            base.as_float(main.get("PRETAX_PROFIT")))
        net_profit = _num(base._pick(inc, ["除税后溢利", "净利润"]),
                          base.as_float(main.get("NETPROFIT")))
        parent_np = _num(base._pick(inc, ["股东应占溢利", "归属于母公司所有者的净利润"]),
                         base.as_float(main.get("HOLDER_PROFIT")))
        eps = _num(base._pick(inc, ["每股基本盈利"]),
                   base.as_float(main.get("BASIC_EPS")))

        # 银行/保险等金融企业没有「营业成本/毛利」，利息收支属于营业收支，不做 EBIT 调整
        is_financial = (("净利息收入" in inc)
                        or ("经营收入总额" in inc and "毛利" not in inc))

        # 历史总股本未单独披露；用归母净利润 / 基本每股盈利推算
        shares = None
        if parent_np is not None and eps not in (None, 0):
            shares = parent_np / eps

        annual[year] = {
            "assets": _num(base._pick(bal, ["总资产"]),
                           base.as_float(main.get("TOTAL_ASSETS"))),
            "parent_eq": _num(base._pick(bal, ["股东权益"]),
                              base.as_float(main.get("TOTAL_PARENT_EQUITY"))),
            "total_equity": _num(base._pick(bal, ["总权益"]),
                                 base._pick(bal, ["股东权益"]),
                                 base.as_float(main.get("TOTAL_PARENT_EQUITY"))),
            "total_liab": _num(base._pick(bal, ["总负债"]),
                               base.as_float(main.get("TOTAL_LIABILITIES"))),
            "money": base._pick(bal, ["现金及等价物", "现金及现金等价物"]),
            "current_assets": base._pick(bal, ["流动资产合计"]),
            "noncurrent_assets": base._pick(bal, ["非流动资产合计"]),
            "current_liab": base._pick(bal, ["流动负债合计"]),
            "noncurrent_liab": base._pick(bal, ["非流动负债合计"]),
            "inventory": base._pick(bal, ["存货"]),
            "fixed_assets": base._pick(bal, ["物业厂房及设备", "固定资产"]),
            "goodwill": base._pick(bal, ["商誉"]),
            "short_loan": base._pick(bal, ["短期贷款", "短期借款"]),
            "long_loan": base._pick(bal, ["长期贷款", "长期借款"]),
            "shares": shares,
            "eps": eps,
            "revenue": revenue,
            "gross_profit": gross_profit,
            "op_cost": op_cost,
            "op_profit": op_profit,
            "total_profit": total_profit,
            "net_profit": net_profit,
            "parent_np": parent_np,
            "interest_income": base._pick(inc, ["利息收入"]),
            "finance_cost": base._pick(inc, ["融资成本", "利息支出"]),
            "operate_cf": base._pick(cf, ["经营业务现金净额",
                                          "经营活动产生的现金流量净额"]),
            "cce_add": base._pick(cf, ["现金净额", "现金及现金等价物净增加额",
                                       "现金及现金等价物净额"]),
            "is_financial": is_financial,
        }
    return annual


def build_hk_metrics(annual: Dict[int, dict], years: List[int]) -> Dict[int, dict]:
    """计算年度指标，口径与 A 股版一致（ROE/ROA/负债率/毛利率/净利率等）。"""
    out = {}
    for year in years:
        d = annual.get(year, {})
        prev = annual.get(year - 1, {})
        assets = d.get("assets")
        parent_eq = d.get("parent_eq")
        total_liab = d.get("total_liab")
        revenue = d.get("revenue")
        net_profit = d.get("net_profit")
        parent_np = d.get("parent_np")
        op_profit = d.get("op_profit")
        total_profit = d.get("total_profit")

        ebit = None
        if total_profit is not None:
            if d.get("is_financial"):
                ebit = total_profit
            else:
                ebit = (total_profit + (d.get("finance_cost") or 0.0)
                        - (d.get("interest_income") or 0.0))
        elif op_profit is not None:
            ebit = op_profit

        prev_assets = prev.get("assets")
        prev_parent_eq = prev.get("parent_eq")
        avg_assets = ((assets + prev_assets) / 2.0
                      if assets is not None and prev_assets is not None else assets)
        avg_parent_eq = ((parent_eq + prev_parent_eq) / 2.0
                         if parent_eq is not None and prev_parent_eq is not None
                         else parent_eq)

        roe = base.div(parent_np, avg_parent_eq)
        roa = base.div(ebit, avg_assets)
        debt_ratio = base.div(total_liab, assets)
        gross_margin = None
        if d.get("gross_profit") is not None and revenue:
            gross_margin = d["gross_profit"] / revenue
        net_margin = base.div(net_profit, revenue)
        current_ratio = base.div(d.get("current_assets"), d.get("current_liab"))
        turnover = base.div(revenue, avg_assets)

        out[year] = {
            **d,
            "ebit": ebit,
            "roe": roe * 100 if roe is not None else None,
            "roa": roa * 100 if roa is not None else None,
            "debt_ratio": debt_ratio * 100 if debt_ratio is not None else None,
            "gross_margin": gross_margin * 100 if gross_margin is not None else None,
            "net_margin": net_margin * 100 if net_margin is not None else None,
            "current_ratio": current_ratio,
            "asset_turnover": turnover,
        }
    return out


def _norm_fy_end(value: Any, default: str = "12-31") -> str:
    """把 FISCAL_YEAR（可能是 3-31）规范成 MM-DD。"""
    text = str(value or "").strip()
    match = re.match(r"^(\d{1,2})-(\d{1,2})$", text)
    if not match:
        return default
    month, day = int(match.group(1)), int(match.group(2))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return default
    return "%02d-%02d" % (month, day)


def _is_fy_end(d: str, fy_end: str) -> bool:
    return len(d) == 10 and d[5:10] == fy_end


def build_hk_ttm(income_by_date: Dict[str, dict], types: Dict[str, str],
                 fy_end: str, as_of: str) -> Tuple[Optional[float], str]:
    """按最新报告期计算 TTM 股东应占溢利（累计值口径）。

    若最新期为年报，TTM = 年报利润；
    若最新期为中期/季度，TTM = 上一年报 + 本期累计 − 上年同期累计。
    """
    available = sorted(d for d in income_by_date if d <= as_of)
    if not available:
        return None, ""
    latest = available[-1]
    cum = base._pick(income_by_date[latest], ["股东应占溢利",
                                              "归属于母公司所有者的净利润"])
    if _is_fy_end(latest, fy_end):
        return cum, latest

    annual_dates = [d for d in available
                    if types.get(d) == "001" or _is_fy_end(d, fy_end)]
    if not annual_dates:
        return cum, latest
    prior_annual = annual_dates[-1]
    annual_np = base._pick(income_by_date[prior_annual],
                           ["股东应占溢利", "归属于母公司所有者的净利润"])
    try:
        prev_same = "%04d%s" % (int(latest[:4]) - 1, latest[4:])
    except ValueError:
        prev_same = ""
    prev_np = base._pick(income_by_date.get(prev_same, {}),
                         ["股东应占溢利", "归属于母公司所有者的净利润"])
    if None not in (annual_np, cum, prev_np):
        return annual_np + cum - prev_np, latest
    return (annual_np if annual_np is not None else cum), latest


def latest_hk_equity(balance_by_date: Dict[str, dict],
                     as_of: str) -> Tuple[Optional[float], str]:
    """最新一期（MRQ）股东权益。"""
    dates = sorted(d for d in balance_by_date if d <= as_of)
    for d in reversed(dates):
        equity = base._pick(balance_by_date[d], ["股东权益", "总权益"])
        if equity is not None:
            return equity, d
    return None, ""


def build_hk_valuation(main_latest: dict, latest_metrics: dict,
                       ttm_np: Optional[float], latest_eq: Optional[float],
                       quote: dict, current_price: Optional[float],
                       shares_now: Optional[float]) -> dict:
    """估值快照。

    数据源的 PE_TTM / PB_TTM / TOTAL_MARKET_CAP 是同一时点快照。
    用「当前市值 / 快照市值」缩放，即可在不做汇率换算的情况下得到当前估值：
      静态 PE = 快照 PE × 市值变动 × (TTM 利润 / 年报利润)
      静态 PB = 快照 PB × 市值变动
      当前 PB = 静态 PB × (年报股东权益 / 最新一期股东权益)
    """
    # 市值按「报告指标日期股价 × 最新总股本」计算，保证历史价格日期也一致；
    # 行情快照只在价格或股本缺失时兜底。
    mcap_yi = None
    if current_price is not None and shares_now:
        mcap_yi = current_price * shares_now / 1e8
    elif quote.get("mcap_yi") is not None:
        mcap_yi = quote.get("mcap_yi")
    mcap_now = mcap_yi * 1e8 if mcap_yi is not None else None

    mcap_report = base.as_float(main_latest.get("TOTAL_MARKET_CAP"))
    pe_report = base.as_float(main_latest.get("PE_TTM"))
    pb_report = base.as_float(main_latest.get("PB_TTM"))
    mcap_ratio = (mcap_now / mcap_report
                  if mcap_now is not None and mcap_report else None)

    annual_np = latest_metrics.get("parent_np")
    annual_eq = latest_metrics.get("parent_eq")

    static_pe = (pe_report * mcap_ratio
                 if pe_report is not None and mcap_ratio is not None
                 else quote.get("pe"))
    dynamic_pe = None
    if static_pe is not None and annual_np and ttm_np not in (None, 0):
        dynamic_pe = static_pe * annual_np / ttm_np
    elif static_pe is not None:
        dynamic_pe = static_pe

    static_pb = (pb_report * mcap_ratio
                 if pb_report is not None and mcap_ratio is not None else None)
    # 最新一期净资产缺失时退回年报口径，避免市净率无谓显示 NA
    if latest_eq is None:
        latest_eq = annual_eq
    current_pb = None
    if (static_pb is not None and annual_eq and latest_eq not in (None, 0)):
        current_pb = static_pb * annual_eq / latest_eq

    return {
        "price_date": "",
        "price": current_price,
        "mcap_yi": mcap_yi,
        "shares": shares_now,
        "snapshot_date": "",
        "static_pe": static_pe,
        "static_pb": static_pb,
        "dynamic_pe": dynamic_pe,
        "current_pb": current_pb,
        "ttm_np": ttm_np,
    }


def build_hk_halfyear(income_by_date: Dict[str, dict], types: Dict[str, str],
                      fy_end: str, as_of: str) -> Dict[int, dict]:
    """按财政年度归集「中期」与「全年」营收/股东应占溢利。"""
    fy_end_month = int(fy_end[:2]) if re.match(r"^\d{2}-\d{2}$", fy_end) else 12
    out: Dict[int, dict] = {}
    for d, items in income_by_date.items():
        if d > as_of or len(d) != 10:
            continue
        report_type = types.get(d)
        if report_type == "001" or _is_fy_end(d, fy_end):
            year = int(d[:4])
            out.setdefault(year, {})["annual"] = {
                "date": d,
                "revenue": base._pick(items, ["营业额", "经营收入总额",
                                              "营运收入", "总收入", "营业收入"]),
                "parent_np": base._pick(items, ["股东应占溢利",
                                                "归属于母公司所有者的净利润"]),
            }
        elif report_type == "002":
            month = int(d[5:7])
            year = int(d[:4]) + (1 if month > fy_end_month else 0)
            out.setdefault(year, {})["interim"] = {
                "date": d,
                "revenue": base._pick(items, ["营业额", "经营收入总额",
                                              "营运收入", "总收入", "营业收入"]),
                "parent_np": base._pick(items, ["股东应占溢利",
                                                "归属于母公司所有者的净利润"]),
            }
    return out


# ---------------------------------------------------------------------------
# 图表
# ---------------------------------------------------------------------------

def _price_chart_rows(code: str, start: str, end: str) -> Tuple[List[dict], str]:
    """月线股价图数据：优先腾讯前复权，负值/失败时依次回退东财前复权、不复权。"""
    # 1) 腾讯前复权
    try:
        rows = tencent_hk_kline(code, "month", 200, "qfq")
        rows = [r for r in rows if start <= r["date"] <= end]
        if rows and all(r["close"] > 0 for r in rows):
            return rows, "前复权"
    except DataError:
        pass
    # 2) 腾讯不复权（港股 hkfqkline 的早期前复权可能为负）
    try:
        rows = tencent_hk_kline(code, "month", 200, "")
        rows = [r for r in rows if start <= r["date"] <= end]
        if rows:
            return rows, "不复权（前复权数据不可用）"
    except DataError:
        pass
    # 3) 东财前复权
    try:
        rows = em_hk_kline(code, "month", start, end, "qfq")
        if rows and all(r["close"] > 0 for r in rows):
            return rows, "前复权"
    except DataError:
        pass
    # 4) 东财不复权
    try:
        rows = em_hk_kline(code, "month", start, end, "")
        if rows:
            return rows, "不复权（前复权数据不可用）"
    except DataError:
        pass
    return [], ""


# ---------------------------------------------------------------------------
# 公司资料
# ---------------------------------------------------------------------------

def hk_company_display_fields(info: dict, name: str) -> List[Tuple[str, str]]:
    """公司概况展示字段（Markdown 与 LaTeX 共用）。"""
    def d8(value: Any) -> str:
        text = str(value or "")[:10]
        return text.replace("-", "") if text else "NA"

    industry = str(info.get("BELONG_INDUSTRY") or "")
    if not industry:
        industry = str(info.get("INDUSTRY_TYPE") or "").split("-")[-1]
    business = str(info.get("MAIN_BUSINESS") or info.get("ORG_PROFILE") or "").strip()
    business = re.sub(r"\s+", " ", business)
    if len(business) > 220:
        business = business[:217] + "..."

    return [
        ("公司全称", str(info.get("ORG_NAME") or name)),
        ("所属行业", industry or "NA"),
        ("成立日期", d8(info.get("FOUND_DATE"))),
        ("上市日期", d8(info.get("LISTING_DATE"))),
        ("注册地", str(info.get("REG_PLACE") or "NA")),
        ("主营业务", business or "NA"),
    ]


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def build_hk_markdown(code: str, info: dict, quote: dict,
                      metrics: Dict[int, dict], years: List[int],
                      latest_year: int, valuation: dict,
                      halfyear: Dict[int, dict],
                      dividends: Dict[int, dict], report_date: str,
                      charts: Dict[str, str], with_charts: bool,
                      price_window: str = "",
                      price_adjust: str = "前复权") -> str:
    name = quote.get("name") or info.get("SECURITY_NAME_ABBR") or norm_hk_ticker(code)
    full = full_hk_code(code)

    md = []
    md.append("# %s（%s）CM 手册" % (name, full))
    md.append("")
    md.append("> 制表日期：%s　|　报告指标日期：%s（报告日最近交易日）　|　财务指标截至：%d 年报"
              % (report_date, valuation.get("price_date") or "NA", latest_year))
    md.append("")
    md.append(base.md_table(
        ["报告指标日期", "当前股价（港元）", "当前市值（亿港元）", "静态市盈率",
         "静态市净率", "动态市盈率", "市净率"],
        [[valuation.get("price_date") or "NA",
          base.fmt_ratio(valuation.get("price")),
          base.fmt_num(valuation.get("mcap_yi"), 0),
          base.fmt_pe(valuation.get("static_pe")),
          base.fmt_pb(valuation.get("static_pb")),
          base.fmt_pe(valuation.get("dynamic_pe")),
          base.fmt_pb(valuation.get("current_pb"))]],
        [":--", "--:", "--:", "--:", "--:", "--:", "--:"]))
    md.append("")
    snap_date = valuation.get("snapshot_date") or ("%d-12-31" % latest_year)
    md.append("> 静态 PE/PB 采用 %d 年报口径；动态 PE 采用 TTM 股东应占溢利；"
              "市净率采用 %s 股东权益。当前股价/市值取报告指标日期收盘价与最新披露股本。"
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
    md.append(base.annual_table(metrics, years, fields1))
    md.append("")
    if with_charts and charts.get("profitability"):
        md.append("![盈利能力趋势](%s)" % charts["profitability"])
        md.append("")

    # ---- 2. 资产负债结构 ----
    md.append("## 二、资产负债结构（百万元，报表币种）")
    md.append("")
    fields2 = [
        ("assets", "资产总计（百万元）", "num"),
        ("parent_eq", "股东权益（百万元）", "num"),
        ("money", "现金及等价物（百万元）", "num"),
        ("current_assets", "流动资产（百万元）", "num"),
        ("noncurrent_assets", "非流动资产（百万元）", "num"),
        ("current_liab", "流动负债（百万元）", "num"),
        ("noncurrent_liab", "非流动负债（百万元）", "num"),
        ("inventory", "存货（百万元）", "num"),
        ("fixed_assets", "物业厂房及设备（百万元）", "num"),
        ("goodwill", "商誉（百万元）", "num"),
        ("short_loan", "短期贷款（百万元）", "num"),
        ("long_loan", "长期贷款（百万元）", "num"),
    ]
    md.append(base.annual_table(metrics, years, fields2))
    md.append("")
    if with_charts and charts.get("assets"):
        md.append("![资产规模趋势](%s)" % charts["assets"])
        md.append("")

    # ---- 3. 利润与现金流 ----
    md.append("## 三、利润与现金流（百万元，报表币种）")
    md.append("")
    fields3 = [
        ("revenue", "营业额（百万元）", "num"),
        ("gross_profit", "毛利（百万元）", "num"),
        ("op_profit", "经营溢利（百万元）", "num"),
        ("ebit", "息税前利润（百万元）", "num"),
        ("parent_np", "股东应占溢利（百万元）", "num"),
        ("operate_cf", "经营净现金流（百万元）", "num"),
        ("cce_add", "现金净额（百万元）", "num"),
    ]
    md.append(base.annual_table(metrics, years, fields3))
    md.append("")
    if with_charts and charts.get("cashflow"):
        md.append("![利润与现金流趋势](%s)" % charts["cashflow"])
        md.append("")

    # ---- 4. 股本与每股盈利 ----
    md.append("## 四、股本与每股盈利")
    md.append("")
    md.append(base.annual_table(metrics, years, [
        ("shares", "总股本（百万股，按 EPS 推算）", "shares"),
        ("eps", "基本每股盈利（元/股）", "ratio"),
    ]))
    md.append("")

    # ---- 5. 股价走势 ----
    md.append("## 五、股价走势")
    md.append("")
    if with_charts and charts.get("price"):
        md.append("![股价走势](%s)" % charts["price"])
        md.append("")
        if price_adjust == "前复权":
            md.append("> 股价图为月线、前复权收盘价；区间为报告日前推 10 年%s。"
                      "前复权价格已按分红送转调整。" %
                      ("（%s）" % price_window if price_window else ""))
        else:
            md.append("> 股价图为月线、不复权收盘价；区间为报告日前推 10 年%s。"
                      "前复权数据不可用，图中为实际成交价。" %
                      ("（%s）" % price_window if price_window else ""))
        md.append("")

    # ---- 6. 公司概况 ----
    md.append("## 六、公司概况")
    md.append("")
    md.append(base.md_table(
        ["项目", "内容"],
        [[label, value] for label, value in hk_company_display_fields(info, name)],
        [":--", ":--"]))
    md.append("")

    # ---- 7. 年均增长率 ----
    md.append("## 七、年均增长率")
    md.append("")
    cagr_rows = []
    for label, key in (("营业额", "revenue"), ("股东应占溢利", "parent_np"),
                       ("总资产", "assets"), ("股东权益", "parent_eq")):
        row = [label]
        for span in (10, 8, 5, 3):
            start_year = latest_year - span
            growth = base.cagr(metrics.get(start_year, {}).get(key),
                               metrics.get(latest_year, {}).get(key), span)
            row.append(base.fmt_pct(growth * 100 if growth is not None else None))
        cagr_rows.append(row)
    md.append(base.md_table(["时间段", "10年", "8年", "5年", "3年"], cagr_rows))
    md.append("")
    md.append("> 年均增长率为复合增长率（CAGR），区间终点为 %d 年；"
              "股东应占溢利为归母口径。" % latest_year)
    md.append("")

    # ---- 8/9. 半年度数据 ----
    hy_years = sorted(set(halfyear) | set(range(latest_year, latest_year - 4, -1)),
                      reverse=True)[:5]
    try:
        report_year = int(str(valuation.get("price_date") or report_date)[:4])
    except (ValueError, TypeError):
        report_year = latest_year
    if report_year > latest_year:
        hy_years = sorted(set(hy_years) | {report_year}, reverse=True)[:5]

    for index, title, key in ((8, "八、半年度营收（百万元，累计值）", "revenue"),
                              (9, "九、半年度股东应占溢利（百万元，累计值）", "parent_np")):
        md.append("## %s" % title)
        md.append("")
        rows = []
        for year in hy_years:
            item = halfyear.get(year, {})
            interim = item.get("interim", {})
            annual = item.get("annual", {})
            mid_value = base.as_float(interim.get(key))
            full_value = base.as_float(annual.get(key))
            rows.append([
                str(year),
                base.fmt_num(mid_value / 1e6 if mid_value is not None else None, 0),
                base.fmt_num(full_value / 1e6 if full_value is not None else None, 0),
            ])
        md.append(base.md_table(["年度", "中期", "全年"], rows))
        md.append("")
    md.append("> 港股通常披露年度与半年度报告；「中期」为财政年度上半年的累计值，"
              "「全年」为年度累计值。部分公司披露季度数据，本表不展示。")
    md.append("")

    # ---- 10. 每股分红 ----
    md.append("## 十、每股分红（港元）")
    md.append("")
    dividend_years = list(range(latest_year, latest_year - 4, -1))
    price = valuation.get("price")
    div_rows = []
    for year in dividend_years:
        dps = base.as_float(dividends.get(year, {}).get("dps_hkd"))
        yield_pct = (dps / price * 100
                     if dps is not None and price not in (None, 0) else None)
        div_rows.append([
            str(year),
            base.fmt_ratio(dps),
            base.fmt_pct(yield_pct),
        ])
    md.append(base.md_table(["年度", "每股派息（港元）", "股息率"], div_rows))
    md.append("")
    md.append("> 每股派息按财政年度汇总中期与末期现金分红；实物分派不计入。"
              "股息率按报告指标日期股价计算。")
    md.append("")

    # ---- 脚注 ----
    md.append("---")
    md.append("")
    md.append("**口径与免责声明**")
    md.append("")
    md.append("- 报告指标日期为 %s（报告日最近交易日）；当前股价/市值取该日收盘价与最新披露股本。"
              % (valuation.get("price_date") or "NA"))
    md.append("- 静态 PE/PB 按 %d 年报口径；动态 PE 按 TTM 股东应占溢利；"
              "市净率按 %s 股东权益。估值比率由数据源报告期 PE/PB 结合当前市值推算。"
              % (latest_year, snap_date))
    md.append("- 港股财报按公司披露币种列示（常见人民币、港元或美元），本表金额未做汇率折算；"
              "股价、市值与分红币种为港元。")
    md.append("- 数据来源：腾讯财经（港股行情、K 线、汇率）；东方财富（港股财报三表、"
              "主要指标、公司资料、分红明细、K 线兜底）。")
    md.append("- 银行、保险等金融企业的报表科目与通用模板不同，"
              "流动比率、存货、短期/长期贷款等不适用科目显示 NA。")
    md.append("- 本资料所有数据均来源于可靠的公开资料，但无法确保完全准确。如有错讹，概不负责。")
    md.append("- 本资料仅适用于个人投资者训练投资能力，未获授权，禁止一切形式的复制、转载和交易。")
    md.append("")
    return "\n".join(md)


# ---------------------------------------------------------------------------
# LaTeX 一页 PDF
# ---------------------------------------------------------------------------

def _latex_halfyear_table(halfyear: Dict[int, dict], years: List[int],
                          key: str) -> str:
    headers = ["\\textbf{年度}", "\\textbf{中期}", "\\textbf{全年}"]
    rows = []
    for year in years:
        item = halfyear.get(year, {})
        interim = item.get("interim", {})
        annual = item.get("annual", {})
        mid_value = base.as_float(interim.get(key))
        full_value = base.as_float(annual.get(key))
        rows.append([
            str(year),
            base._tex_escape(base.fmt_num(
                mid_value / 1e6 if mid_value is not None else None, 0)),
            base._tex_escape(base.fmt_num(
                full_value / 1e6 if full_value is not None else None, 0)),
        ])
    colspec = ("|>{\\centering\\arraybackslash}p{17mm}|"
               "*{2}{>{\\raggedleft\\arraybackslash}p{18mm}|}")
    return base._latex_tabular(colspec, headers, rows, 6.0, 7.0)


def _latex_hk_dividend_table(dividends: Dict[int, dict], years: List[int],
                             price: Optional[float]) -> str:
    headers = ["\\textbf{年度}", "\\textbf{每股派息（港元）}", "\\textbf{股息率}"]
    rows = []
    for year in years:
        dps = base.as_float(dividends.get(year, {}).get("dps_hkd"))
        yield_pct = (dps / price * 100
                     if dps is not None and price not in (None, 0) else None)
        rows.append([
            str(year),
            base._tex_escape(base.fmt_ratio(dps)),
            base._tex_escape(base.fmt_pct(yield_pct)),
        ])
    colspec = ("|>{\\centering\\arraybackslash}p{17mm}|"
               ">{\\raggedleft\\arraybackslash}p{21mm}|"
               ">{\\raggedleft\\arraybackslash}p{13mm}|")
    return base._latex_tabular(colspec, headers, rows, 6.3, 7.3)


def render_latex_pdf_hk(tex_path: str, name: str, full: str, info: dict,
                        valuation: dict, metrics: Dict[int, dict],
                        years: List[int], latest_year: int,
                        halfyear: Dict[int, dict], dividends: Dict[int, dict],
                        report_date: str, charts: Dict[str, str],
                        price_window: str, price_adjust: str) -> str:
    """生成一页港股 LaTeX PDF。Markdown 由调用方先落盘。"""
    out_dir = os.path.dirname(os.path.abspath(tex_path))
    os.makedirs(out_dir, exist_ok=True)

    chart_pdfs = {}
    for key in ("profitability_print", "assets_print", "cashflow_print", "price_print"):
        svg_path = charts.get(key) or charts.get(key.replace("_print", ""))
        if not svg_path or not os.path.exists(svg_path):
            continue
        pdf_path = base._svg_to_pdf(svg_path)
        chart_pdfs[key] = os.path.relpath(pdf_path, out_dir).replace(os.sep, "/")

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
        ("money", "现金及等价物", "num"),
        ("current_assets", "流动资产", "num"),
        ("noncurrent_assets", "非流动资产", "num"),
        ("current_liab", "流动负债", "num"),
        ("noncurrent_liab", "非流动负债", "num"),
        ("inventory", "存货", "num"),
        ("fixed_assets", "物业厂房及设备", "num"),
        ("goodwill", "商誉", "num"),
        ("short_loan", "短期贷款", "num"),
        ("long_loan", "长期贷款", "num"),
    ]
    fields3 = [
        ("revenue", "营业额", "num"),
        ("gross_profit", "毛利", "num"),
        ("op_profit", "经营溢利", "num"),
        ("ebit", "息税前利润", "num"),
        ("parent_np", "股东应占溢利", "num"),
        ("operate_cf", "经营净现金流", "num"),
        ("cce_add", "现金净额", "num"),
        ("shares", "总股本（百万股）", "shares"),
    ]

    dividend_years = list(range(latest_year, latest_year - 4, -1))
    hy_years = sorted(set(halfyear) | set(dividend_years), reverse=True)[:5]
    try:
        report_year = int(str(valuation.get("price_date") or report_date)[:4])
    except (ValueError, TypeError):
        report_year = latest_year
    if report_year > latest_year:
        hy_years = sorted(set(hy_years) | {report_year}, reverse=True)[:5]

    # 金额单位自适应：特大公司用亿元，普通公司用百万元
    max_num = 0.0
    for key, _label, fmt in fields2 + fields3:
        if fmt != "num":
            continue
        for year in years:
            value = metrics.get(year, {}).get(key)
            if value is not None:
                max_num = max(max_num, abs(value))
    if max_num >= 1e12:
        unit_divisor, col_width = 1e8, 9.0
    else:
        unit_divisor, col_width = 1e6, 8.5

    def include_chart(key: str) -> str:
        path = chart_pdfs.get(key)
        if not path:
            return ""
        return ("\\vspace{0mm}\\includegraphics[width=\\linewidth]{%s}"
                "\\par\\vspace{0mm}" % path)

    top_headers = ["\\textbf{报告指标日期}", "\\textbf{当前股价（港元）}",
                   "\\textbf{当前市值（亿港元）}", "\\textbf{静态PE}",
                   "\\textbf{静态PB}", "\\textbf{动态PE}", "\\textbf{市净率}"]
    top_values = [
        base._tex_escape(valuation.get("price_date") or "NA"),
        base._tex_escape(base.fmt_ratio(valuation.get("price"))),
        base._tex_escape(base.fmt_num(valuation.get("mcap_yi"), 0)),
        base._tex_escape(base.fmt_pe(valuation.get("static_pe"))),
        base._tex_escape(base.fmt_pb(valuation.get("static_pb"))),
        base._tex_escape(base.fmt_pe(valuation.get("dynamic_pe"))),
        base._tex_escape(base.fmt_pb(valuation.get("current_pb"))),
    ]
    name_tex = base._tex_escape(name)
    full_tex = base._tex_escape(full)
    snap_date = valuation.get("snapshot_date") or ("%d-12-31" % latest_year)

    footer_mid = base._tex_escape(
        "报告指标日期为 %s（报告日最近交易日）；当前股价/市值取该日收盘价与最新披露股本。"
        "静态 PE/PB 按 %d 年报口径；动态 PE 按 TTM 股东应占溢利；"
        "市净率按 %s 股东权益。"
        % (valuation.get("price_date") or "NA", latest_year, snap_date))
    footer_right = base._tex_escape(
        "数据来源：腾讯财经（港股行情、K 线、汇率）；东方财富（港股财报三表、主要指标、"
        "公司资料、分红明细、K 线兜底）。港股财报按披露币种列示，未做汇率折算。"
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

    # 顶部摘要
    tex.append(r"\noindent{\fontsize{6.2}{7.2}\selectfont"
               r"\renewcommand{\arraystretch}{1.0}")
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

    # 左栏
    tex.append(r"\sbox{\cmtable}{"
               + base._latex_annual_table(metrics, years, fields1,
                                          unit_divisor, col_width)
               + "}")
    tex.append(r"\setlength{\cmleftwidth}{\wd\cmtable}")
    tex.append(r"\noindent\begin{minipage}[t]{\dimexpr1.05\cmleftwidth\relax}"
               r"\vspace{0pt}")
    tex.append(r"\makebox[\linewidth][c]{\usebox{\cmtable}}")
    tex.append(include_chart("profitability_print"))
    tex.append(r"\sbox{\cmtable}{"
               + base._latex_annual_table(metrics, years, fields2,
                                          unit_divisor, col_width)
               + "}")
    tex.append(r"\makebox[\linewidth][c]{\usebox{\cmtable}}")
    tex.append(include_chart("assets_print"))
    tex.append(r"\sbox{\cmtable}{"
               + base._latex_annual_table(metrics, years, fields3,
                                          unit_divisor, col_width)
               + "}")
    tex.append(r"\makebox[\linewidth][c]{\usebox{\cmtable}}")
    tex.append(include_chart("cashflow_print"))
    tex.append(r"\end{minipage}%")
    tex.append(r"\hspace{0.2mm}%")

    # 右栏
    tex.append(r"\begin{minipage}[t]{\dimexpr\textwidth-1.05\cmleftwidth-0.2mm\relax}"
               r"\vspace{0pt}\setlength{\righttablewidth}{\linewidth}")
    if chart_pdfs.get("price_print"):
        tex.append(r"\includegraphics[width=\linewidth]{%s}\par"
                   % chart_pdfs["price_print"])
        tex.append(r"\vspace{-1.4mm}{\centering\fontsize{5.5}{6.5}\selectfont "
                   r"股价走势（月线，%s）\\[0.1mm] %s\par}"
                   % (base._tex_escape(price_adjust),
                      base._tex_escape(price_window)))
        tex.append(r"\vspace{0.2mm}")
    tex.append(r"\vspace{0.4mm}{\centering\textbf{\fontsize{10}{12}\selectfont %s}"
               r"\par}" % name_tex)
    tex.append(_latex_company_table_hk(info, name))
    tex.append(r"\vspace{0.7mm}\textbf{\fontsize{6.5}{7.5}\selectfont 年均增长率}"
               r"\par\vspace{0.2mm}")
    tex.append(base._fit_width(base._latex_cagr_table(metrics, latest_year)))
    tex.append(r"\vspace{0.7mm}\textbf{\fontsize{6.5}{7.5}\selectfont "
               r"半年度营收（百万元，累计值）}\par\vspace{0.2mm}")
    tex.append(base._fit_width(_latex_halfyear_table(halfyear, hy_years, "revenue")))
    tex.append(r"\vspace{0.7mm}\textbf{\fontsize{6.5}{7.5}\selectfont "
               r"半年度股东应占溢利（百万元，累计值）}\par\vspace{0.2mm}")
    tex.append(base._fit_width(_latex_halfyear_table(halfyear, hy_years, "parent_np")))
    tex.append(r"\vspace{0.7mm}\textbf{\fontsize{6.5}{7.5}\selectfont "
               r"每股分红（港元）}\par\vspace{0.2mm}")
    tex.append(base._fit_width(_latex_hk_dividend_table(
        dividends, dividend_years, valuation.get("price"))))
    tex.append(r"\end{minipage}")

    # 说明信息
    tex.append(r"\vspace{1.5mm}\hrule\vspace{1mm}")
    tex.append(r"{\fontsize{5}{6}\selectfont")
    tex.append(r"\noindent\begin{minipage}[t]{0.46\textwidth}%s\end{minipage}\hfill"
               % footer_mid)
    tex.append(r"\begin{minipage}[t]{0.52\textwidth}%s\end{minipage}" % footer_right)
    tex.append(r"}")
    tex.append(r"\end{document}")

    with open(tex_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(tex))

    tex_name = os.path.basename(tex_path)
    try:
        result = base.subprocess.run(
            ["xelatex", "-interaction=nonstopmode", "-halt-on-error", tex_name],
            cwd=out_dir, capture_output=True, timeout=180)
    except FileNotFoundError:
        raise DataError("未找到 xelatex；请安装 TeX Live / TinyTeX")
    except base.subprocess.SubprocessError as exc:
        raise DataError("xelatex 执行失败：%s" % exc)
    if result.returncode != 0:
        log_text = result.stdout.decode("utf-8", "replace")
        raise DataError("xelatex 编译失败：\n%s" % log_text[-1500:])

    pdf_path = os.path.splitext(tex_path)[0] + ".pdf"
    pdf_path = base.crop_pdf_to_content(pdf_path)
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(pdf_path)
        if doc.page_count != 1:
            log("  警告：PDF 共 %d 页，未达到一页要求" % doc.page_count)
        else:
            w_mm = doc[0].rect.width / 72.0 * 25.4
            h_mm = doc[0].rect.height / 72.0 * 25.4
            log("  PDF 页面尺寸：%.1f x %.1f mm（高度按内容自适应）" % (w_mm, h_mm))
    except Exception:  # noqa: BLE001
        pass
    return pdf_path


def _latex_company_table_hk(info: dict, name: str) -> str:
    lines = ["{\\fontsize{6.3}{7.6}\\selectfont",
             "\\begin{tabularx}{\\righttablewidth}{|>{\\raggedright\\arraybackslash}p{15mm}|"
             ">{\\raggedright\\arraybackslash}X|}",
             "\\hline"]
    for label, value in hk_company_display_fields(info, name):
        lines.append("%s & %s \\\\" % (base._tex_escape(label),
                                       base._tex_escape(value)))
        lines.append("\\hline")
    lines.append("\\end{tabularx}}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def generate_hk(code: str, out_path: Optional[str], report_date: str,
                with_charts: bool, current_year: Optional[int] = None,
                price_date: Optional[str] = None,
                make_pdf: bool = False) -> str:
    ticker = norm_hk_ticker(code)
    full = full_hk_code(code)
    log("== 正在生成 %s CM 手册（港股） ==" % full)

    log("[1/7] 获取行情与公司资料 ...")
    try:
        quote = tencent_hk_quote(ticker)
    except Exception as exc:  # noqa: BLE001
        log("  腾讯港股行情失败（继续使用财报数据）：%s" % exc)
        quote = {"name": ticker, "price": None, "mcap_yi": None, "shares": None}
    try:
        info = fetch_hk_profile(ticker)
    except Exception as exc:  # noqa: BLE001
        log("  港股公司资料获取失败：%s" % exc)
        info = {}
    info["SECURITY_NAME_ABBR"] = (
        info.get("SECURITY_NAME_ABBR") or quote.get("name") or ticker)

    log("[2/7] 获取港股主要指标与年度 ...")
    try:
        main_rows, main_annual = fetch_hk_main(ticker)
    except Exception as exc:  # noqa: BLE001
        log("  港股主要指标获取失败：%s" % exc)
        main_rows, main_annual = [], {}
    main_latest_row = main_annual.get(max(main_annual)) if main_annual else {}
    fy_end = _norm_fy_end(main_latest_row.get("FISCAL_YEAR")
                          or info.get("YEAR_SETTLE_DAY"))

    log("[3/7] 获取港股财务报表三表 ...")
    try:
        balance_by_date, balance_types = fetch_hk_statement(ticker, "BALANCE")
        income_by_date, income_types = fetch_hk_statement(ticker, "INCOME")
        cashflow_by_date, cashflow_types = fetch_hk_statement(ticker, "CASHFLOW")
    except Exception as exc:  # noqa: BLE001
        raise DataError("港股财务报表获取失败：%s" % exc)

    balance_annual = _annual_map(balance_by_date, balance_types, fy_end)
    income_annual = _annual_map(income_by_date, income_types, fy_end)
    cashflow_annual = _annual_map(cashflow_by_date, cashflow_types, fy_end)
    annual = build_hk_annual(balance_annual, income_annual,
                             cashflow_annual, main_annual)
    if not annual:
        raise DataError("没有取到任何年度财务数据，无法生成手册")

    all_years = sorted(annual)
    latest_year = max(all_years)
    if current_year is not None:
        latest_year = current_year
    display_years = list(range(latest_year - 10, latest_year + 1))
    fetch_years = list(range(latest_year - 11, latest_year + 1))
    metrics = build_hk_metrics(annual, fetch_years)

    # 最新总股本优先用行情快照
    shares_now = quote.get("shares") or base.as_float(
        main_annual.get(latest_year, {}).get("ISSUED_COMMON_SHARES"))
    if shares_now and latest_year in metrics:
        metrics[latest_year]["shares"] = shares_now
        if not metrics[latest_year].get("eps") and metrics[latest_year].get("parent_np"):
            metrics[latest_year]["eps"] = metrics[latest_year]["parent_np"] / shares_now

    log("[4/7] 获取报告指标日期 %s 的收盘价 ..."
        % (price_date or report_date))
    price_asof = price_date or report_date
    current_price, current_date = hk_price_on_or_before(ticker, price_asof)
    if current_price is None:
        log("  未取到 %s 当日或之前的价格，退回到 %d 年末收盘价"
            % (price_asof, latest_year))
        current_price, current_date = hk_price_on_year_end(ticker, latest_year)
    if current_price is None:
        current_price = quote.get("price")
        current_date = current_date or "最新交易日"
        log("  仍未取到 K 线，改用腾讯最新行情价：%s" % (current_price or "NA"))

    # TTM 与最新一期净资产
    ttm_np, ttm_period = build_hk_ttm(income_by_date, income_types,
                                      fy_end, current_date or price_asof)
    latest_eq, eq_date = latest_hk_equity(balance_by_date,
                                          current_date or price_asof)
    valuation = build_hk_valuation(
        main_latest_row, metrics.get(latest_year, {}), ttm_np, latest_eq,
        quote, current_price, shares_now)
    valuation["price_date"] = current_date or price_asof
    valuation["snapshot_date"] = eq_date or ("%d-12-31" % latest_year)
    if ttm_period:
        log("  TTM 报告期：%s" % ttm_period)

    log("[5/7] 获取分红与半年度数据 ...")
    try:
        fx = fetch_hk_fx()
    except Exception as exc:  # noqa: BLE001
        log("  汇率获取失败：%s" % exc)
        fx = {"hkd_per_cny": 1.176, "hkd_per_usd": 7.80}
    try:
        dividends = fetch_hk_dividends(ticker, fx)
    except Exception as exc:  # noqa: BLE001
        log("  分红数据获取失败：%s" % exc)
        dividends = {}
    halfyear = build_hk_halfyear(income_by_date, income_types,
                                 fy_end, current_date or price_asof)

    # 输出路径
    name = info.get("SECURITY_NAME_ABBR") or quote.get("name") or ticker
    if not out_path:
        out_path = os.path.join("output",
                                "%s%s_CM 手册.md" % (base.safe_name(name), full))
    out_dir = os.path.dirname(os.path.abspath(out_path))
    charts_dir = os.path.join(out_dir, "charts")
    os.makedirs(out_dir, exist_ok=True)

    charts: Dict[str, str] = {}
    price_window = ""
    price_adjust = "前复权"
    if make_pdf:
        with_charts = True
    if with_charts:
        log("[6/7] 生成图表 ...")
        os.makedirs(charts_dir, exist_ok=True)
        chart_base = ticker

        # 1) 盈利能力
        p = os.path.join(charts_dir, "%s_profitability.svg" % chart_base)
        labels = [str(y) for y in display_years]
        prof_series = [
            ("净资产收益率", [metrics.get(y, {}).get("roe") for y in display_years]),
            ("总资产收益率", [metrics.get(y, {}).get("roa") for y in display_years]),
            ("销售净利率", [metrics.get(y, {}).get("net_margin") for y in display_years]),
        ]
        base.render_line_svg(p, "盈利能力趋势（%）", labels, prof_series,
                             "百分比（%）", y_fmt="{:.1f}%")
        charts["profitability"] = os.path.relpath(p, out_dir).replace(os.sep, "/")
        if make_pdf:
            pp = os.path.join(charts_dir, "%s_profitability_print.svg" % chart_base)
            base.render_line_svg(pp, "", labels, prof_series, "百分比（%）",
                                 width=900, height=170, y_fmt="{:.1f}%",
                                 bottom_margin=34)
            charts["profitability_print"] = pp

        # 2) 资产规模
        p = os.path.join(charts_dir, "%s_assets.svg" % chart_base)
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
        base.render_line_svg(p, "资产规模趋势（百万元）", labels, asset_series,
                             "百万元", y_fmt="{:.0f}")
        charts["assets"] = os.path.relpath(p, out_dir).replace(os.sep, "/")
        if make_pdf:
            pp = os.path.join(charts_dir, "%s_assets_print.svg" % chart_base)
            base.render_line_svg(pp, "", labels, asset_series, "百万元",
                                 width=900, height=170, y_fmt="{:.0f}",
                                 bottom_margin=34)
            charts["assets_print"] = pp

        # 3) 利润与现金流
        p = os.path.join(charts_dir, "%s_cashflow.svg" % chart_base)
        cf_series = [
            ("息税前利润", [((metrics.get(y, {}).get("ebit") or 0) / 1e6)
                      if metrics.get(y, {}).get("ebit") is not None else None
                      for y in display_years]),
            ("股东应占溢利", [((metrics.get(y, {}).get("parent_np") or 0) / 1e6)
                        if metrics.get(y, {}).get("parent_np") is not None else None
                        for y in display_years]),
            ("经营净现金流", [((metrics.get(y, {}).get("operate_cf") or 0) / 1e6)
                       if metrics.get(y, {}).get("operate_cf") is not None else None
                       for y in display_years]),
        ]
        base.render_line_svg(p, "利润与现金流趋势（百万元）", labels, cf_series,
                             "百万元", y_fmt="{:.0f}")
        charts["cashflow"] = os.path.relpath(p, out_dir).replace(os.sep, "/")
        if make_pdf:
            pp = os.path.join(charts_dir, "%s_cashflow_print.svg" % chart_base)
            base.render_line_svg(pp, "", labels, cf_series, "百万元",
                                 width=900, height=170, y_fmt="{:.0f}",
                                 bottom_margin=34)
            charts["cashflow_print"] = pp

        # 4) 股价（月线，前复权；报告日前推 10 年）
        try:
            chart_start, chart_end = base.report_window(report_date)
            krows, price_adjust = _price_chart_rows(ticker, chart_start, chart_end)
            if krows:
                p = os.path.join(charts_dir, "%s_price.svg" % chart_base)
                x_labels = []
                for i, row in enumerate(krows):
                    year = row["date"][:4]
                    if i == 0 or year != krows[i - 1]["date"][:4]:
                        x_labels.append(year)
                    else:
                        x_labels.append("")
                price_series = [
                    ("前复权收盘价" if price_adjust == "前复权" else "收盘价",
                     [r["close"] for r in krows])]
                price_title = ("股价走势（%s ~ %s，%s）"
                               % (krows[0]["date"][:7], krows[-1]["date"][:7],
                                  price_adjust))
                price_window = "%s ~ %s" % (chart_start, chart_end)
                base.render_line_svg(p, price_title, x_labels, price_series,
                                     "股价（港元）", y_fmt="{:.2f}", line_width=3.5)
                charts["price"] = os.path.relpath(p, out_dir).replace(os.sep, "/")
                if make_pdf:
                    non_empty = [i for i, label in enumerate(x_labels) if label]
                    print_x_labels = [""] * len(x_labels)
                    for pos, idx in enumerate(non_empty):
                        if pos % 2 == 0 or idx == non_empty[-1]:
                            print_x_labels[idx] = x_labels[idx]
                    pp = os.path.join(charts_dir,
                                      "%s_price_print.svg" % chart_base)
                    base.render_line_svg(pp, "", print_x_labels, price_series,
                                         "股价（港元）", width=480, height=290,
                                         y_fmt="{:.2f}", bottom_margin=32,
                                         line_width=3.5)
                    charts["price_print"] = pp
        except Exception as exc:  # noqa: BLE001
            log("  股价图生成失败：%s" % exc)

    log("[7/7] 写入 Markdown ...")
    md = build_hk_markdown(ticker, info, quote, metrics, display_years,
                           latest_year, valuation, halfyear, dividends,
                           report_date, charts, with_charts, price_window,
                           price_adjust)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(md)
    log("已生成 Markdown：%s" % out_path)

    if make_pdf:
        log("[8/8] 生成一页 LaTeX PDF ...")
        tex_path = os.path.splitext(out_path)[0] + ".tex"
        pdf_path = render_latex_pdf_hk(
            tex_path, name, full, info, valuation, metrics, display_years,
            latest_year, halfyear, dividends, report_date, charts,
            price_window, price_adjust)
        log("已生成 PDF：%s" % pdf_path)
    return out_path


def main() -> None:
    global QUIET
    parser = argparse.ArgumentParser(
        description="CM 手册生成器（港股版）：腾讯行情/K线 + 东方财富港股财报/公司资料/分红")
    parser.add_argument("code", help="港股代码，如 00700 / 700 / HK00700 / 00700.HK")
    parser.add_argument("-o", "--out",
                        help="输出 Markdown 路径（默认 output/<名称><代码>_CM 手册.md）")
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
        generate_hk(args.code, args.out, args.report_date, not args.no_charts,
                    args.current_year, args.price_date, args.pdf)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write("生成失败：%s\n" % exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
