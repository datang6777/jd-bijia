# 京东全量比价（jd-bijia）

只读原 Excel，按「副本比价流程」规则在京东搜/打开商品，写入券后价与匹配状态。  
结果写到新文件；`jsonl` 做断点续跑。登录只能在你本机完成，仓库不存放也不发明京东私有接口凭证。

## 环境

```bash
cd /path/to/jd-bijia
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

macOS 上若用 `--via chrome`（默认），需本机已安装 Google Chrome，并已在浏览器里登录京东。

## 输入表路径（不再写死 Mac 桌面）

优先级：

1. 命令行 `--input /绝对或相对路径.xlsx`
2. 环境变量 `BIJIA_INPUT`
3. 仓库根目录 `全量比价.xlsx`（若存在）
4. `~/Desktop` 或 `~/桌面` 下的 `全量比价.xlsx` / `副本全量比价1.xlsx` 等候选

默认输出：

- `output/比价结果.jsonl`（续跑依据；「失败待重试」**不会**算作完成）
- `output/比价结果.xlsx`（由 jsonl 导出，便于人工看）

原表里已有「京东价」的行会被跳过；未填价且 jsonl 里还不是终态（已匹配 / 待复核 / 京东无）的行会继续跑。

## 何时必须登录

| 场景 | 做法 |
|------|------|
| 要读**券后价**、或搜索被拦 | 本机执行一次 `python run.py --login`，或保持 Chrome 已登录京东并用默认 `--via chrome` |
| 仅 `--stats` 统计 | 不需要登录 |
| 已有链接但页面要求登录才出价格 | 会记「失败待重试」；登录后再跑同一命令即可 |

登录态保存在 `chrome-profile/jd-state.json`（http 模式）。不要把该文件提交进 Git。

## 推荐：完成全表的批次策略

表较大（约几十 MB，多含图片），**不要**一次无限制狂跑到被频控。建议：

```bash
# 0）看还剩多少（不访问京东）
python run.py --stats

# 1）登录一次（本机弹窗）
python run.py --login

# 2）先小批量试跑，例如 20 条
python run.py --limit 20 --pick sequential

# 3）打开 output/比价结果.xlsx，人工看「待复核」
# 4）再跑下一批；「失败待重试」会自动重试，终态会跳过
python run.py --limit 50 --pick sequential

# 5）失败多或出现验证码时：停一下、必要时重新 --login，再继续
python run.py --limit 50

# 6）确认流程稳定后，跑完所有未完成（仍建议分多次，或隔夜分批）
python run.py --limit 0 --pick sequential
```

### 用仓库内工作簿

```bash
python run.py --input ./全量比价.xlsx --limit 20 --pick sequential
# 或
export BIJIA_INPUT="$(pwd)/全量比价.xlsx"
python run.py --stats
python run.py --limit 20
```

### 用 Mac 桌面副本

```bash
python run.py --input "$HOME/Desktop/副本全量比价1.xlsx" --limit 20 --pick sequential
# 中文「桌面」目录：
python run.py --input "$HOME/桌面/全量比价.xlsx" --limit 20
```

### 续跑与失败重试

- 同一 `--jsonl` 再次执行即可续跑。
- 状态为「失败待重试」的 SKU **会再次进入队列**；已匹配 / 待复核 / 京东无 **会跳过**。
- 换输出文件名时请同时改 `--jsonl`，否则续跑状态对不上。

```bash
python run.py --limit 0 --jsonl output/比价结果.jsonl --output output/比价结果.xlsx
```

## 匹配规则（与流程文档对齐的要点）

实现见 `bijia/match.py` / `bijia/spec.py`，对应 `副本比价流程.docx`：

- 店铺优先：京东大药房等 > 自营旗舰 > 三方（同规格时）
- 品牌/通用名/规格要对上；数量可换算（如 20 片×3≈60 片），**剂量/克重不可硬乘**
- 多盒装按名称外包装倍数换算
- 对不上则「京东无」；页面/风控/未登录导致读不到价则「失败待重试」，不当成「京东无」

## 架构

| 模块 | 作用 |
|------|------|
| `bijia/excel_io.py` | 流式读原表、jsonl 追加、导出结果 xlsx |
| `bijia/jd_http.py` / `jd_chrome.py` / `jd_browser.py` | 打开商品/搜索 |
| `bijia/match.py` + `spec.py` | 匹配与规格换算 |
| `bijia/runner.py` | 批处理、缓存同款、频控退避 |
| `run.py` | CLI |

## 测试

```bash
python -m unittest discover -s tests -v
```

验证大表时只抽样小 `--limit`，不要一次加载整表做端到端压测。

## 风险（需人工侧注意）

- 京东频控 / 验证码 / 登录过期 → 暂停、`--login` 或检查 Chrome 登录后再续跑
- `--via chrome` 依赖本机 Chrome + AppleScript（macOS）；Linux/云端一般用 `--via http` + 事先 `--login`
- 全量跑完后请人工复核「待复核」行，再决定是否回写业务表
