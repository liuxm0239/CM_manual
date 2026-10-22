# CM 手册生成器

参考 `template/南山铝业600219.SH_CM 手册.pdf`  “诚明阁“的版式，生成 A 股公司的 **Markdown CM 手册** 与 **一页 PDF 报告**。

- 输入一个股票代码，自动抓取行情、财务三表、公司资料、分红等数据。
- 先输出 Markdown（含 SVG 图表），再输出 LaTeX 排版的 PDF（A4 宽度 210mm、页面高度按内容自适应）。
- 报告内容：11 年核心财务指标、资产负债结构、利润与现金流、股价走势、公司概况、年均增长率、季度营收/净利润、每股分红。

---

## 快速开始

```bash
# 1. 安装 Python 依赖
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. 确认 PDF 工具已安装
xelatex --version
rsvg-convert --version

# 3. 生成 Markdown
python3 scripts/generate_cm_manual.py 600219.SH

# 4. 生成 Markdown + 一页 PDF
python3 scripts/generate_cm_manual.py 600219.SH --report-date 2026/05/06 --pdf
```

### 港股版

港股使用独立脚本 `scripts/generate_cm_manual_hk.py`，用法与 A 股版一致：

```bash
# 只生成 Markdown
python3 scripts/generate_cm_manual_hk.py 00700

# 指定报告指标日期并生成一页 PDF
python3 scripts/generate_cm_manual_hk.py 00700.HK --report-date 2026/10/22 --pdf

# 其他港股
python3 scripts/generate_cm_manual_hk.py 01398 --pdf     # 工商银行
python3 scripts/generate_cm_manual_hk.py 09988 --pdf     # 阿里巴巴-W
```

港股版口径：

- 数据源：腾讯财经（行情、K 线、汇率）+ 东方财富（港股财报三表、主要指标、公司资料、分红明细）。
- 财报为年度 + 半年度口径；港股没有「扣非净利润」，利润表使用「股东应占溢利」，毛利率直接采用披露的「毛利」。
- 财报金额按公司披露币种列示（常见人民币、港元、美元），不做汇率折算；股价、市值、分红统一为港元。
- 分红按财政年度汇总中期/末期现金分红，展示每股派息（港元）与按当前股价计算的股息率。

---

## 功能

### 1. Markdown 报告

- 顶部概览：代码/名称、报告指标日期、当前股价、当前市值、静态 PE/PB、动态 PE、市净率。
- 左栏核心表：
  - 盈利能力与偿债能力（ROE、ROA、资产负债率、毛利率、净利率、流动比率、总资产周转率）
  - 资产负债结构（资产、股东权益、货币资金、流动/非流动资产与负债、存货、固定资产、商誉、短期/长期贷款）
  - 利润与现金流（营业收入、营业利润、EBIT、扣非净利润、归母净利润、经营净现金流、现金及等价物净增、总股本）
- 三张趋势图：盈利能力、资产规模、利润与现金流。
- 右栏：
  - 月线、前复权股价图（报告日前推 10 年）
  - 公司概况（全称、城市、成立/上市日期、行业、主营业务）
  - 年均增长率（10/8/5/3 年 CAGR）
  - 季度营收、季度净利润（当前报告年份 + 最近 4 个完整年度）
  - 每股分红（现金、送股、转增）

### 2. 一页 PDF

- 使用 LaTeX（`ctexart` + `xeCJK` + `PingFang SC`）排版。
- 版式对照原模板：顶部摘要栏、左侧年度表 + 三张趋势图、右侧股价图 + 公司资料 + 季度/分红表。
- 左侧三张表统一宽度；三张图宽度为表格的 105%，高度约为表格图表的 60%。
- 右侧所有表格统一宽度。
- 图表文字加粗；使用色盲友好的 Okabe-Ito 配色；股价行情线加粗。
- 页面宽度约 210mm，高度按内容自动裁剪（不再固定 A4 297mm）。
- 说明信息紧跟正文内容，不再固定为页面脚注。

---

## 报告口径

### “当期”/报告指标日期

- `--price-date` 指定报告指标日期，默认等于 `--report-date`（制表日期）。
- 取该日期当天或之前最近一个交易日的收盘价。
- 当前市值 = 该日收盘价 × 最新披露总股本。
- 静态 PE/PB 用最新年报归母净利润/归母股东权益。
- 动态 PE 用 TTM 归母净利润（最近四个季度滚动）。
- 市净率用最新一期归母股东权益（MRQ）。

### 核心指标

