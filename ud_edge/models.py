"""Pydantic models for legs, options, games, players, and ranked picks."""
from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field, field_validator


class Player(BaseModel):
    id: str
    first_name: Optional[str] = ""
    last_name: Optional[str] = ""
    sport_id: Optional[str] = "UNK"  # e.g. "NBA", "MLB", "NFL"
    team_id: Optional[str] = None
    position_id: Optional[str] = None

    @property
    def full_name(self) -> str:
        return f"{self.first_name or ''} {self.last_name or ''}".strip() or "(unknown)"


class Appearance(BaseModel):
    id: str
    player_id: str
    match_id: Optional[int] = None
    match_type: Optional[str] = None
    team_id: Optional[str] = None


class Game(BaseModel):
    id: int
    abbreviated_title: str
    full_team_names_title: Optional[str] = None
    matchup_text: Optional[str] = None
    scheduled_at: Optional[str] = None


class Option(BaseModel):
    """One side of a player-prop over/under line."""
    id: str
    choice: str  # "higher" | "lower"
    choice_display: str  # "Higher" | "Lower"
    american_price: str  # e.g. "-136" — note STRING in UD API
    decimal_price: str  # e.g. "1.74"
    payout_multiplier: str  # e.g. "0.86" — what UD pays on a winning entry-stake unit
    line_value: Optional[float] = None  # extracted from parent line


class OverUnderLine(BaseModel):
    """One player prop, both sides priced."""
    id: str
    options: list[Option]
    line_type: str  # "balanced" | "alternate"
    live_event: bool
    expires_at: Optional[str] = None

    # Extracted from over_under.appearance_stat
    stat_name: Optional[str] = None  # e.g. "points", "hits", "rebounds"
    appearance_id: Optional[str] = None
    line_value: Optional[float] = None  # the actual line (e.g. 27.5)

    @field_validator("options")
    @classmethod
    def at_least_two_options(cls, v):
        if len(v) < 2:
            raise ValueError(f"over/under line must have >= 2 options, got {len(v)}")
        return v

    def higher(self) -> Option:
        for o in self.options:
            if o.choice == "higher":
                return o
        raise ValueError(f"no 'higher' option in line {self.id}")

    def lower(self) -> Option:
        for o in self.options:
            if o.choice == "lower":
                return o
        raise ValueError(f"no 'lower' option in line {self.id}")


class Leg(BaseModel):
    """One pickable leg, flattened from the raw UD response."""
    line_id: str
    appearance_id: Optional[str] = None
    player_id: str
    player_name: str
    sport_id: Optional[str] = "UNK"  # NBA, MLB, NFL, ...
    match_id: Optional[int] = None
    match_title: Optional[str] = None
    scheduled_at: Optional[str] = None
    stat_name: str  # "points", "hits", "rebounds", ...
    line_value: float  # e.g. 27.5
    line_type: str  # "balanced" | "alternate"
    higher_american: int
    higher_decimal: float
    higher_multiplier: float
    lower_american: int
    lower_decimal: float
    lower_multiplier: float


class RankedLeg(BaseModel):
    """One leg after the no-vig edge calc."""
    leg: Leg
    # Higher (over) side
    higher_true_prob: float
    higher_implied_prob: float
    higher_edge_pp: float  # positive = +EV vs break-even
    # Lower (under) side
    lower_true_prob: float
    lower_implied_prob: float
    lower_edge_pp: float
    # Picked side
    picked_side: str  # "higher" | "lower"
    picked_true_prob: float
    picked_edge_pp: float
    overround: float  # diagnostic: >1 = book vig, <1 = soft-book anomaly
    # Sharp-book cross-reference (optional)
    sharp_true_prob: Optional[float] = None  # true prob from sharp book, if matched
    sharp_book: Optional[str] = None  # e.g. "Pinnacle", "DraftKings", "manual-csv"
    sharp_overround: Optional[float] = None
    mispricing_edge_pp: Optional[float] = None  # sharp_true_prob - ud_true_prob (positive = UD too low)


class FlexEntryType(BaseModel):
    name: str  # "3-man-power", "6-flex", etc.
    n_legs: int
    payouts: dict[int, float]  # {hits: multiplier}, e.g. {6: 25.0, 5: 2.0, 4: 0.4}
    break_even: float  # per-leg break-even hit rate


class FlexEntryEV(BaseModel):
    entry: FlexEntryType
    expected_value_per_dollar: float  # EV / $1 staked (positive = +EV)
    win_prob: float  # probability of returning > stake at all
    median_payout: float  # expected payout given any hit count
    recommendation: str  # "play", "skip", "small"