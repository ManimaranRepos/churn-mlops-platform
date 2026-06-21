"""
Event schema definitions and realistic data generation.

Customer lifecycle model:
  healthy    → session_duration HIGH, transactions FREQUENT, features USED
  at_risk    → session_duration DECLINING, transactions SPARSE
  churning   → login-only visits, no transactions, features IGNORED
  churned    → no events at all (silence = signal)
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any
import random
import uuid


class EventType(str, Enum):
    LOGIN           = "login"
    SESSION_START   = "session_start"
    SESSION_END     = "session_end"
    TRANSACTION     = "transaction"
    FEATURE_USAGE   = "feature_usage"
    PROFILE_UPDATE  = "profile_update"
    SUPPORT_TICKET  = "support_ticket"   # High churn signal — frustrated customer
    PLAN_DOWNGRADE  = "plan_downgrade"   # Very high churn signal
    CHURN           = "churn"


class Device(str, Enum):
    MOBILE  = "mobile"
    DESKTOP = "desktop"
    TABLET  = "tablet"


class CustomerState(str, Enum):
    HEALTHY   = "healthy"
    AT_RISK   = "at_risk"
    CHURNING  = "churning"
    CHURNED   = "churned"


# Feature flags represent product features — their usage patterns matter for churn
FEATURE_FLAGS = [
    "premium_analytics",
    "api_access",
    "team_collaboration",
    "custom_reports",
    "advanced_filtering",
    "bulk_export",
    "webhooks",
    "sso",
]

# Acquisition cohort by quarter
COHORTS = [
    "2022-Q3", "2022-Q4",
    "2023-Q1", "2023-Q2", "2023-Q3", "2023-Q4",
    "2024-Q1",
]


@dataclass
class CustomerProfile:
    """Simulates a customer's characteristics and current lifecycle state."""
    customer_id: str
    cohort: str
    state: CustomerState
    plan: str  # free, starter, pro, enterprise
    device_preference: Device
    base_session_duration: int      # seconds, varies by state
    transaction_frequency: float    # probability per day of making a transaction
    feature_adoption_count: int     # how many features they use actively
    days_since_last_login: int
    lifetime_value: float

    @classmethod
    def generate(cls, state: CustomerState = None) -> "CustomerProfile":
        """Generate a realistic customer profile, optionally with forced state."""
        if state is None:
            # Realistic distribution: most customers are healthy, some at risk
            state = random.choices(
                [CustomerState.HEALTHY, CustomerState.AT_RISK,
                 CustomerState.CHURNING, CustomerState.CHURNED],
                weights=[60, 25, 10, 5]
            )[0]

        # Session duration decreases as customer approaches churn
        session_durations = {
            CustomerState.HEALTHY:  random.randint(600, 3600),   # 10 min - 1 hr
            CustomerState.AT_RISK:  random.randint(120, 600),    # 2 - 10 min
            CustomerState.CHURNING: random.randint(30, 120),     # 30 sec - 2 min
            CustomerState.CHURNED:  0,
        }

        tx_frequencies = {
            CustomerState.HEALTHY:  random.uniform(0.3, 0.8),
            CustomerState.AT_RISK:  random.uniform(0.05, 0.2),
            CustomerState.CHURNING: random.uniform(0.0, 0.05),
            CustomerState.CHURNED:  0.0,
        }

        feature_counts = {
            CustomerState.HEALTHY:  random.randint(3, 8),
            CustomerState.AT_RISK:  random.randint(1, 3),
            CustomerState.CHURNING: random.randint(0, 1),
            CustomerState.CHURNED:  0,
        }

        plans = ["free", "starter", "pro", "enterprise"]
        plan_weights = {
            CustomerState.HEALTHY:  [0.1, 0.3, 0.4, 0.2],
            CustomerState.AT_RISK:  [0.3, 0.4, 0.2, 0.1],
            CustomerState.CHURNING: [0.5, 0.3, 0.15, 0.05],
            CustomerState.CHURNED:  [0.7, 0.2, 0.08, 0.02],
        }

        return cls(
            customer_id=f"cust_{uuid.uuid4().hex[:12]}",
            cohort=random.choice(COHORTS),
            state=state,
            plan=random.choices(plans, weights=plan_weights[state])[0],
            device_preference=random.choice(list(Device)),
            base_session_duration=session_durations[state],
            transaction_frequency=tx_frequencies[state],
            feature_adoption_count=feature_counts[state],
            days_since_last_login=random.randint(0, 30 if state != CustomerState.CHURNED else 90),
            lifetime_value=random.uniform(0, 5000),
        )


