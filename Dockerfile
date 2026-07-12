FROM python:3.12-slim

WORKDIR /app

# ffmpeg bridges RTSP camera feeds to MJPEG for the optional camera cards
# (camera_stream.py) -- not in the base python:slim image by default.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first so this layer is cached unless requirements change
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code -- copies everything not excluded by .dockerignore, so adding a
# new .py module later can't silently drop it from the image the way an
# explicit file list can.
COPY . .

# Hotspot config persists here -- mount a volume to keep it across rebuilds
VOLUME ["/app/data"]

EXPOSE 5000

CMD ["python", "app.py"]
