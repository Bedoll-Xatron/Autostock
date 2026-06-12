"""R 기반 포지션 사이징"""

from dataclasses import dataclass

from config import Grade, SignalConfig


@dataclass
class PositionResult:
    """포지션 사이징 결과"""
    entry_price: int          # 진입가
    stop_price: int           # 손절가
    target_price: int         # 목표가
    quantity: int             # 수량
    position_size: float      # 포지션 크기 (금액)
    r_value: float            # R값 (자본 × r_ratio)
    risk_amount: float        # 리스크 금액 (r_value × r_multiplier)
    r_multiplier: float       # R배수
    potential_profit: float   # 예상 수익
    potential_loss: float     # 예상 손실
    risk_reward_ratio: float  # 손익비
    position_pct: float       # 자본 대비 비율 %


class PositionSizer:
    """R 기반 포지션 사이징 계산기"""

    def __init__(self, capital: int, config: SignalConfig = None):
        self.capital = capital
        self.config = config or SignalConfig()
        self.r_value = capital * self.config.r_ratio

    def calculate(self, entry_price: int, grade: Grade) -> PositionResult:
        cfg = self.config
        stop_loss_pct = cfg.stop_loss_pct
        take_profit_pct = cfg.take_profit_pct

        # 1. 등급별 R배수 가져오기
        grade_cfg = cfg.grade_configs.get(grade)
        r_multiplier = grade_cfg.r_multiplier if grade_cfg else 0.0

        # 2. R배수가 0이면 매매 안 함
        if r_multiplier == 0:
            stop_price = int(entry_price * (1 - stop_loss_pct))
            target_price = int(entry_price * (1 + take_profit_pct))
            return PositionResult(
                entry_price=entry_price,
                stop_price=stop_price,
                target_price=target_price,
                quantity=0,
                position_size=0.0,
                r_value=self.r_value,
                risk_amount=0.0,
                r_multiplier=0.0,
                potential_profit=0.0,
                potential_loss=0.0,
                risk_reward_ratio=0.0,
                position_pct=0.0,
            )

        # 3. risk_amount = r_value × r_multiplier
        risk_amount = self.r_value * r_multiplier

        # 4. stop_price = entry_price × (1 - 5%)
        stop_price = int(entry_price * (1 - stop_loss_pct))

        # 5. target_price = entry_price × (1 + 15%)
        target_price = int(entry_price * (1 + take_profit_pct))

        # 6. position_size = risk_amount ÷ stop_loss_pct
        position_size = risk_amount / stop_loss_pct

        # 7. 최대 50% 제한
        max_position = self.capital * cfg.max_position_pct
        position_size = min(position_size, max_position)

        # 8. quantity = int(position_size ÷ entry_price)
        quantity = int(position_size / entry_price) if entry_price > 0 else 0

        # 9. 실제 포지션 = quantity × entry_price (정수 보정)
        position_size = float(quantity * entry_price)

        # 10. 예상 수익/손실 계산
        potential_profit = float(quantity * entry_price * take_profit_pct)
        potential_loss = float(quantity * entry_price * stop_loss_pct)

        # 11. 손익비 = take_profit_pct ÷ stop_loss_pct
        risk_reward_ratio = take_profit_pct / stop_loss_pct

        # 자본 대비 비율
        position_pct = position_size / self.capital * 100 if self.capital > 0 else 0.0

        return PositionResult(
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            quantity=quantity,
            position_size=position_size,
            r_value=self.r_value,
            risk_amount=risk_amount,
            r_multiplier=r_multiplier,
            potential_profit=potential_profit,
            potential_loss=potential_loss,
            risk_reward_ratio=round(risk_reward_ratio, 2),
            position_pct=round(position_pct, 1),
        )
