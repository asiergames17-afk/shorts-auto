"""shorts-auto: genera 1 YouTube Short al día (guion + voz + imágenes + subtítulos)
y lo sube a YouTube como PRIVADO. Todo con servicios gratuitos."""
import asyncio
import html
import json
import math
import os
import re
import subprocess
import time
from pathlib import Path

import edge_tts
import requests

ROOT = Path(__file__).parent
OUT = ROOT / "output"
WORK = ROOT / "work"
TOPICS_FILE = ROOT / "used_topics.txt"

NICHE = os.environ.get("NICHE", "curiosidades históricas")
VOICE = os.environ.get("TTS_VOICE", "es-ES-AlvaroNeural")
FALLBACK_MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-flash-latest"]
UA = "shorts-auto/1.0 (https://github.com/asiergames17-afk/shorts-auto)"
W, H, FPS = 1080, 1920, 30


# ---------------------------------------------------------------- utilidades
def run(cmd):
    r = subprocess.run(cmd, cwd=WORK, capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        print(r.stderr[-1500:])
        raise RuntimeError(f"Falló: {' '.join(cmd[:3])}...")
    return r.stdout


def audio_duration(name):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=nw=1:nk=1", name])
    return float(out.strip())


# -------------------------------------------------------------------- Gemini
def candidate_models(key):
    """Pregunta a Google qué modelos 'flash' hay disponibles y los ordena."""
    names = []
    try:
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                         headers={"x-goog-api-key": key}, params={"pageSize": 200}, timeout=30)
        r.raise_for_status()
        for m in r.json().get("models", []):
            n = m["name"].replace("models/", "")
            if "generateContent" not in m.get("supportedGenerationMethods", []):
                continue
            if "flash" not in n or any(x in n for x in (
                    "image", "tts", "live", "audio", "embedding", "robotics",
                    "computer", "thinking", "native")):
                continue
            names.append(n)
    except Exception as e:  # noqa: BLE001
        print("[Gemini] No pude listar los modelos:", e)
    names.sort(reverse=True)  # versiones más nuevas primero
    names.sort(key=lambda n: (("preview" in n or "exp" in n), ("lite" in n)))
    ordered = []
    for n in [os.environ.get("GEMINI_MODEL")] + names + FALLBACK_MODELS:
        if n and n not in ordered:
            ordered.append(n)
    return ordered[:6]


