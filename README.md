# Lockbox Batch Verification API（银行锁箱回款核验接口）

纯后端核验服务：接收银行锁箱每天送来的**无分隔符定长文本**，逐字节解析头记录与明细记录，
核对实际笔数与金额之和。合法返回 `ACCEPT` 与按序明细；任何非法都**整批 `REJECT`**，
只报告按“行号、再按列号”最靠前的那一个错误。财务接入方永远不会收到任何半批回款。

- 语言/框架：Python 3.12 + FastAPI（仅后端，无页面）
- 解析核心：`app/parser.py`，纯函数字节解析，对畸形输入**不抛异常**
- 运行方式：Docker Compose（默认只运行 API；另有名为 `verify` 的一次性验收服务）

## 报文格式

只允许 ASCII；行尾可用 `LF` 或 `CRLF`；最后一个行尾换行可省略。不得出现第二个 `H`，不得出现空行。

**第 1 行：头记录，恰好 25 字节**

| 字节位置 | 长度 | 含义 | 规则 |
| --- | --- | --- | --- |
| 1 | 1 | 记录类型 | 固定 `H` |
| 2–9 | 8 | 批次号 | 八位十进制数字 |
| 10–13 | 4 | 明细数（声明值） | 四位十进制数字 |
| 14–25 | 12 | 总金额（声明值，单位：分） | 十二位十进制数字 |

**第 2 行起：明细记录，每行恰好 27 字节**

| 字节位置 | 长度 | 含义 | 规则 |
| --- | --- | --- | --- |
| 1 | 1 | 记录类型 | 固定 `D` |
| 2–5 | 4 | 序号 | 从 `0001` 开始连续递增 1 |
| 6–15 | 10 | 发票号 | 1–10 位大写字母 `A–Z` 或数字 `0–9`，仅在右侧补空格 |
| 16–27 | 12 | 金额（单位：分） | 十二位十进制**正**整数（`000000000001` 起步） |

示例（`·` 表示补位空格，实际为空格）：

```
H000000010002000000003000
D0001INV1······000000001000
D0002INV2······000000002000
```

关键约束让“一个多余空格就让金额落入错误列”无法蒙混过关：每条明细必须恰好 27 字节，
发票号内空格只能出现在右侧；任何错位都会使行长度或某一列的字符合法性失败，
不会出现“行数对了但金额算错”的截断/错位批次通过的情况。

## 请求

- `POST /verify`
- `Content-Type: application/octet-stream`（原始文件字节，无表单封装）
- 请求体不超过 **1 MiB**（1048576 字节）；超出返回 `413`，且按字节流式截断，不全量读入内存

> 说明：四位明细数把结构合法的批次上限约束在 9999 笔（约 280 KiB）。
> 1 MiB 是纯粹的传输层上限，合法批次永远不会触及。

### 成功（HTTP 200）

```json
{
  "status": "ACCEPT",
  "declared": {
    "batch_number": "00000001",
    "detail_count": 2,
    "total_amount_cents": 3000
  },
  "computed_detail_count": 2,
  "computed_total_amount_cents": 3000,
  "details": [
    {"line": 2, "sequence": 1, "invoice": "INV1", "amount_cents": 1000},
    {"line": 3, "sequence": 2, "invoice": "INV2", "amount_cents": 2000}
  ],
  "differences": null,
  "error": null
}
```

### 结构非法（HTTP 200，整批 REJECT）

只给最靠前的一个错误，不返回任何明细、声明值或实算值：

```json
{
  "status": "REJECT",
  "declared": null,
  "computed_detail_count": null,
  "computed_total_amount_cents": null,
  "details": [],
  "differences": null,
  "error": {
    "line": 3,
    "column": 2,
    "code": "bad_sequence",
    "message": "expected sequence 0002, got 0001"
  }
}
```

### 汇总不符（HTTP 200，整批 REJECT）

头记录本身结构合法，但实算笔数或金额与声明值不符。此时额外返回声明值、实算值与
两个**带符号差异**（实算 − 声明），`error.column` 指向最早不符的汇总字段
（笔数字段为 10，仅金额不符时为 14）：

```json
{
  "status": "REJECT",
  "declared": {"batch_number": "00000001", "detail_count": 2, "total_amount_cents": 99},
  "computed_detail_count": 1,
  "computed_total_amount_cents": 1,
  "details": [],
  "differences": {"detail_count": -1, "total_amount_cents": -98},
  "error": {"line": 1, "column": 10, "code": "summary_mismatch", "message": "..."}
}
```

### 错误码

| code | 含义 | 定位 |
| --- | --- | --- |
| `empty_line` | 空载荷或出现空行 | 空行行首 |
| `non_ascii` | 出现非 ASCII 字节 | 该字节的 (行, 列) |
| `bad_record_type` | 行首非 `H`/`D`，或第 1 行不是头 | 第 1 列 |
| `duplicate_header` | 出现第二个 `H` | 该行第 1 列 |
| `bad_length` | 头非 25 字节 / 明细非 27 字节 | 首个**缺失列**（截断）或首个**多余列**（超长，头为 26、明细为 28） |
| `bad_batch_number` | 批次号非 8 位数字 | 首个非法列 |
| `bad_declared_count` | 声明笔数非 4 位数字 | 首个非法列 |
| `bad_declared_total` | 声明总额非 12 位数字 | 首个非法列 |
| `bad_sequence` | 序号非数字或未从 0001 连续递增 | 首个非法列 / 第 2 列 |
| `bad_invoice` | 发票号字符合法性或右侧补位规则被破坏 | 最早涉及的列 |
| `bad_amount` | 金额非 12 位数字或非正 | 首个非法列 / 第 16 列 |
| `summary_mismatch` | 笔数或金额汇总与实算不符 | 第 10 或 14 列 |

