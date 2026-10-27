FROM python:3.12-slim

# MALLOC_ARENA_MAX=2: コンテナで動く Python のメモリ断片化を抑える
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MALLOC_ARENA_MAX=2

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg libopus0 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY asagao ./asagao

CMD ["python", "-m", "asagao"]
