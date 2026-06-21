-- Daily event counts by type — pipeline health dashboard
-- Use this to detect if Kinesis/Glue is broken (sudden drop in events)
-- Run in Athena workgroup: churn-platform-dev-workgroup

SELECT
    DATE(event_timestamp)       AS event_date,
    event_type,
    COUNT(*)                    AS event_count,
    COUNT(DISTINCT customer_id) AS unique_customers,

    -- Average session duration per event type per day
    AVG(CAST(session_duration AS DOUBLE))   AS avg_session_duration_secs,

    -- Total revenue-generating events
    SUM(CASE WHEN transaction_amount > 0 THEN 1 ELSE 0 END) AS transaction_events,
    SUM(COALESCE(transaction_amount, 0.0))                  AS total_transaction_amount

FROM churn_platform_dev_curated.events  -- Update database name per environment

WHERE
    -- Partition pruning: only scan recent partitions (CRITICAL for cost control)
    event_year  = YEAR(CURRENT_DATE)
    AND event_month = MONTH(CURRENT_DATE)
    AND event_day >= DAY(CURRENT_DATE) - 7

GROUP BY
    DATE(event_timestamp),
    event_type

ORDER BY
    event_date DESC,
    event_count DESC
;
