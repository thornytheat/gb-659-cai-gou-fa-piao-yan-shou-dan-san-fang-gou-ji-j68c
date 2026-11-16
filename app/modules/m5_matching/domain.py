"""撤销补偿域的状态常量与派生口径。

匹配凭证（match_link）状态机：
    ACTIVE            正常占用，可登记付款
    RELEASED          未付款关联在验收撤销时释放，发票侧重新可匹配
    PAID              已全额付款，撤销时原样保留
    PARTIAL_RETAINED  部分付款：已付部分保留待追回，未付部分已释放
    RETAINED          已付款关联在撤销后原样保留（含 PAID 转换而来）

追回（recovery）状态机：
    PENDING  红字冲抵到账前，只列待追回，不增加任何可匹配余额
    SETTLED  红字冲抵到账后结清，依旧不回补可匹配余额

验收单（goods_receipt）状态机：
    ACTIVE   参与匹配与对账
    REVOKED  撤销完成，原额从当前应付扣除，关联按补偿流程处置
"""

GR_ACTIVE = "ACTIVE"
GR_REVOKED = "REVOKED"

LINK_ACTIVE = "ACTIVE"
LINK_RELEASED = "RELEASED"
LINK_PAID = "PAID"
LINK_PARTIAL_RETAINED = "PARTIAL_RETAINED"
LINK_RETAINED = "RETAINED"

RECOVERY_PENDING = "PENDING"
RECOVERY_SETTLED = "SETTLED"

# 撤销补偿步骤（顺序即 saga 恢复点）
STEP_RELEASE_UNPAID = "RELEASE_UNPAID"
STEP_RETAIN_PAID = "RETAIN_PAID"
STEP_REVOKE_GR = "REVOKE_GR"
SAGA_STEPS = (STEP_RELEASE_UNPAID, STEP_RETAIN_PAID, STEP_REVOKE_GR)

# 保留原样的凭证状态：永远不会被再次释放或参与新匹配
RETAINED_LINK_STATES = frozenset({LINK_PAID, LINK_PARTIAL_RETAINED, LINK_RETAINED})
CLOSED_LINK_STATES = frozenset(
    {LINK_RELEASED, LINK_PAID, LINK_PARTIAL_RETAINED, LINK_RETAINED}
)
