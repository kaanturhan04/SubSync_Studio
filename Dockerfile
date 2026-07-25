FROM python:3.13-slim

# ffmpeg kurulumu (apt uzerinden, Linux paket yoneticisi)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Once sadece requirements'i kopyala (Docker layer cache icin verimli)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Uygulama kodunun tamami
COPY . .

# Runtime klasorleri
RUN mkdir -p uploads outputs

# CPU-only calisma modu (GPU passthrough sonraki asama)
ENV SUBSYNC_DEVICE=cpu

EXPOSE 5000

CMD ["python", "app.py"]