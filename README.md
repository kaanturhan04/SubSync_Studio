\# SubSync Studio



AI destekli video/ses lokalizasyon aracı — Whisper (faster-whisper) ile otomatik transkripsiyon, 52+ dile çeviri, ve altyazı gömme (burn-in). Flask tabanlı web arayüzü + REST API.



\## Özellikler



\- 52+ dile otomatik çeviri ve altyazı üretimi

\- Video dosyalarına altyazı gömme (ffmpeg burn-in)

\- Web arayüzü + REST API (`/api/process`)

\- İnsan-döngülü (human-in-the-loop) altyazı editörü — video senkron, cue bazlı düzenleme

\- ROI dashboard — zaman/maliyet tasarrufu tahmini

\- Zero-retention — işlem sonrası dosyalar otomatik silinir (varsayılan 1 saat)

\- Docker desteği (CPU-only; GPU desteği yerel kurulumda mevcut)



\## Kurulum — Docker (önerilen, en hızlı)



```bash

git clone <bu-repo-url>

cd SubSync\_Studio

docker compose up --build

```



Tarayıcıda `http://localhost:5000` adresini aç. İlk çalıştırmada `large-v3` Whisper modeli otomatik indirilir (\~3GB, internet bağlantısı gerekir).



\## Kurulum — Yerel (GPU destekli)



Gereksinimler:

\- Python 3.10+

\- \[ffmpeg](https://ffmpeg.org/download.html) (PATH'e ekli olmalı)

\- NVIDIA GPU + CUDA (opsiyonel, hız için önerilir)



```bash

python -m venv venv

venv\\Scripts\\activate      # Windows

pip install -r requirements.txt

```



Model indirme (ilk çalıştırmada otomatik, ya da manuel):

```bash

python -c "from huggingface\_hub import snapshot\_download; snapshot\_download('Systran/faster-whisper-large-v3')"

```



Çalıştır:

```bash

python app.py

```



\### Ortam Değişkenleri



| Değişken | Varsayılan | Açıklama |

|---|---|---|

| `SUBSYNC\_MODEL\_PATH` | `large-v3` | Yerel model klasör yolu ya da Hugging Face repo ID |

| `SUBSYNC\_DEVICE` | `cuda` | `cuda` (GPU) veya `cpu` |

| `SUBSYNC\_CLEANUP\_DELAY` | `3600` | Zero-retention süresi (saniye) |



\## API Kullanımı



```bash

curl -X POST http://localhost:5000/api/process \\

&#x20; -H "X-API-Key: subsync-demo-key-2026" \\

&#x20; -F "file=@video.mp4" \\

&#x20; -F "langs=EN,DE" \\

&#x20; -F "source\_lang=auto"

```



Detaylı API dokümantasyonu: `/api-docs` sayfası.



\## Lisans



Bu proje kişisel/portföy amaçlı geliştirilmiştir.

