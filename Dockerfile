FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/data/jukebox.db \
    CONFIG_FILE=/data/config.env \
    OPEN_BROWSER=0

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static
COPY run.py .

# Run as an unprivileged user; the database lives in the /data volume.
RUN useradd --create-home jukebox && mkdir /data && chown jukebox /data
USER jukebox
VOLUME /data

EXPOSE 8000
CMD ["python", "run.py"]
