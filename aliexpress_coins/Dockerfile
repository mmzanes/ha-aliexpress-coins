ARG BUILD_FROM
FROM ${BUILD_FROM}
RUN apt-get update \
 && apt-get install -y --no-install-recommends xvfb x11vnc novnc \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir --break-system-packages playwright==1.52.0 aiohttp==3.10.10
COPY app.py panel.html /app/
CMD ["python3", "-u", "/app/app.py"]
