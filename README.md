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

## 断点续传上传（可续传会话）

专线中断时不必整批重传：接入方**自选上传号** `upload_id`，把同一批报文分块追加，
收满后触发核验。会话状态（已收字节、声明总长、状态、固化响应）与数据一起**原子写入本地
SQLite**（单表单行、单事务提交），崩溃不会只落一半。

- `PUT /uploads/{upload_id}` — 写入一个原始分块（`application/octet-stream`）
  - 请求头 `Upload-Offset`（必填）：本分块在整体中的起始偏移；**首块必须为 0**
  - 请求头 `Upload-Length`（首块必填，≤ 1 MiB）：整批总字节数；后续分块可省略，
    若携带必须与首次声明一致
  - 成功 `200`：响应头 `Upload-Offset` 与 JSON 体给出**下一偏移**，客户端据此续传
  - **幂等重试**：整块完全落在已保存区间且字节一致 → `200`，偏移不变、数据不动
  - 冲突 `409`：越界（偏移超过当前末尾或超出声明总长）、跨越末尾、总数变化、
    内容冲突、会话已完成 —— 响应头 `Upload-Offset` 与 JSON 体给出**当前已提交偏移**，
    已存数据一字节不改
  - 缺头/非法头 `400`；声明总长或单块超过 1 MiB `413`
- `POST /uploads/{upload_id}/complete` — 触发核验
  - 仅当**已收字节 == 声明总长**时，会话在同一事务内从 `receiving` 原子转为
    `completed`：调用与 `/verify` 相同的解析器，并把完整响应**固化**进 SQLite
  - 重复完成返回**同一份**固化响应（字节一致）；完成后的写入一律 `409`
  - 未收满 `409`（带当前偏移）；未知 `upload_id` `404`

并发规则：同一 `upload_id` 的写入与完成各自包裹在 SQLite `IMMEDIATE` 事务里，
按**事务提交顺序**判定 —— 竞争中恰有一次合法变更生效，其余请求依据提交后的偏移或状态
得到确定反馈（重试者拿到幂等成功，冲突者拿到 409 与当前偏移，完成者拿到同一份固化响应）。

收满后的核验语义与 `/verify` 完全一致：合法批次 `ACCEPT` 带明细，非法批次整批
`REJECT` 带最早错误，汇总不符带声明值/实算值/带符号差异。

会话存储位置由环境变量 `UPLOADS_DB_PATH` 指定，默认为工作目录下的 `uploads.db`
（Docker 容器内为 `/app/uploads.db`）。

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
  store.py      # 可续传会话：SQLite 单表，数据与元数据同事务原子写入
  main.py       # FastAPI、1 MiB 流式上限、/verify 与 /uploads 会话、序列化
tests/          # pytest：解析规则单测 + 接口端到端 + 续传会话（in-process 与 BASE_URL 黑盒两用）
scripts/
  acceptance-entrypoint.sh   # verify 一次性服务入口：等健康 → pytest
Dockerfile
docker-compose.yml
```
