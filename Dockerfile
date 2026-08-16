FROM python:3.12-slim

# Python's stdout is fully block-buffered (not line-buffered) whenever it
# isn't a real terminal -- which is always true inside a container, since
# `docker logs` reads it via a pipe. Without this, every plain print()
# used for degrade-gracefully diagnostics across this app (aslstats.py,
# asl_audio.py, etc.) can sit invisibly in that buffer for a long time
# instead of showing up in `docker logs` right away -- confirmed live:
# a real connection-failure print() in asl_audio.py produced zero output
# across two separate 5/20-minute `docker logs` windows before this was
# found and fixed.
ENV PYTHONUNBUFFERED=1

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
