# Standalone image for the add-on. The repository Dockerfile is NOT modified.
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
COPY requirements.txt ./repo-requirements.txt
RUN pip install -r repo-requirements.txt
COPY rpc_service/requirements.txt ./rpc_service-requirements.txt
RUN pip install -r rpc_service-requirements.txt
COPY . .
WORKDIR /app
EXPOSE 8080
CMD ["uvicorn", "rpc_service.app:app", "--host", "0.0.0.0", "--port", "8080"]