| 指标            | 计算方式                                                  |
| --------------- | --------------------------------------------------------- |
| 净资产收益率    | 归母净利润 / 平均归母股东权益                             |
| 总资产收益率    | EBIT / 平均总资产                                         |
| 息税前利润 EBIT | 利润总额 + 利息费用 − 利息收入（金融企业直接用利润总额） |
| 资产负债率      | 总负债 / 总资产                                           |
| 销售毛利率      | (营业收入 − 营业成本) / 营业收入                         |
| 销售净利率      | 净利润 / 营业收入                                         |
| 流动比率        | 流动资产 / 流动负债                                       |
| 总资产周转率    | 营业收入 / 平均总资产                                     |
| 年均增长率      | (期末值 / 期初值)^(1/N) − 1                              |

> 历史财务数据采用数据源当前披露口径，可能包含后续年度追溯调整；与当年原始年报披露值可能存在差异。

### 季度数据

- 表格包含“报告指标日期所在年份”+ 最近 4 个完整年度。
- 只展示报告指标日期之前已经结束的季度；未来季度显示 `NA`。
- 营收和净利润均为累计值（一季度、二季度累计、三季度累计、四季度全年）。

---

## 数据源

遵循“能用腾讯/交易所/新浪，就不用东财”的原则：

| 数据                                     | 主来源             | 说明                                                                   |
| ---------------------------------------- | ------------------ | ---------------------------------------------------------------------- |
| 实时行情、市值、PE/PB 快照               | **腾讯财经** | `qt.gtimg.cn`                                                        |
| 日/周/月 K 线、报告指标日期收盘价        | **腾讯财经** | `web.ifzq.gtimg.cn` / 备用入口                                       |
| 财务三表（资产负债表/利润表/现金流量表） | **新浪财经** | `CompanyFinanceService.getFinanceReport2022`，一次取约 12 年季度数据 |
| 公司全称、城市、成立/上市日期、主营业务  | **新浪财经** | `vCI_CorpInfo` 公司资料页                                            |
| 所属行业（申万）                         | **新浪财经** | `vCI_CorpOtherInfo` 行业页                                           |
| 扣非归母净利润                           | **东方财富** | 新浪三表无此科目，东财独有                                             |
| 按报告期归档的分红送转                   | **东方财富** | `RPT_SHAREBONUS_DET`，按 `REPORT_DATE` 对应年度                    |
| 保险等缺失的现金净增/经营净现金流补充    | **东方财富** | `RPT_F10_FINANCE_{G,B,S,I}CASHFLOW`                                  |
| 北交所 K 线                              | **东方财富** | 腾讯 K 线不支持北交所，使用 `push2his`                               |
| 新浪三表/公司资料失败时的整表兜底        | **东方财富** | `RPT_F10_FINANCE_{G,B,S,I}_{BALANCE,INCOME,CASHFLOW}`                |

### 东财限流

所有东方财富请求统一走 `em_get()`：

- 串行请求，最小间隔约 0.8 秒 + 随机抖动。
- 复用 HTTP Session。
- 连接失败/429/5xx 自动重试。
- 北交所 K 线使用 `curl_json()`，同样经过 `_sleep_between_em()` 节流。

### mootdx

本脚本**不依赖 mootdx**。其行情命令 2026-09 起已失效（#52），财务快照/F10 仍可用，但本报告所需的 12 年财务三表由新浪提供，mootdx 不参与。

---

## 安装指南

### 环境要求

| 依赖               | 版本/说明                                                                                                                                    |
| ------------------ | -------------------------------------------------------------------------------------------------------------------------------------------- |
| Python             | 3.9+                                                                                                                                         |
| requests           | 任意较新版本                                                                                                                                 |
| PyMuPDF (`fitz`) | 用于 PDF 页面自适应裁剪与页数检查；未安装时 PDF 保持 A4                                                                                      |
| xelatex            | TeX Live 2023+ / TinyTeX                                                                                                                     |
| LaTeX 宏包         | `ctex`、`xeCJK`、`fontspec`、`geometry`、`graphicx`、`tabularx`、`array`、`multirow`、`makecell`、`xcolor`、`colortbl` |
| rsvg-convert       | librsvg，用于把 SVG 图表转成 PDF                                                                                                             |
| 中文字体           | 默认 `PingFang SC`；Linux/Windows 可用 `CM_CJK_FONT` 指定其他字体                                                                        |
| curl               | 北交所 K 线兜底需要；macOS/Linux 自带，Windows 10+ 自带                                                                                      |

### macOS

