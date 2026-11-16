from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def to_decimal(value) -> Decimal:
    """金额统一按 Decimal 处理，字符串保形，禁止 float 入域。"""
    if isinstance(value, Decimal):
        return value.quantize(CENT)
    if isinstance(value, bool):
        raise ValueError("金额不接受布尔值")
    if isinstance(value, (int, str)):
        return Decimal(str(value)).quantize(CENT)
    raise ValueError(f"不支持的金额类型: {type(value)!r}")


def as_text(value: Decimal) -> str:
    return str(to_decimal(value))


def is_positive(value: Decimal) -> bool:
    return value > ZERO


def largest_remainder_split(total: Decimal, weights: list[Decimal]) -> list[Decimal]:
    """按权重把 total 拆到分：先按比例半入到分，尾差按最大余额法补给最大份额。

    保证各份之和严格等于 total，且每份不超过其权重（权重即该份额上限）。
    """
    total = to_decimal(total)
    weights = [to_decimal(weight) for weight in weights]
    if not weights:
        return []
    if total < ZERO:
        raise ValueError("拆分金额不能为负")
    weight_sum = sum(weights, ZERO)
    if weight_sum <= ZERO:
        raise ValueError("权重之和必须为正")
    if total > weight_sum:
        raise ValueError("拆分金额超过权重之和")
    if total == ZERO:
        return [ZERO for _ in weights]

    raw = [total * weight / weight_sum for weight in weights]
    floored = [piece.quantize(CENT, rounding=ROUND_HALF_UP) for piece in raw]
    remainder = total - sum(floored, ZERO)
    if remainder != ZERO:
        # 尾差（最多几分）按未取整余额从大到小补给，平手按权重再按索引保证确定性
        order = sorted(
            range(len(weights)),
            key=lambda idx: (raw[idx] - floored[idx], weights[idx], -idx),
            reverse=True,
        )
        steps = int((remainder / CENT).to_integral_value())
        for idx in order[: abs(steps)]:
            floored[idx] += CENT if steps > 0 else -CENT
    result = [to_decimal(piece) for piece in floored]
    assert sum(result, ZERO) == total
    for piece, weight in zip(result, weights):
        assert ZERO <= piece <= weight
    return result
