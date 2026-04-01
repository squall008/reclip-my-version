FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg curl ca-certificates unzip chromium && \
    curl -fsSL https://deno.land/install.sh | sh && \
    rm -rf /var/lib/apt/lists/*

# Force no-sandbox for Chromium to run as root in Docker without errors
RUN mv /usr/bin/chromium /usr/bin/chromium-orig && \
    echo '#!/bin/sh\nexec /usr/bin/chromium-orig --no-sandbox --disable-dev-shm-usage "$@"' > /usr/bin/chromium && \
    chmod +x /usr/bin/chromium

ENV DENO_INSTALL="/root/.deno"
ENV PATH="$DENO_INSTALL/bin:$PATH"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8899
ENV HOST=0.0.0.0
CMD ["python", "app.py"]
