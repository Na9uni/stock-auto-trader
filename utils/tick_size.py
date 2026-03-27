"""한국 주식시장 호가 단위 유틸."""


def align_tick_size(price: int, direction: str = "down") -> int:
    """한국 주식시장 호가 단위에 맞춤.
    direction: "up"=올림, "down"=내림
    """
    if price < 2000:
        tick = 1
    elif price < 5000:
        tick = 5
    elif price < 20000:
        tick = 10
    elif price < 50000:
        tick = 50
    elif price < 200000:
        tick = 100
    elif price < 500000:
        tick = 500
    else:
        tick = 1000
    if direction == "up":
        return ((price + tick - 1) // tick) * tick
    return (price // tick) * tick
