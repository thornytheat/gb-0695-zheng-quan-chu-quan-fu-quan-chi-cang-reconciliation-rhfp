# 接口

`POST /api/run`：JSON中的csv文本，省略使用样例；或multipart字段file。最大5MB。

`POST /api/m1`至`POST /api/m7`：独立运行到指定模块，JSON结构同运行接口。

`GET /api/history`：历史批次。`POST /api/review`接收id、revision和decision（通过或退回）。`GET /api/evidence.csv`提供单一证据导出。

结构错误422，版本变化409，文件过大413；错误返回code与message。所有页面可从左侧七个模块入口访问。

`GET /api/runs/<id>`读取指定批次；页面与证据CSV通过run_id查询参数绑定批次，模块导航保留该参数。

## 期后调整（M8）

关账批次（复核通过的 run）冻结不改写；关账后补来的登记/到账记录通过调整分录入账到开放期间，每笔记录只有一个生效期间。

- `POST /api/adjustments`：JSON `{csv, source_run_id, adjust_period}`，或 multipart 字段 `file` + 表单字段。来源批次必须已复核通过，调整期间格式 `YYYY-MM`；期间已关账返回 422 `PERIOD_CLOSED`。同文重复提交返回 `reused=true`。
- `GET /api/adjustments`：调整单列表；`GET /api/adjustments/<id>` 单据详情（含 entries 两期差异与 quarantine 留痕）。
- `POST /api/adjustments/<id>/review`：`{revision, decision}`，通过即生效；已生效单据决策锁定，再改返回 409 `LOCKED`；陈旧版本 409。
- `GET /api/adjustments/<id>/evidence.csv`：调整证据，含原期间、调整期间、幂等键与来源行号。
- `GET /api/periods`：期间状态；`POST /api/periods/<period>/close` body `{revision}` 关账，关账不可回退。
- 幂等键 `idem_key` 全局唯一：同一笔晚到记录重跑或跨期间再投都只生效一次，重复部分以 `DEDUPED` 隔离留痕。

JSON请求体必须是对象，数组、标量、null、损坏JSON及错误上传字段返回400，且不运行默认样例。
