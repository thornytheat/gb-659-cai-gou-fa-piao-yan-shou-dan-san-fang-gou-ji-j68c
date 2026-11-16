# 深度登记 采购三方勾稽报告

- 规模：中型
- 核心规则：采购、分批验收和合并发票按货号与税率分组，数量和金额使用 Decimal 守恒。
- 使用者与工作流：应付会计导入三类单据，映射主数据，核对数量税额，多对多匹配并导出差异。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 单据采信 | 导入三类单据并隔离冲突 | POST /api/documents/import | `app/modules/m1_documents/parser.py` `app/modules/m1_documents/money.py` | `tests/test_m1_documents.py` |
| M2 主数据映射 | 归一货号与旧字段 | POST /api/master/resolve | `app/modules/m2_master/aliases.py` `app/modules/m2_master/evolve.py` | `tests/test_m2_master.py` |
| M3 数量勾稽 | 统一包装单位并核对未到量 | POST /api/quantity/match | `app/modules/m3_quantity/units.py` `app/modules/m3_quantity/match.py` | `tests/test_m3_quantity.py` |
| M4 税额核验 | 重算净额、税额与含税额 | POST /api/tax/check | `app/modules/m4_tax/calculate.py` `app/modules/m4_tax/verify.py` | `tests/test_m4_tax.py` |
| M5 多对多匹配 | 登记真实单据/匹配/付款，撤销走补偿并登记红字冲抵 | GET /api/matching/ledger · POST /api/matching/revocations · /api/matching/credit-notes | `app/modules/m5_matching/ledger_service.py` `app/modules/m5_matching/repository.py` `app/modules/m5_matching/domain.py` | `tests/test_m5_matching.py` `tests/test_m5_compensation.py` |
| M6 差异批次 | 核对金额守恒并导出差异 | POST /api/reconcile | `app/modules/m6_batches/compare.py` `app/modules/m6_batches/exporter.py` | `tests/test_m6_batches.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-05 | 单据缺少字段 | 静默补空进入匹配 | 隔离并保留行号 | `app/modules/m1_documents/parser.py` | `tests/test_m1_documents.py::test_missing_field_quarantined` |
| TRAP-06 | 金额带千分位 | 直接转换失败 | 清理分隔符后使用 Decimal | `app/modules/m1_documents/money.py` | `tests/test_m1_documents.py::test_thousands_amount_uses_decimal` |
| TRAP-07 | 空值哨兵 | 被当作零金额 | 空值集合统一拒绝 | `app/modules/m1_documents/money.py` | `tests/test_m1_documents.py::test_missing_field_quarantined` |
| TRAP-08 | 供应商编码前导零 | 转整数后主数据失配 | 编码始终按字符串保形 | `app/modules/m2_master/aliases.py` | `tests/test_m2_master.py::test_supplier_code_preserves_leading_zero` |
| TRAP-10 | 税额浮点尾差 | 分项和总额差一分 | Decimal 半入到分 | `app/modules/m4_tax/calculate.py` | `tests/test_m4_tax.py::test_half_up_to_cent` |
| TRAP-14 | 重复单号值冲突 | 后写覆盖金额 | 单独隔离冲突版本 | `app/modules/m1_documents/parser.py` | `tests/test_m1_documents.py::test_conflicting_duplicate_detected` |
| TRAP-17 | 旧列名仍在使用 | 新版读取报错 | 受控字段迁移并报告冲突 | `app/modules/m2_master/evolve.py` | `tests/test_m2_master.py::test_old_columns_migrate` |
| TRAP-19 | 组合候选顺序变化 | 匹配结果随输入顺序变化 | 按差额、数量与编号稳定择一 | `app/modules/m5_matching/solver.py` | `tests/test_m5_matching.py::test_choice_is_order_independent` |
| TRAP-23 | 相邻税率边界 | 不同税率金额互相抵消 | 货号与税率共同分组 | `app/modules/m4_tax/verify.py` | `tests/test_m4_tax.py::test_different_tax_rates_never_offset` |
| TRAP-26 | 同批重复执行 | 重复新增对账结果 | 输入摘要作为运行唯一键 | `app/store.py` | `tests/test_m6_batches.py::test_idempotent_record` |
| TRAP-30 | 撤销验收单直接释放占用 | 已付款发票的同笔货款被再次匹配 | 未付款关联才 RELEASED；已付款凭证 RETAINED 原样保留并登记待追回 | `app/modules/m5_matching/ledger_service.py` | `tests/test_m5_compensation.py::test_revoke_releases_unpaid_but_keeps_paid_voucher` |
| TRAP-31 | 撤销即把金额加回可匹配余额 | 红字未到账货款被重复支用 | 红字到账前只列 PENDING 待追回，到账 SETTLED，两阶段都不回补可用余额 | `app/modules/m5_matching/ledger_service.py` | `tests/test_m5_compensation.py::test_credit_note_settles_recovery_without_refilling_balance` |
| TRAP-32 | 部分付款凭证整笔释放或整笔冻结 | 漏待追回或重复匹配 | 按已付/未付拆分为 RETAINED 与 RELEASED，金额守恒到分 | `app/modules/m5_matching/ledger_service.py` `app/modules/m5_matching/money.py` | `tests/test_m5_compensation.py::test_partial_payment_split_retains_paid_releases_unpaid` |
| TRAP-33 | 并发撤销互相覆盖 | 同一验收单被撤销两次、应付重复扣除 | version 乐观锁 + BEGIN IMMEDIATE，过期版本返回 409 | `app/modules/m5_matching/repository.py` | `tests/test_m5_compensation.py::test_concurrent_revoke_rejects_stale_version` |
| TRAP-34 | 补偿写入中断后状态半截 | 重试重复冲抵或原匹配丢失 | saga 每步独立事务并持久化步骤标记，resume 跳过已完成步骤，失败保留原匹配 | `app/modules/m5_matching/ledger_service.py` | `tests/test_m5_compensation.py::test_saga_interruption_is_recoverable_and_keeps_original_matching` |
| TRAP-35 | 红字/付款/撤销重试重复入账 | 待追回被多次冲抵 | idempotency_key 缓存首笔回执，重复请求返回同一结果 | `app/modules/m5_matching/repository.py` | `tests/test_m5_compensation.py::test_credit_note_idempotent_retry_does_not_double_offset` |

## 对账规则

- 采购数量 = 净验收数量 + 未到数量。
- 净验收含税额 = 已开票含税额 + 未开票金额 + 已解释差异。
- 不同税率的行不可相互抵消。
- 当前应付 = 验收原额合计 − 撤销金额；ACTIVE 验收单满足 原额 = 占用 + 可用。
- 撤销补偿：未付款关联释放（发票余额可再匹配），已付款关联保留并形成待追回；红字冲抵合计 = 待追回已结合计，任何阶段都不增加可匹配余额。

## 已知不覆盖

- 不处理外币汇兑，样例与输出均为人民币。

