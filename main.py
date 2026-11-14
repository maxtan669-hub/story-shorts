import asyncio
import json
import os
import random
import subprocess
import time
import urllib.parse

import edge_tts
import requests

LANG = os.environ.get("LANGUAGE", "hindi").lower()
VOICE = "hi-IN-SwaraNeural" if LANG == "hindi" else "en-US-AriaNeural"
DRY_RUN = os.environ.get("DRY_RUN", "1") == "1"
OUT = "out"

MODELS = [
    os.environ.get("GEMINI_MODEL", ""),
    "gemini-flash-latest",
]

THEMES = [
    "a clever animal outwits a bigger enemy",
    "a poor farmer finds a surprising treasure",
    "a kind child helps a stranger and is rewarded",
    "a magical lamp with a twist",
    "two friends and a lesson about honesty",
    "a greedy king learns humility",
    "a small bird with a big dream",
    "a village mystery solved by a smart girl",
    "a mysterious old man and a gift of wisdom",
    "a lost traveler finds help in an unexpected place",
]


def make_story():
    lang_name = "Hindi (Devanagari script)" if LANG == "hindi" else "English"
    theme = random.choice(THEMES)
    prompt = f"""Write an original short story for a YouTube Short in {lang_name}.
Theme: {theme}.
Rules: exactly 5 scenes, each narration 18-22 words, strong hook in the first line,
a twist or moral at the end, family-friendly, no violence.
Return ONLY JSON in this shape:
{{"title": "max 70 chars, in {lang_name}",
  "description": "2 short lines",
  "tags": ["5 short tags"],
  "scenes": [{{"text": "narration in {lang_name}",
              "image_prompt": "vivid English description of the scene, illustration, no text in image"}}]}}"""
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    key = os.environ["GEMINI_API_KEY"]
    for model in [m for m in MODELS if m]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(2):
            try:
                r = requests.post(url, params={"key": key}, json=body, timeout=120)
                if r.status_code == 404:
                    print("Model not found, trying next:", model)
                    break
                r.raise_for_status()
                text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                story = json.loads(text)
                if len(story["scenes"]) >= 3:
                    print("Used model:", model)
                    return story
            except Exception as e:
                print("Gemini retry:", model, e)
                time.sleep(10)
    try:
        r = requests.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            params={"key": key}, timeout=60,
        )
        names = [m["name"] for m in r.json().get("models", [])]
        print("Available models:", names)
    except Exception as e:
        print("Could not list models:", e)
    raise RuntimeError("Story generation failed")


def make_image(prompt, path):
    style = "vertical 9:16 storybook illustration, vibrant colors, cinematic lighting, no text"
    q = urllib.parse.quote(f"{prompt}, {style}")
    url = (
        f"https://image.pollinations.ai/prompt/{q}"
        f"?width=1080&height=1920&nologo=true&seed={random.randint(1, 999999)}"
    )
    for attempt in range(5):
        try:
            r = requests.get(url, timeout=180)
            if r.ok and r.headers.get("content-type", "").startswith("image"):
                with open(path, "wb") as f:
                    f.write(r.content)
                return
        except requests.RequestException as e:
            print("Image retry:", e)
        time.sleep(10)
    raise RuntimeError("Image generation failed")


async def make_voice(text, path):
    await edge_tts.Communicate(text, VOICE).save(path)


def run(cmd):
    subprocess.run(cmd, check=True)


def duration(path):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path]
    )
    return float(out)


def build_video(story):
    os.makedirs(OUT, exist_ok=True)
    clips = []
    for i, scene in enumerate(story["scenes"]):
        img = f"{OUT}/img{i}.jpg"
        aud = f"{OUT}/aud{i}.mp3"
        clip = f"{OUT}/clip{i}.mp4"
        make_image(scene["image_prompt"], img)
        asyncio.run(make_voice(scene["text"], aud))
        run([
            "ffmpeg", "-y", "-loop", "1", "-i", img, "-i", aud,
            "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,format=yuv420p",
            "-c:v", "libx264", "-r", "30", "-c:a", "aac", "-ar", "44100", "-ac", "2",
            "-shortest", clip,
        ])
        clips.append(os.path.abspath(clip))
    lst = f"{OUT}/list.txt"
    with open(lst, "w") as f:
        for c in clips:
            f.write(f"file '{c}'\n")
    final = f"{OUT}/final.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", final])
    print("Video length (sec):", round(duration(final), 1))
    return final


def upload(path, story):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = Credentials(
        None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    yt = build("youtube", "v3", credentials=creds)
    title = (story["title"][:90] + " #Shorts")[:100]
    body = {
        "snippet": {
            "title": title,
            "description": story["description"] + "\n\n#Shorts #Story",
            "tags": story.get("tags", []) + ["Shorts"],
            "categoryId": "24",
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": True,
        },
    }
    media = MediaFileUpload(path, mimetype="video/mp4", resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    print("Uploaded! Video id:", resp["id"])


if __name__ == "__main__":
    story = make_story()
    print("Title:", story["title"])
    video = build_video(story)
    if DRY_RUN:
        print("DRY_RUN on: video saved, not uploaded.")
    else:
        upload(video, story)
