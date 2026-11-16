# 采购三方勾稽报告

面向真实数据复核的中型数据工程工具，包含六个可独立调用的业务模块。规则、失败原因与证据字段见 docs/DATA-DICTIONARY.md，陷阱和测试映射见 docs/DEPTH.md。

## 启动

docker compose up --build

浏览器访问 http://localhost:18218。测试：docker compose exec -T app pytest -q

## 模块

- M1 单据采信：页面 /documents，接口 /api/documents/import
- M2 主数据映射：页面 /master，接口 /api/master/resolve
- M3 数量勾稽：页面 /quantity，接口 /api/quantity/match
- M4 税额核验：页面 /tax，接口 /api/tax/check
- M5 多对多匹配：页面 /matching，台账 GET /api/matching/ledger，登记 /api/matching/matches、/api/matching/payments，撤销 /api/matching/revocations（补偿恢复 /resume），红字冲抵 /api/matching/credit-notes；原组合样例接口保留为 POST /api/matches/solve
- M6 差异批次：页面 /batches，接口 /api/reconcile

## M5 撤销补偿规则

- 撤销验收单即从当前应付扣除撤销金额；匹配凭证永不删除。
- 未付款关联才释放（发票侧重新可匹配）；已付款关联原样保留，逐笔登记待追回。
- 部分付款按已付/未付拆分：已付保留待追回，未付释放。
- 红字冲抵到账前只列待追回，到账后结清；两个阶段都不提前增加可匹配余额。
- 撤销为三步骤 saga（释放未付 → 保留已付 → 翻转验收单），每步独立事务；中断后 `/api/matching/revocations/resume` 可恢复，重试不重复冲抵，失败保留原匹配。
- 并发撤销以验收单 `version` 乐观锁拒绝过期状态（409）；付款、撤销、红字均以幂等键去重。

## 运行规则

同一输入内容生成相同运行标识，重复执行复用已有批次。隔离记录包含原因码、原始行号与原值；业务导出只保留一份证据清单。

