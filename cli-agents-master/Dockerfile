FROM python:3.12-slim

# Install LibreOffice for formula recalculation
RUN apt-get update && \
    apt-get install -y --no-install-recommends libreoffice-calc-nogui && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install the package
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .

# Credentials come from .env or environment variables at runtime
# docker run --env-file .env excel-agent ...

ENTRYPOINT ["excel-agent"]
