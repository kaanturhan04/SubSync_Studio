import os
import time
import shutil
import subprocess
from faster_whisper import WhisperModel
from deep_translator import GoogleTranslator

models = {}

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv"}

LANGUAGES = {
    "EN": "english", "DE": "german", "FR": "french", "ES": "spanish",
    "IT": "italian", "PT": "portuguese", "NL": "dutch", "PL": "polish",
    "RU": "russian", "UK": "ukrainian", "CS": "czech", "SK": "slovak",
    "HU": "hungarian", "RO": "romanian", "BG": "bulgarian", "HR": "croatian",
    "SR": "serbian", "SL": "slovenian", "EL": "greek", "SV": "swedish",
    "NO": "norwegian", "DA": "danish", "FI": "finnish", "ET": "estonian",
    "LV": "latvian", "LT": "lithuanian", "AR": "arabic", "HE": "hebrew",
    "FA": "persian", "UR": "urdu", "HI": "hindi", "BN": "bengali",
    "TA": "tamil", "TE": "telugu", "MR": "marathi", "GU": "gujarati",
    "PA": "punjabi", "ZH-CN": "chinese (simplified)", "ZH-TW": "chinese (traditional)",
    "JA": "japanese", "KO": "korean", "TH": "thai", "VI": "vietnamese",
    "ID": "indonesian", "MS": "malay", "TL": "filipino", "SW": "swahili",
    "AF": "afrikaans", "AZ": "azerbaijani", "KA": "georgian", "KK": "kazakh",
    "UZ": "uzbek",
}

WHISPER_TO_GOOGLE = {
    "tr": "tr", "en": "en", "de": "de", "fr": "fr", "es": "es",
    "it": "it", "pt": "pt", "nl": "nl", "pl": "pl", "ru": "ru",
    "uk": "uk", "cs": "cs", "sk": "sk", "hu": "hu", "ro": "ro",
    "bg": "bg", "hr": "hr", "sr": "sr", "sl": "sl", "el": "el",
    "sv": "sv", "no": "no", "da": "da", "fi": "fi", "et": "et",
    "lv": "lv", "lt": "lt", "ar": "ar", "he": "iw", "fa": "fa",
    "ur": "ur", "hi": "hi", "bn": "bn", "ta": "ta", "te": "te",
    "mr": "mr", "gu": "gu", "pa": "pa", "zh": "zh-CN", "ja": "ja",
    "ko": "ko", "th": "th", "vi": "vi", "id": "id", "ms": "ms",
    "tl": "tl", "sw": "sw", "af": "af", "az": "az", "ka": "ka",
    "kk": "kk", "uz": "uz",
}

def is_video(file_path):
    return os.path.splitext(file_path)[1].lower() in VIDEO_EXTENSIONS

def get_model(model_name="large-v3"):
    global models
    if model_name not in models:
        print("[SubSync] Model yukleniyor: " + model_name)
        start = time.time()
        device = os.environ.get("SUBSYNC_DEVICE", "cuda")
        compute_type = "float16" if device == "cuda" else "int8"
        models[model_name] = WhisperModel(model_name, device=device, compute_type=compute_type)
        print("[SubSync] Model hazir -> " + str(round(time.time() - start, 2)) + "s")
    return models[model_name]

def format_time(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

def save_srt(segments, path):
    with open(path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, 1):
            f.write(f"{i}\n{format_time(seg['start'])} --> {format_time(seg['end'])}\n{seg['text'].strip()}\n\n")
    print("[SubSync] SRT kaydedildi: " + path)

def translate_segments(segments, source_lang, target_lang):
    print(f"[SubSync] Ceviri: {source_lang} -> {target_lang}")
    try:
        translator = GoogleTranslator(source=source_lang, target=target_lang)
    except Exception:
        translator = GoogleTranslator(source="auto", target=target_lang)
    result = []
    for seg in segments:
        try:
            text = translator.translate(seg["text"].strip())
        except Exception:
            text = seg["text"].strip()
        result.append({"start": seg["start"], "end": seg["end"], "text": text})
    return result

def burn_subtitles(video_path, srt_path, output_path):
    print("[SubSync] Burn-in: " + output_path)
    tmp_srt = "tmp_subsync_burn.srt"
    shutil.copy2(srt_path, tmp_srt)
    try:
        cmd = [
            "ffmpeg", "-y",
            "-i", os.path.abspath(video_path),
            "-vf", f"subtitles={tmp_srt}:force_style='FontSize=20,PrimaryColour=&Hffffff,OutlineColour=&H000000,Outline=2,Alignment=2'",
            "-c:a", "copy",
            os.path.abspath(output_path)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            print("[SubSync] Burn-in hatasi: " + result.stderr[-300:])
            return False
        print("[SubSync] Burn-in tamamlandi.")
        return True
    except Exception as e:
        print("[SubSync] Burn-in exception: " + str(e))
        return False
    finally:
        if os.path.exists(tmp_srt):
            os.remove(tmp_srt)

def print_languages():
    for code, name in LANGUAGES.items():
        print(f"  {code} - {name}")

if __name__ == "__main__":
    print(f"[SubSync Studio] {len(LANGUAGES)} dil destegi\n")
    print("1 - EN+DE (varsayilan)\n2 - Tum diller\n3 - Dil listesi\n4 - Manuel sec")
    mod = input("\nMod: ").strip()
    if mod == "3":
        print_languages()
        mod = input("Mod: ").strip()
    file_path = input("Dosya yolu: ").strip()
    lang_mod = input("Kaynak dil: 1-Otomatik  2-Manuel: ").strip()
    source_language = None
    if lang_mod == "2":
        source_language = input("Dil kodu (tr/en/de...): ").strip().lower()
    if mod == "2":
        selected = list(LANGUAGES.keys())
    elif mod == "4":
        print_languages()
        secim = input("Dil kodlari (EN,FR,ES): ").strip().upper()
        selected = [x.strip() for x in secim.split(",") if x.strip() in LANGUAGES]
        if not selected:
            selected = ["EN", "DE"]
    else:
        selected = ["EN", "DE"]

    model = get_model(r"C:\Models\faster-whisper-large-v3")
    base = file_path.rsplit(".", 1)[0]

    segments_gen, info = model.transcribe(
        file_path,
        language=source_language,
        task="transcribe",
        beam_size=5,
        temperature=0.0,
        vad_filter=True
    )
    detected = info.language
    print(f"[SubSync] Tespit: {detected}")

    # faster-whisper generator dondurur, bir kez tuketilir -> once dict listesine cevir
    segments = [{"start": s.start, "end": s.end, "text": s.text} for s in segments_gen]

    src_srt = base + f"_{detected.upper()}.srt"
    save_srt(segments, src_srt)
    src_google = WHISPER_TO_GOOGLE.get(detected, "auto")
    for code in selected:
        if code.lower() == detected.lower():
            continue
        tgt = code.lower()
        if code == "ZH-CN": tgt = "zh-CN"
        elif code == "ZH-TW": tgt = "zh-TW"
        segs = translate_segments(segments, src_google, tgt)
        save_srt(segs, base + f"_{code.replace('-','_')}.srt")
    if is_video(file_path):
        burn_subtitles(file_path, src_srt, base + f"_{detected.upper()}_burned.mp4")
    print("[SubSync] Tamamlandi.")