另有 `GET /health` 返回 `{"status":"ok"}` 供存活与验收等待使用。

## 断点续传上传（PUT /uploads）

专线中断时不必整批重传：接入方自选**上传号**，把同一批次分块写入会话，收满后触发核验。
会话数据与元数据落在本地 SQLite 文件（默认 `./uploads.db`，可用环境变量
`LOCKBOX_UPLOAD_DB` 改路径），每次写入都是单事务原子落库。

### PUT /uploads/{upload_id}

请求体为原始分块字节（无表单封装），配合两个请求头：

| 请求头 | 规则 |
| --- | --- |
| `Upload-Offset` | 必填，非负整数。首块必须为 `0`；后续块必须使用上一次返回的下一偏移 |
| `Upload-Length` | 首块必填，声明整批总字节数，不得超过 **1 MiB**；后续块可省略（沿用已声明总数），若携带则必须与已声明值一致 |

- 成功（含幂等重放）：`204 No Content`，响应头 `Upload-Offset` 给出当前下一偏移。
- 写入仅接受**当前末尾追加**；**完全落在已保存区间且字节一致**的整块重试按幂等成功返回
  （响应丢失后安全重发）。
- 以下情况一律 `409` 并携带当前偏移（响应头 `Upload-Offset`），且不改动任何已存数据：
  越界（偏移超过末尾或块尾超出声明总数）、跨越末尾（部分落在已存区间、部分超出）、
  总数变化（`Upload-Length` 与已声明值不一致）、内容冲突（落在已存区间但字节不一致）、
  会话已完成。
- `400`：缺少/非法 `Upload-Offset`，或首块缺少 `Upload-Length`；`404`：上传号不存在且偏移非 0；
  `413`：声明总数或单块请求体超过 1 MiB。

### POST /uploads/{upload_id}/complete

- 仅当**已收字节等于声明总数**时，把会话从接收中原子转为已完成：调用与 `POST /verify`
  完全相同的解析器核验拼好的整批字节，并把完整响应**固化**到 SQLite。
- 成功返回 `200` 与核验响应（形状同 `POST /verify`，合法批次 `ACCEPT`、非法批次 `REJECT`，
  语义不变）；**重复完成返回同一份固化结果**。
- 未收满：`409`（响应头 `Upload-Offset` 给出当前偏移）；上传号不存在：`404`。
- 完成后的任何写入（包括字节一致的重试）一律 `409`。

### 并发规则

同一上传号的并发写入与完成按**事务提交顺序**判定：每个请求都是一个
`BEGIN IMMEDIATE` 事务，竞争中的一次合法变更生效，其余请求依据提交后的
偏移或状态得到确定反馈（`409` + 当前偏移，或已固化的完成响应），已存内容不会被污染。

## 用 Docker Compose 运行

默认 `up` **只运行 API**：

```bash
docker compose up --build
# 接口默认暴露在宿主 8000 端口
curl -X POST --data-binary @batch.txt \
  -H 'Content-Type: application/octet-stream' \
  http://localhost:8000/verify
```

用 `API_PORT` 覆盖宿主端口（容器内始终监听 8000）：

```bash
API_PORT=9000 docker compose up --build
# 然后访问 http://localhost:9000
```

### 一次性验收服务 `verify`

`verify` 不常驻：它先等待 API 健康，再对**运行中的 API** 以黑盒 HTTP 方式运行一次 pytest，
随后退出。位于 `acceptance` profile，因此不会被普通 `up` 拉起。需要显式先构建镜像：

```bash
docker compose build
docker compose up -d api
docker compose run --rm verify
docker compose down
```

`verify` 通过环境变量 `BASE_URL=http://api:8000` 让同一套 pytest 走真实网络请求。

## 本地开发与测试

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q                 # in-process（TestClient），无需起服务

# 或对已运行的服务做黑盒验收：
uvicorn app.main:app --port 8000 &
BASE_URL=http://127.0.0.1:8000 pytest -q
```

## 设计要点：为什么不会有“固定响应”

`app/parser.py` 是无状态纯函数：输入字节，输出由该字节实算得出的结果。

- `ACCEPT`/`REJECT`、声明值、实算值、明细全部来自上传报文本身；代码中不存在任何
  按输入“匹配后返回的固定答案”，畸形输入也不会抛出异常或走到兜底响应。
- 解析严格从左到右、逐字节进行，因此遇到的**第一个**错误天然就是按 (行号, 列号)
  最靠前的错误；非 ASCII 字节也纳入同一排序（同一位置上非 ASCII 优先报告）。
- 任何拒绝路径都不会携带已解析的明细：要么整批 `ACCEPT`，要么什么明细都不给出，
  从机制上杜绝“半批回款”。
- 金额使用 Python 任意精度整数，12 位金额及求和不会溢出。

## 目录结构

```
app/
  parser.py     # 逐字节解析 + 核验（纯函数，无固定响应）
  schemas.py    # 响应模型
  uploads.py    # 断点续传会话：SQLite 单事务读写、偏移/冲突判定、完成固化
  main.py       # FastAPI、1 MiB 流式上限、/verify 与 /uploads 路由、序列化
tests/          # pytest：解析规则单测 + 接口端到端 + 续传验收（in-process 与 BASE_URL 黑盒两用）
scripts/
  acceptance-entrypoint.sh   # verify 一次性服务入口：等健康 → pytest
Dockerfile
docker-compose.yml
```