@dataclass
class CustomerEvent:
    """A single customer event ready to publish to Kinesis."""
    event_id: str
    customer_id: str
    event_type: str
    timestamp: str
    device: str
    session_id: str
    session_duration: int
    transaction_amount: float
    feature_flags: dict[str, bool]
    cohort: str
    plan: str
    customer_state: str       # ground truth — used for model training labels
    metadata: dict[str, Any]

    def to_kinesis_record(self) -> dict:
        """Format for Kinesis PutRecords API."""
        return {
            "Data": self._to_json().encode("utf-8"),
            # Partition key: same customer_id routes to same shard
            # This preserves event ordering per customer
            "PartitionKey": self.customer_id,
        }

    def _to_json(self) -> str:
        import json
        return json.dumps(asdict(self), default=str)


def generate_event(profile: CustomerProfile, session_id: str = None) -> CustomerEvent:
    """
    Generate a realistic event for a customer based on their lifecycle state.
    The event_type distribution is the key signal for churn prediction.
    """
    session_id = session_id or f"sess_{uuid.uuid4().hex[:8]}"

    # Healthy customers use features; churning customers just log in and leave
    event_type_weights = {
        CustomerState.HEALTHY: {
            EventType.SESSION_START:  15,
            EventType.LOGIN:          10,
            EventType.TRANSACTION:    25,
            EventType.FEATURE_USAGE:  30,
            EventType.PROFILE_UPDATE: 10,
            EventType.SESSION_END:    10,
            EventType.SUPPORT_TICKET: 0,
            EventType.PLAN_DOWNGRADE: 0,
        },
        CustomerState.AT_RISK: {
            EventType.SESSION_START:  20,
            EventType.LOGIN:          30,
            EventType.TRANSACTION:    10,
            EventType.FEATURE_USAGE:  15,
            EventType.PROFILE_UPDATE: 5,
            EventType.SESSION_END:    10,
            EventType.SUPPORT_TICKET: 8,   # Frustration signal
            EventType.PLAN_DOWNGRADE: 2,
        },
        CustomerState.CHURNING: {
            EventType.SESSION_START:  25,
            EventType.LOGIN:          50,  # Login-only behaviour
            EventType.TRANSACTION:    2,
            EventType.FEATURE_USAGE:  3,
            EventType.PROFILE_UPDATE: 2,
            EventType.SESSION_END:    15,
            EventType.SUPPORT_TICKET: 2,
            EventType.PLAN_DOWNGRADE: 1,
        },
        CustomerState.CHURNED: {
            EventType.CHURN: 100,
        },
    }

    weights = event_type_weights[profile.state]
    event_type = random.choices(list(weights.keys()), weights=list(weights.values()))[0]

    # Add noise: session duration jitters around the base value
    session_duration = max(0, int(
        profile.base_session_duration * random.uniform(0.5, 1.5)
    ))

    # Transaction amount: higher-value customers spend more
    transaction_amount = 0.0
    if event_type == EventType.TRANSACTION:
        base_amount = {"free": 0, "starter": 29, "pro": 99, "enterprise": 499}
        transaction_amount = base_amount.get(profile.plan, 0) * random.uniform(0.8, 1.5)

    # Feature flags: active features for this customer
    active_features = random.sample(
        FEATURE_FLAGS,
        min(profile.feature_adoption_count, len(FEATURE_FLAGS))
    )
    feature_flags = {f: (f in active_features) for f in FEATURE_FLAGS}

    # Add slight jitter to device (customers sometimes switch devices)
    device = profile.device_preference
    if random.random() < 0.15:
        device = random.choice(list(Device))

    return CustomerEvent(
        event_id=str(uuid.uuid4()),
        customer_id=profile.customer_id,
        event_type=event_type.value,
        timestamp=datetime.now(timezone.utc).isoformat(),
        device=device.value,
        session_id=session_id,
        session_duration=session_duration,
        transaction_amount=round(transaction_amount, 2),
        feature_flags=feature_flags,
        cohort=profile.cohort,
        plan=profile.plan,
        customer_state=profile.state.value,
        metadata={
            "source": "event_producer",
            "schema_version": "1.0",
            "days_since_last_login": profile.days_since_last_login,
            "lifetime_value": round(profile.lifetime_value, 2),
        },
    )
