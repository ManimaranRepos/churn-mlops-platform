# =============================================================================
# Custom Airflow image — extends the official image with:
#   - AWS providers (S3, Glue, SageMaker, Athena, CloudWatch)
#   - awswrangler (Athena queries in DAG tasks)
#   - great_expectations (data validation in feature_pipeline)
#   - scikit-learn + pandas (used in export_train_val_test_splits task)
#
# WHY extend the official image instead of building from scratch?
#   The official image includes Airflow with correct user permissions, entrypoint,
#   health-check logic, and the init-db / migrate-db flow. Building from scratch
#   means re-implementing all of that. Extending is the official recommendation.
#
# The resulting image is used for:
#   - scheduler pod
#   - webserver pod
#   - triggerer pod
#   - Each task pod spawned by KubernetesExecutor
#   (all pods must run the same image so imports work everywhere)
# =============================================================================

ARG AIRFLOW_VERSION=2.8.4
FROM apache/airflow:${AIRFLOW_VERSION}-python3.11

# Switch to root temporarily to install system deps
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

USER airflow

# Copy requirements first (layer caching — only invalidated when deps change)
COPY requirements.txt /requirements.txt
RUN pip install --no-cache-dir -r /requirements.txt

# Copy DAGs and plugins (overridden by gitSync in production, but needed for local dev)
COPY dags/ ${AIRFLOW_HOME}/dags/
COPY plugins/ ${AIRFLOW_HOME}/plugins/