```bash
# 1. Python 依赖
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. rsvg-convert
brew install librsvg

# 3a. TeX：完整 MacTeX（体积较大）
brew install --cask mactex-no-gui

# 3b. 或使用 TinyTeX
curl -sL "https://yihui.org/tinytex/install-bin-unix.sh" | sh
tlmgr install ctex xecjk fontspec geometry graphicx tabularx array multirow makecell xcolor colortbl

# 4. 验证
xelatex --version
rsvg-convert --version
python3 -c "import requests, fitz; print('ok')"
```

默认中文字体 `PingFang SC` 是 macOS 自带字体。如果提示找不到字体，可指定其他字体：

```bash
export CM_CJK_FONT="Songti SC"
```

### Linux（Debian/Ubuntu）

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip \
  librsvg2-bin texlive-xetex texlive-lang-chinese fonts-noto-cjk

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 把默认字体切换为 Linux 上可用的中文字体
export CM_CJK_FONT="Noto Sans CJK SC"

xelatex --version
rsvg-convert --version
python3 -c "import requests, fitz; print('ok')"
```

如果缺少 `makecell` 等宏包：

```bash
sudo apt-get install -y texlive-latex-extra
# 或者
tlmgr install makecell multirow
```

### Windows

1. 安装 Python 3.9+。
2. 安装 TeX Live 或 MiKTeX（需要 `xelatex`）。
3. 安装 librsvg：
   - 推荐使用 WSL；或在 MSYS2 中执行 `pacman -S mingw-w64-x86_64-librsvg`。
   - 确保 `rsvg-convert.exe` 在 PATH 中。
4. 安装 Python 依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

5. 设置中文字体（示例为微软雅黑）：

```powershell
$env:CM_CJK_FONT="Microsoft YaHei"
```

6. 验证：

```powershell
xelatex --version
rsvg-convert --version
python -c "import requests, fitz; print('ok')"
```

### 字体说明

脚本默认使用：

```tex
\setCJKmainfont{PingFang SC}
\setCJKsansfont{PingFang SC}
\setCJKmonofont{PingFang SC}
```

可通过环境变量覆盖：

```bash
export CM_CJK_FONT="Noto Sans CJK SC"     # Linux
export CM_CJK_FONT="Microsoft YaHei"      # Windows
export CM_CJK_FONT="Songti SC"            # macOS 备选
```

查看系统可用中文字体：

```bash
fc-list :lang=zh family | sort -u
```

---

## 使用说明

### 命令

```bash
python3 sripts/generate_cm_manual.py <股票代码> [选项]
```

股票代码支持：

- `600219`
- `600219.SH`
- `SH600219`
- `920982.BJ`
- `000001.SZ`

### 参数

| 参数               | 说明                                                                 |
| ------------------ | -------------------------------------------------------------------- |
| `code`           | 股票代码（必填）                                                     |
| `-o, --out`      | 输出 Markdown 路径；默认 `output/<名称><代码>_CM 手册.md`          |
| `--report-date`  | 制表日期，默认今天，格式 `YYYY/MM/DD` 或 `YYYY-MM-DD`            |
| `--price-date`   | 报告指标日期（当前股价/市值/PE/PB 快照日），默认同 `--report-date` |
| `--current-year` | 指定最新年报年度（一般不需要）                                       |
| `--no-charts`    | 不生成 SVG 图表；使用 `--pdf` 时会自动强制生成                     |
| `--pdf`          | 在 Markdown 之后生成 `.tex` 和一页 PDF                             |
| `-q, --quiet`    | 静默模式                                                             |

### 示例

```bash
# 只生成 Markdown
python3 sripts/generate_cm_manual.py 600219.SH

# 按模板日期生成 Markdown + PDF（南山铝业）
python3 sripts/generate_cm_manual.py 600219.SH --report-date 2026/05/06 --pdf

# 中国平安
python3 sripts/generate_cm_manual.py 601318 --report-date 2026/10/18 --pdf

# 指定报告指标日期回看年末快照
python3 sripts/generate_cm_manual.py 600219.SH --report-date 2026/05/06 --price-date 2025-12-31 --pdf

# 不生成图表
python3 sripts/generate_cm_manual.py 000895 --no-charts
```

### 输出文件

```text
output/
├── 南山铝业600219.SH_CM 手册.md
├── 南山铝业600219.SH_CM 手册.tex
├── 南山铝业600219.SH_CM 手册.pdf
└── charts/
    ├── 600219_profitability.svg
    ├── 600219_assets.svg
    ├── 600219_cashflow.svg
    ├── 600219_price.svg
    ├── 600219_profitability_print.svg
    ├── 600219_assets_print.svg
    ├── 600219_cashflow_print.svg
    ├── 600219_price_print.svg
    └── 600219_*_print.pdf
