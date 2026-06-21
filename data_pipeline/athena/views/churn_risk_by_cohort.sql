-- Churn risk distribution by acquisition cohort
-- Answers: "Which customer cohorts are churning fastest?"
-- This drives product decisions: cohorts with high churn may indicate
-- a broken onboarding flow or a pricing mismatch.

WITH latest_features AS (
    -- Get most recent feature snapshot per customer
    -- (snapshot_date is the Iceberg partition key)
    SELECT *
    FROM churn_platform_dev_curated.customer_features
    WHERE snapshot_date = (
        SELECT MAX(snapshot_date)
        FROM churn_platform_dev_curated.customer_features
    )
),

cohort_stats AS (
    SELECT
        cohort,
        current_plan,

        -- Customer counts by lifecycle state
        COUNT(*) AS total_customers,
        SUM(CASE WHEN customer_state = 'healthy'  THEN 1 ELSE 0 END) AS healthy_count,
        SUM(CASE WHEN customer_state = 'at_risk'  THEN 1 ELSE 0 END) AS at_risk_count,
        SUM(CASE WHEN customer_state = 'churning' THEN 1 ELSE 0 END) AS churning_count,
        SUM(CASE WHEN customer_state = 'churned'  THEN 1 ELSE 0 END) AS churned_count,

        -- Engagement metrics by cohort
        AVG(avg_session_duration_7d)     AS avg_session_duration_7d,
        AVG(avg_session_duration_30d)    AS avg_session_duration_30d,
        AVG(session_duration_trend)      AS avg_session_duration_trend,
        AVG(transaction_count_30d)       AS avg_transactions_30d,
        AVG(total_transaction_amount_30d) AS avg_revenue_30d,
        AVG(days_since_last_login)       AS avg_days_since_last_login,
        AVG(support_ticket_rate_30d)     AS avg_support_ticket_rate,

        -- Feature engagement
        AVG(feature_engagement_ratio_7d) AS avg_feature_engagement

    FROM latest_features
    WHERE cohort IS NOT NULL
    GROUP BY cohort, current_plan
)

SELECT
    cohort,
    current_plan,
    total_customers,
    healthy_count,
    at_risk_count,
    churning_count,
    churned_count,

    -- Churn rate: % of customers who have churned
    ROUND(100.0 * churned_count / NULLIF(total_customers, 0), 2) AS churn_rate_pct,

    -- At-risk rate: % who are trending toward churn
    ROUND(100.0 * (at_risk_count + churning_count) / NULLIF(total_customers, 0), 2) AS at_risk_rate_pct,

    -- Engagement trends (negative = declining)
    ROUND(avg_session_duration_trend, 4)  AS session_duration_trend,
    ROUND(avg_days_since_last_login, 1)   AS avg_days_since_login,
    ROUND(avg_transactions_30d, 2)        AS avg_monthly_transactions,
    ROUND(avg_revenue_30d, 2)             AS avg_monthly_revenue_usd,
    ROUND(avg_support_ticket_rate * 100, 2) AS support_ticket_rate_pct,
    ROUND(avg_feature_engagement * 100, 2) AS feature_engagement_pct

FROM cohort_stats

ORDER BY
    churn_rate_pct DESC,
    cohort ASC
;
