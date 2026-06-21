FROM python:3.11-slim

# SageMaker's DLC base already has CUDA for GPU — we use CPU-only for XGBoost
# since XGBoost tree training on CPU is often faster for our feature set size

WORKDIR /opt/ml/code

# Install dependencies first (layer cached unless requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy training code
COPY train.py .
COPY ../common ./common/

# SageMaker Training: SAGEMAKER_PROGRAM tells the toolkit which file is the entry point
ENV SAGEMAKER_PROGRAM=train.py

# Non-root user — security best practice
RUN useradd -m -u 1000 sagemaker && chown -R sagemaker:sagemaker /opt/ml
USER sagemaker

ENTRYPOINT ["python", "train.py"]