```

- `.md`：Markdown 报告，引用 `charts/*.svg`。
- `.tex`：LaTeX 源文件，可重新编译。
- `.pdf`：一页 PDF，页面高度按内容裁剪。
- `charts/*.svg`：Markdown 使用的网页图表。
- `charts/*_print.svg` / `*.pdf`：PDF 使用的打印图表。

> 先生成 Markdown，再生成 PDF；PDF 失败不会影响已保存的 Markdown。

---

## PDF 排版说明

- 纸张宽度：约 210mm。
- 纸张高度：按内容自适应，脚本用 PyMuPDF 扫描内容边界后裁剪。
- 左栏：三张年度表 + 三张趋势图，无分节标题，间距最小。
- 左栏表格：统一缩放到 130mm。
- 左栏图表：宽度为表格宽度的 105%，高度约为 60%。
- 右栏：股价图、公司概况、年均增长率、季度营收、季度净利润、每股分红。
- 右栏表格：统一使用 `\righttablewidth`，左右边界一致。
- 图表：Okabe-Ito 色盲友好配色；标题/坐标轴/图例加粗；股价线宽度 3.5。
- 说明信息：紧跟正文内容，两栏排版，不再固定在页面底部。

---

## 项目结构

```text
CM/
├── README.md
├── requirements.txt
├── sripts/
│   └── generate_cm_manual.py
├── template/
│   └── 南山铝业600219.SH_CM 手册.pdf
└── output/
    ├── ..._CM 手册.md
    ├── ..._CM 手册.tex
    ├── ..._CM 手册.pdf
    └── charts/
```

---

## 故障排查

### `xelatex: command not found`

安装 TeX Live / TinyTeX，并确保 `xelatex` 在 PATH 中。

```bash
# macOS TinyTeX
curl -sL "https://yihui.org/tinytex/install-bin-unix.sh" | sh
# Linux
sudo apt-get install -y texlive-xetex texlive-lang-chinese
```

### `! LaTeX Error: File 'ctex.sty' not found`

安装中文 LaTeX 支持：

```bash
tlmgr install ctex xecjk fontspec
# Linux 也可
sudo apt-get install -y texlive-lang-chinese
```

### `fontspec Error: The font "PingFang SC" cannot be found`

当前系统没有 PingFang SC。安装中文字体或指定环境变量：

```bash
export CM_CJK_FONT="Noto Sans CJK SC"    # Linux
export CM_CJK_FONT="Microsoft YaHei"     # Windows
```

### `rsvg-convert: command not found`

```bash
brew install librsvg          # macOS
sudo apt-get install librsvg2-bin   # Debian/Ubuntu
```

### `ModuleNotFoundError: No module named 'fitz'`

```bash
pip install pymupdf
```

未安装 PyMuPDF 时，PDF 仍会生成，但页面会保持 A4 高度，且无法自动裁剪。

### `ModuleNotFoundError: No module named 'requests'`

```bash
pip install -r requirements.txt
```

### 东方财富接口 403 / 返回空

- 东方财富有 IP 风控，脚本已内置串行限流与重试。
- 大部分数据来自腾讯/新浪，东财只用于扣非、分红、北交所 K 线等独有数据；东财失败时对应字段可能显示 `NA`。
- 稍后重试，或降低请求频率。

### 腾讯 K 线返回空

- 脚本会尝试三个腾讯 K 线入口，并回退到东方财富 K 线。
- 北交所只支持东方财富 K 线兜底。

### PDF 超过 1 页

脚本会输出警告。可尝试：

- 缩短 `--report-date` 与 `--price-date` 的跨度；
- 减少图表数量（修改脚本中的图表生成逻辑）；
- 调整字号或图表高度。

当前默认参数下，南山铝业和中国平安均能保持 1 页。

### 中文显示为方块/乱码

- 确认 `CM_CJK_FONT` 指向已安装的中文字体。
- Linux 安装 `fonts-noto-cjk`，Windows 使用 `Microsoft YaHei`，macOS 使用 `PingFang SC`。
- `rsvg-convert` 依赖 fontconfig；安装字体后重新生成图表。

---

## 免责声明

- 本工具仅用于个人投资研究与学习。
- 所有数据来自腾讯、新浪、东方财富等公开接口，不保证完整性、准确性与实时性。
- 报告中的估值、增长率等指标为程序按固定口径计算，可能与数据源或其他软件存在差异。
- 本工具不构成任何投资建议。据此操作，风险自负。
- 原 PDF 模板仅作版式参考；模板版权归原作者所有。
