FROM python:3.13.5-alpine
WORKDIR /app
RUN pip install --no-cache-dir websockets==16.0
COPY flight_recorder.py ha_feed.py ./
ENV PYTHONUNBUFFERED=1
CMD ["python", "-u", "/app/ha_feed.py"]
