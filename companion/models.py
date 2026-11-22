"""Bounded application inputs; imported prose is data, never executable instructions."""

from datetime import date
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Preferences(Strict):
    timezone: str | None = None
    exam_date: str | None = None
    goals: dict[str, int] = Field(default_factory=dict)
    weekly_minutes: int | None = Field(default=None, ge=0, le=10080)
    quiet_start: str = "21:00"
    quiet_end: str = "08:00"
    lesson_slots: list[str] = Field(
        default_factory=lambda: ["10:00", "13:00", "16:00", "19:00"], max_length=8
    )
    daily_cap: int = Field(default=4, ge=0, le=8)
    max_current_offers: int = Field(default=2, ge=1, le=2)
    offer_expiry_hours: int = Field(default=24, ge=1, le=24)
    freshness_hours: int = Field(default=48, ge=1, le=720)
    readiness_min_forms: int = Field(default=3, ge=3, le=10)
    readiness_horizon_days: int = Field(default=90, ge=1, le=365)
    schedules_enabled: bool = False
    scheduled_delivery_mode: Literal["read_write", "backend_prepared_reads"] = (
        "read_write"
    )

    @model_validator(mode="after")
    def values(self):
        if self.timezone:
            ZoneInfo(self.timezone)
        if self.exam_date:
            date.fromisoformat(self.exam_date)
        for value in [self.quiet_start, self.quiet_end, *self.lesson_slots]:
            if (
                len(value) != 5
                or value[2] != ":"
                or not value.replace(":", "").isdigit()
            ):
                raise ValueError("Times must be HH:MM")
            if not (0 <= int(value[:2]) < 24 and 0 <= int(value[3:]) < 60):
                raise ValueError("Invalid local time")
        if len(set(self.lesson_slots)) != len(self.lesson_slots):
            raise ValueError("Lesson slots must be unique")
        if set(self.goals) - {"total", "CP", "CARS", "BB", "PS"}:
            raise ValueError("Unknown goal section")
        for section, goal in self.goals.items():
            low, high = (472, 528) if section == "total" else (118, 132)
            if not low <= goal <= high:
                raise ValueError("Goal is outside the MCAT scale")
        return self


class Confidence(Strict):
    scale: Literal["probability", "categorical"]
    value: float | str
    before_feedback: bool = True

    @model_validator(mode="after")
    def values(self):
        if not self.before_feedback:
            raise ValueError("Confidence on commitment must precede feedback")
        if self.scale == "probability" and (
            not isinstance(self.value, (int, float)) or not 0 <= self.value <= 1
        ):
            raise ValueError("Probability must be between zero and one")
        if self.scale == "categorical" and self.value not in {"low", "medium", "high"}:
            raise ValueError("Use low, medium, or high")
        return self


class Target(Strict):
    section: Literal["CP", "CARS", "BB", "PS"]
    concept: str = Field(min_length=1, max_length=200)
    skill: str = Field(min_length=1, max_length=200)


class LearnerView(Strict):
    stimulus: str = Field(default="", max_length=15000)
    question: str = Field(min_length=1, max_length=3000)
    options: dict[str, str]


class InstructorView(Strict):
    key: str
    explanation: str = Field(min_length=1, max_length=15000)
    hints: list[str] = Field(default_factory=list, max_length=3)
    rubric: str = Field(min_length=1, max_length=5000)
    source_claims: list[dict] = Field(default_factory=list, max_length=20)


class Item(Strict):
    id: str = Field(min_length=1, max_length=150)
    content_version: int = Field(ge=1)
    family: str = Field(min_length=1, max_length=200)
    target: Target
    purpose: Literal[
        "diagnostic",
        "immediate_application",
        "delayed_recall",
        "delayed_transfer",
        "maintenance",
    ]
    learner_view: LearnerView
    instructor_view: InstructorView
    challenge_intent: bool = False
    difficulty_calibrated: Literal[False] = False
    synthetic: bool = False
    provenance: dict

    @model_validator(mode="after")
    def values(self):
        options = self.learner_view.options
        if not 2 <= len(options) <= 6 or self.instructor_view.key not in options:
            raise ValueError("A fixed-choice item needs 2..6 options and a valid key")
        if any(
            not 1 <= len(k) <= 20 or not 1 <= len(v) <= 3000 for k, v in options.items()
        ):
            raise ValueError("Invalid option")
        return self
