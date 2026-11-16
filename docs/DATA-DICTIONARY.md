# 数据字典

- run_id：规则版本与输入 SHA-256 前 12 位组成的稳定批次标识。
- line_no：从 1 开始的物理来源行号，不因隔离或排序改变。
- business_key：本项目声明的业务唯一键，始终按字符串存储。
- reason_code：隔离原因的稳定英文编码。
- accepted：进入业务计算的记录数。
- quarantined：保留证据但不参与业务计算的记录数。
- 数值：金额、税额、用量和权重使用 Decimal；对外序列化为字符串。
- 时间：存储为带偏移的 ISO 8601，分桶使用半开区间。
- 空值：缺失键、空串和业务哨兵分别记录。
- 幂等：完整输入字节与规则版本相同则复用 run_id，不新增结果。

## M5 撤销补偿台账

- 表前缀 `m5_`：`m5_goods_receipt`（验收单，带 `version` 乐观锁）、`m5_invoice`、`m5_match_link`（匹配凭证，永不删除）、`m5_recovery`（待追回）、`m5_credit_note`（红字冲抵）、`m5_revocation` 与 `m5_revocation_step`（saga 恢复点）、`m5_idempotency`（幂等回执）。
- 匹配凭证状态：`ACTIVE` 占用中；`RELEASED` 未付款关联撤销时释放；`PAID` 已全额付款；`PARTIAL_RETAINED` 部分付款（已付保留、未付释放）；`RETAINED` 已付款关联撤销后原样保留。
- 待追回状态：`PENDING` 红字到账前只列待追回，不增加任何可匹配余额；`SETTLED` 红字到账结清，依旧不回补余额。
- 验收单状态：`ACTIVE` 可匹配；`REVOKED` 撤销完成，原额从当前应付扣除。
- 口径：当前应付 = ACTIVE 验收单原额合计 = 验收原额合计 − 撤销金额；ACTIVE 验收单原额 = 占用 + 可用；发票原额 = 占用 + 可用。
- 撤销 saga 三步骤（每步独立事务，步骤完成标记持久化）：`RELEASE_UNPAID` → `RETAIN_PAID` → `REVOKE_GR`；中断后 `/api/matching/revocations/resume` 只推进未完成步骤，重试不重复冲抵。
- 并发：撤销携带 `expected_version`，与当前 `version` 不符返回 409 `GR_VERSION_CONFLICT`；写事务以 `BEGIN IMMEDIATE` 串行化。
- 幂等键：付款、撤销、红字冲抵按 `idempotency_key` 缓存首笔回执，重复请求返回同一结果，不产生第二张红字或第二笔待追回。


