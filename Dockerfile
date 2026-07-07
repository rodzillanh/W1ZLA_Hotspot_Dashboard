FROM python:3.12-slim

WORKDIR /app

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
