"""
Customer Event Producer
=======================
Generates realistic customer behavioral events and publishes them to Kinesis.

Usage:
    # Generate 1000 events across 100 simulated customers (one-shot)
    python event_producer.py --customers 100 --events-per-customer 10

    # Continuous mode: simulate real-time event stream at 50 events/sec
    python event_producer.py --mode continuous --rate 50

    # Generate a churn scenario: customer degrades over 30 days
    python event_producer.py --mode churn-scenario --customer-id cust_abc123

Kinesis PutRecords batches up to 500 records per call.
We use batching for efficiency — individual PutRecord calls cost 10x more.
"""

import argparse
import json
import logging
import random
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Generator

import boto3
from botocore.config import Config

from event_schemas import (
    CustomerEvent,
    CustomerProfile,
    CustomerState,
    generate_event,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("event_producer")

# Kinesis PutRecords hard limit
KINESIS_MAX_BATCH_SIZE = 500
KINESIS_MAX_BATCH_BYTES = 5 * 1024 * 1024  # 5 MB


class KinesisProducer:
    """
    Efficient Kinesis producer with batching, retries, and metrics.
    Uses adaptive retries — backs off when the shard is being throttled.
    """

    def __init__(self, stream_name: str, region: str = "us-east-1"):
        self.stream_name = stream_name
        self.client = boto3.client(
            "kinesis",
            region_name=region,
            config=Config(
                retries={"max_attempts": 5, "mode": "adaptive"},
                max_pool_connections=50,
            ),
        )
        self.metrics = Counter()

    def publish_batch(self, events: list[CustomerEvent]) -> tuple[int, int]:
        """
        Publish a batch to Kinesis. Returns (success_count, failure_count).
        PutRecords is partial — some records can succeed while others fail.
        We retry failed records up to 3 times before giving up.
        """
        records = [e.to_kinesis_record() for e in events]
        remaining = records
        success = 0
        max_retries = 3

        for attempt in range(max_retries):
            if not remaining:
                break

            response = self.client.put_records(
                StreamName=self.stream_name,
                Records=remaining,
            )

            failed_count = response["FailedRecordCount"]
            if failed_count == 0:
                success += len(remaining)
                break

            # Extract and retry only the failed records
            newly_failed = []
            for i, result in enumerate(response["Records"]):
                if "ErrorCode" in result:
                    newly_failed.append(remaining[i])
                    if result["ErrorCode"] == "ProvisionedThroughputExceededException":
                        # Shard is hot — back off before retry
                        time.sleep(0.1 * (2 ** attempt))
                else:
                    success += 1

            log.warning(
                f"Attempt {attempt+1}: {failed_count} records failed, retrying..."
            )
            remaining = newly_failed

        failures = len(remaining)
        self.metrics["published"] += success
        self.metrics["failed"] += failures
        return success, failures

    def publish_events_in_batches(
        self, events: list[CustomerEvent]
    ) -> tuple[int, int]:
        """Chunk events into Kinesis-sized batches and publish all of them."""
        total_success = total_failure = 0

        for i in range(0, len(events), KINESIS_MAX_BATCH_SIZE):
            batch = events[i : i + KINESIS_MAX_BATCH_SIZE]
            s, f = self.publish_batch(batch)
            total_success += s
            total_failure += f

        return total_success, total_failure

    def log_metrics(self):
        log.info(
            f"Producer metrics — published: {self.metrics['published']:,}, "
            f"failed: {self.metrics['failed']:,}"
        )


def generate_events_for_customers(
    customers: list[CustomerProfile],
    events_per_customer: int,
) -> list[CustomerEvent]:
    """Generate a burst of events across all customers."""
    all_events = []
    for customer in customers:
        if customer.state == CustomerState.CHURNED:
            # Churned customers generate no events (their silence IS the signal)
            continue
        for _ in range(events_per_customer):
            all_events.append(generate_event(customer))

    # Shuffle so events from different customers are interleaved (realistic)
    random.shuffle(all_events)
    return all_events


def continuous_stream(
    producer: KinesisProducer,
    customer_pool: list[CustomerProfile],
    target_rate: int,
    duration_seconds: int = None,
) -> None:
    """
    Simulates a continuous real-time event stream.
    target_rate: events per second to generate.
    Adjusts sleep time each second to maintain the target rate.
    """
    log.info(f"Starting continuous stream at ~{target_rate} events/sec")
    start_time = time.time()
    events_this_second = []
    second_start = time.time()

    while True:
        if duration_seconds and (time.time() - start_time) > duration_seconds:
            log.info("Duration reached, stopping continuous stream")
            break

        # Pick a random active customer and generate an event
        customer = random.choice([c for c in customer_pool
                                  if c.state != CustomerState.CHURNED])
        events_this_second.append(generate_event(customer))

        # At the end of each 1-second window, publish the batch
        now = time.time()
        elapsed = now - second_start
        if elapsed >= 1.0:
            success, failed = producer.publish_events_in_batches(events_this_second)
            actual_rate = len(events_this_second) / elapsed
            log.info(
                f"Rate: {actual_rate:.0f} evt/s | "
                f"Published: {success} | Failed: {failed}"
            )
            events_this_second = []
            second_start = now

        # Throttle to approach target rate
        if len(events_this_second) >= target_rate:
            time.sleep(max(0, 1.0 - (time.time() - second_start)))


def churn_scenario(
    producer: KinesisProducer,
    customer_id: str,
    days: int = 30,
) -> None:
    """
    Simulates a single customer's churn journey over N days.
    Useful for testing that the model correctly identifies degrading behaviour.
    """
    import uuid

    log.info(f"Running churn scenario for {customer_id} over {days} days")

    # Customer starts healthy and degrades to churned over `days`
    day_to_state = {
        **{d: CustomerState.HEALTHY for d in range(0, days // 3)},
        **{d: CustomerState.AT_RISK for d in range(days // 3, 2 * days // 3)},
        **{d: CustomerState.CHURNING for d in range(2 * days // 3, days - 1)},
        days - 1: CustomerState.CHURNED,
    }

    all_events = []
    for day, state in day_to_state.items():
        profile = CustomerProfile(
            customer_id=customer_id,
            cohort="2023-Q4",
            state=state,
            plan="pro",
            device_preference=CustomerProfile.generate().device_preference,
            base_session_duration={
                CustomerState.HEALTHY: 1800,
                CustomerState.AT_RISK: 300,
                CustomerState.CHURNING: 60,
                CustomerState.CHURNED: 0,
            }[state],
            transaction_frequency={
                CustomerState.HEALTHY: 0.6,
                CustomerState.AT_RISK: 0.1,
                CustomerState.CHURNING: 0.01,
                CustomerState.CHURNED: 0.0,
            }[state],
            feature_adoption_count={
                CustomerState.HEALTHY: 6,
                CustomerState.AT_RISK: 2,
                CustomerState.CHURNING: 0,
                CustomerState.CHURNED: 0,
            }[state],
            days_since_last_login=day,
            lifetime_value=1500.0,
        )

        if state != CustomerState.CHURNED:
            events_per_day = max(1, int(5 * (1 - day / days)))
            for _ in range(events_per_day):
                all_events.append(generate_event(profile))

    success, failed = producer.publish_events_in_batches(all_events)
    log.info(
        f"Churn scenario complete: {success} events published, {failed} failed"
    )


def main():
    parser = argparse.ArgumentParser(description="Churn Platform Event Producer")
    parser.add_argument(
        "--stream-name",
        default=None,
        help="Kinesis stream name (default: from env KINESIS_STREAM_NAME)",
    )
    parser.add_argument(
        "--region", default="us-east-1",
        help="AWS region",
    )
    parser.add_argument(
        "--mode",
        choices=["one-shot", "continuous", "churn-scenario"],
        default="one-shot",
    )
    parser.add_argument("--customers", type=int, default=100,
                        help="Number of simulated customers")
    parser.add_argument("--events-per-customer", type=int, default=10,
                        help="Events per customer (one-shot mode)")
    parser.add_argument("--rate", type=int, default=100,
                        help="Events per second (continuous mode)")
    parser.add_argument("--duration", type=int, default=None,
                        help="Duration in seconds (continuous mode). Default: run forever")
    parser.add_argument("--customer-id", default=None,
                        help="Specific customer ID (churn-scenario mode)")

    args = parser.parse_args()

    import os
    stream_name = args.stream_name or os.environ.get(
        "KINESIS_STREAM_NAME", "churn-platform-dev-events"
    )

    producer = KinesisProducer(stream_name=stream_name, region=args.region)

    if args.mode == "churn-scenario":
        import uuid
        customer_id = args.customer_id or f"cust_{uuid.uuid4().hex[:12]}"
        churn_scenario(producer, customer_id)

    elif args.mode == "continuous":
        # Build a pool of customers with realistic state distribution
        customers = [CustomerProfile.generate() for _ in range(args.customers)]
        state_dist = Counter(c.state for c in customers)
        log.info(f"Customer pool: {dict(state_dist)}")
        continuous_stream(
            producer, customers,
            target_rate=args.rate,
            duration_seconds=args.duration,
        )

    else:  # one-shot
        log.info(f"Generating events for {args.customers} customers...")
        customers = [CustomerProfile.generate() for _ in range(args.customers)]
        state_dist = Counter(c.state for c in customers)
        log.info(f"Customer states: {dict(state_dist)}")

        events = generate_events_for_customers(customers, args.events_per_customer)
        log.info(f"Generated {len(events)} events, publishing to {stream_name}...")

        success, failed = producer.publish_events_in_batches(events)
        producer.log_metrics()

        log.info(f"Done. Published: {success:,} | Failed: {failed:,}")
        return 1 if failed > 0 else 0


if __name__ == "__main__":
    exit(main())
