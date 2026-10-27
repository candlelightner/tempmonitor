FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    TM_DATA_DIR=/data NICEGUI_STORAGE_PATH=/data/nicegui HOME=/tmp
RUN groupadd --gid 1000 app && useradd --uid 1000 --gid app --no-create-home app \
    && mkdir /data && chown app:app /data
WORKDIR /app
COPY requirements.lock pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.lock
COPY tempmonitor ./tempmonitor
RUN pip install --no-cache-dir --no-deps .
USER app
CMD ["python", "-m", "tempmonitor.web"]