def gemini_json(prompt):
    key = os.environ["GEMINI_API_KEY"]
    models = candidate_models(key)
    print("[Gemini] Modelos a probar:", ", ".join(models))
    last = None
    for ronda in range(4):
        for model in models:
            try:
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                    json={"contents": [{"parts": [{"text": prompt}]}],
                          "generationConfig": {"responseMimeType": "application/json",
                                               "temperature": 0.9}},
                    timeout=120)
            except requests.RequestException as e:
                last = f"{model}: {e}"
                print("[Gemini]", last)
                continue
            if r.status_code != 200:
                last = f"{model}: HTTP {r.status_code} {r.text[:200]}"
                print("[Gemini]", last)
                continue
            try:
                parts = r.json()["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
                return json.loads(text)
            except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
                last = f"{model}: respuesta no válida ({e})"
                print("[Gemini]", last)
        if ronda < 3:
            espera = 45 * (ronda + 1)
            print(f"[Gemini] Todos fallaron. Reintento en {espera}s...")
            time.sleep(espera)
    raise RuntimeError(f"Gemini falló: {last}")


def make_plan(used):
    prompt = f"""Eres guionista de YouTube Shorts en español de España neutro.
Nicho: {NICHE}. Público: jóvenes de 18 a 30 años.
Elige UN tema nuevo y fascinante que NO esté en esta lista: {used or 'ninguno todavía'}.
Reglas:
- Usa solo hechos históricos bien documentados. No inventes citas, cifras ni nombres. Si algo es leyenda, dilo ("según la tradición").
- Guion hablado de 115 a 135 palabras (45-55 segundos), sin emojis ni acotaciones, solo texto para locutar.
- Estructura: gancho en la primera frase (máximo 12 palabras, sorprendente), curiosidad o problema, desarrollo rápido con frases cortas y cierre con llamada a la acción sin fricción (por ejemplo "Sígueme para la parte 2").
- 8 búsquedas de imágenes EN INGLÉS para Wikimedia Commons: lugares, objetos, pinturas, grabados o personajes reales y concretos relacionados, en orden de aparición. Cada búsqueda de 2 a 4 palabras.
Devuelve SOLO un JSON con esta forma exacta:
{{"topic": "tema en pocas palabras", "script": "texto completo a locutar", "image_queries": ["..."], "title": "máximo 48 caracteres, impactante", "description": "2-3 frases SEO en español", "hashtags": ["#historia", "..."]}}
Exactamente 5 hashtags."""
    plan = gemini_json(prompt)
    for k in ("topic", "script", "image_queries", "title", "description", "hashtags"):
        if k not in plan:
            raise RuntimeError(f"Falta '{k}' en la respuesta de Gemini")
    plan["title"] = plan["title"].strip()[:50]
    plan["hashtags"] = [h if h.startswith("#") else f"#{h}" for h in plan["hashtags"]][:5]
    return plan


# ------------------------------------------------------- Wikimedia Commons
def license_ok(lic):
    low = lic.lower()
    if re.search(r"(^|[-\s])(nc|nd)([-\s]|$)", low):
        return False
    return ("public domain" in low or low.startswith("pd") or "cc0" in low
            or low.startswith("cc by"))


def commons_images(queries, need):
    files, credits, seen = [], [], set()
    for q in queries:
        if len(files) >= need:
            break
        try:
            r = requests.get(
                "https://commons.wikimedia.org/w/api.php", headers={"User-Agent": UA},
                params={"action": "query", "format": "json", "generator": "search",
                        "gsrnamespace": 6, "gsrsearch": f"{q} filetype:bitmap",
                        "gsrlimit": 12, "prop": "imageinfo",
                        "iiprop": "url|mime|size|extmetadata", "iiurlwidth": 1280},
                timeout=30)
            pages = r.json().get("query", {}).get("pages", {})
        except Exception as e:  # noqa: BLE001
            print(f"Commons falló para '{q}': {e}")
            continue
        for p in sorted(pages.values(), key=lambda x: x.get("index", 99)):
            info = (p.get("imageinfo") or [{}])[0]
            meta = info.get("extmetadata", {})
            lic = meta.get("LicenseShortName", {}).get("value", "")
            url = info.get("thumburl") or info.get("url")
            if (not url or url in seen or not license_ok(lic)
                    or info.get("mime") not in ("image/jpeg", "image/png")
                    or info.get("width", 0) < 800 or info.get("height", 0) < 600):
                continue
            try:
                img = requests.get(url, headers={"User-Agent": UA}, timeout=60)
                img.raise_for_status()
            except requests.RequestException:
                continue
            seen.add(url)
            name = f"img{len(files)}.jpg"
            (WORK / name).write_bytes(img.content)
            artist = re.sub(r"<[^>]+>", "", html.unescape(
                meta.get("Artist", {}).get("value", "autor desconocido"))).strip()[:80]
            title = p.get("title", "").replace("File:", "")[:80]
            files.append(name)
            credits.append(f"{title} - {artist} ({lic})")
            break
        time.sleep(0.5)
    return files, credits


# ------------------------------------------------------------- voz y subtítulos
async def tts(text, path):
    kwargs = {"rate": "+4%"}
    try:
        comm = edge_tts.Communicate(text, VOICE, boundary="WordBoundary", **kwargs)
    except TypeError:
        comm = edge_tts.Communicate(text, VOICE, **kwargs)
    words = []
    with open(path, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                s = chunk["offset"] / 1e7
                words.append((s, s + chunk["duration"] / 1e7, chunk["text"]))
    return words


def make_voice(text):
    for i in range(3):
        try:
            return asyncio.run(asyncio.wait_for(tts(text, WORK / "voice.mp3"), 180))
        except Exception as e:  # noqa: BLE001
            print(f"[Voz] intento {i + 1} falló: {e!r}")
            time.sleep(5)
    raise RuntimeError("La voz (edge-tts) falló 3 veces")


def proportional_words(script, dur):
    ws = script.split()
    total = sum(len(w) + 2 for w in ws)
    t, out = 0.0, []
    for w in ws:
        d = dur * (len(w) + 2) / total
        out.append((t, t + d, w))
        t += d
    return out


def ass_time(t):
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def build_ass(words, path):
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,84,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,2,2,60,60,520,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    chunks = [words[i:i + 3] for i in range(0, len(words), 3)]
    lines = []
    for i, ch in enumerate(chunks):
        start = ch[0][0]
        end = chunks[i + 1][0][0] if i + 1 < len(chunks) else ch[-1][1] + 0.3
        text = " ".join(w[2] for w in ch).upper().replace("\n", " ")
        lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Default,,0,0,0,,{text}")
    (WORK / path).write_text(header + "\n".join(lines) + "\n", encoding="utf-8")


# -------------------------------------------------------------------- vídeo
def make_clip(i, img, d):
    frames = max(1, round(d * FPS))
    out = f"clip{i}.mp4"
    if img:
        z = f"1+0.25*on/{frames}" if i % 2 == 0 else f"1.25-0.25*on/{frames}"
        vf = (f"split[a][b];"
              f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=30:5[bg];"
              f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
              f"[bg][fg]overlay=(W-w)/2:(H-h)/2,scale=1620:2880,"
              f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps={FPS},"
              f"format=yuv420p")
        cmd = ["ffmpeg", "-y", "-i", img, "-vf", vf, "-frames:v", str(frames)]
    else:
        cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i",
               f"color=c=0x16213e:s={W}x{H}:r={FPS}:d={d:.3f}", "-vf", "format=yuv420p"]
    run(cmd + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", out])
    return out


def build_video(imgs, dur, ass_name):
    n = max(6, math.ceil(dur / 6))
    per = dur / n
    clips = [make_clip(i, imgs[i % len(imgs)] if imgs else None, per) for i in range(n)]
    (WORK / "list.txt").write_text("".join(f"file '{c}'\n" for c in clips))
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy", "raw.mp4"])
    run(["ffmpeg", "-y", "-i", "raw.mp4", "-i", "voice.mp3", "-vf", f"ass={ass_name}",
         "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-shortest", "final.mp4"])
    return WORK / "final.mp4"


# ------------------------------------------------------------------- YouTube
def upload_youtube(path, title, description, tags):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    creds = Credentials(
        token=None, refresh_token=os.environ["YT_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YT_CLIENT_ID"], client_secret=os.environ["YT_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/youtube.upload"])
    creds.refresh(Request())
    yt = build("youtube", "v3", credentials=creds)
    body = {"snippet": {"title": title, "description": description[:4900], "tags": tags,
                        "categoryId": "27", "defaultLanguage": "es",
                        "defaultAudioLanguage": "es"},
            "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False}}
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(str(path), chunksize=-1, resumable=True))
    resp, fallos = None, 0
    while resp is None:
        try:
            _, resp = req.next_chunk()
        except HttpError as e:
            if e.resp.status in (500, 502, 503, 504) and fallos < 5:
                fallos += 1
                time.sleep(5 * fallos)
                continue
            raise
    print("Subido a YouTube (privado). ID:", resp["id"])


# ---------------------------------------------------------------------- main
def main():
    OUT.mkdir(exist_ok=True)
    WORK.mkdir(exist_ok=True)
    used_list = TOPICS_FILE.read_text(encoding="utf-8").splitlines() if TOPICS_FILE.exists() else []
    plan = make_plan("; ".join(used_list[-80:]))
    print("Tema:", plan["topic"])

    words = make_voice(plan["script"])
    dur = audio_duration("voice.mp3")
    if not words:
        words = proportional_words(plan["script"], dur)
    build_ass(words, "subs.ass")

    n = max(6, math.ceil(dur / 6))
    imgs, credits = commons_images(plan["image_queries"] + [plan["topic"]], n)
    print(f"{len(imgs)} imágenes encontradas, duración {dur:.1f}s")

    final = build_video(imgs, dur, "subs.ass")
    (OUT / "short.mp4").write_bytes(final.read_bytes())

    tags = " ".join(plan["hashtags"])
    desc = (f"{plan['description']}\n\n{tags} #Shorts\n\n"
            "Imágenes: Wikimedia Commons\n" + "\n".join(f"- {c}" for c in credits))
    (OUT / "metadata.txt").write_text(
        f"TÍTULO:\n{plan['title']}\n\nDESCRIPCIÓN:\n{desc}\n\nHASHTAGS:\n{tags}\n\n"
        f"GUION:\n{plan['script']}\n", encoding="utf-8")

    needed = ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN")
    if all(os.environ.get(k) for k in needed):
        upload_youtube(OUT / "short.mp4", plan["title"], desc,
                       [h.lstrip("#") for h in plan["hashtags"]])
    else:
        print("Sin claves de YouTube: vídeo generado pero NO subido.")

    with open(TOPICS_FILE, "a", encoding="utf-8") as f:
        f.write(plan["topic"].replace("\n", " ") + "\n")


if __name__ == "__main__":
    main()
