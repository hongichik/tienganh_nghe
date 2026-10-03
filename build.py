#!/usr/bin/env python3
"""Build dữ liệu luyện nghe cho mọi đề *.txt trong thư mục này.
Mỗi đề: tải audio, chuyển giọng nói -> text (Google free), dịch sang tiếng Việt (Google free).
Chạy: python3 build.py              (tất cả *.txt)
      python3 build.py De02.txt     (chỉ một số đề)
Kết quả: data/<slug>/audio/*.mp3, data/<slug>/data.json (cache), data/<slug>/data.js, data/index.js
Đáp án sai bổ sung (web chọn ngẫu nhiên 2 đáp án sai mỗi lần): <slug>.extra.json dạng {"1": ["...", "..."]}.
Đề chưa có đáp án: tạo file <tên đề>.answers.json dạng {"1": "A", ...} cạnh file .txt (đánh dấu là đáp án suy ra).
Chạy lại an toàn: bỏ qua phần đã có (xoá data/<slug>/data.json để làm lại).
"""
import glob
import json
import os
import re
import sys
import tempfile
import time

import requests
import speech_recognition as sr
from pydub import AudioSegment
from pydub.silence import split_on_silence

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")


def slug_of(path):
    s = os.path.splitext(os.path.basename(path))[0]
    return re.sub(r"_dap_an$", "", s)


def parse(path):
    text = open(path, encoding="utf-8").read()
    title = text.strip().splitlines()[0].strip()
    # "APTIS LISTENING – PART 1.5 (50 câu)" -> "APTIS LISTENING PART 1 – ĐỀ 05 (50 câu)"
    m = re.match(r"(.*?)\s*[–-]?\s*PART\s*(\d+)\.(\d+)\s*(.*)$", title)
    if m:
        title = "%s PART %s – ĐỀ %02d %s" % (m.group(1), m.group(2), int(m.group(3)), m.group(4))
        title = title.strip()
    items = []
    for block in re.split(r"\n(?=Câu \d+\.)", text):
        m = re.match(r"Câu (\d+)\.\s*(.+)", block)
        if not m:
            continue
        opts = re.findall(r"^\s*([A-D])\.\s*(.+)$", block, re.M)
        ans = re.search(r"Đáp án:\s*([A-D])\b", block)
        audio = re.search(r"Audio:\s*(\S+)", block)
        items.append({
            "id": int(m.group(1)),
            "question": m.group(2).strip(),
            "options": [{"key": k, "text": t.strip()} for k, t in opts],
            "answer": ans.group(1) if ans else "",
            "url": audio.group(1) if audio else "",
        })
    return title, items


def download(q, slug):
    if not q["url"]:
        return ""
    rel = "data/%s/audio/q%02d.mp3" % (slug, q["id"])
    path = os.path.join(ROOT, rel)
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        r = requests.get(q["url"], timeout=60)
        r.raise_for_status()
        open(path, "wb").write(r.content)
    return rel


def transcribe(mp3_path):
    audio = AudioSegment.from_file(mp3_path).set_channels(1).set_frame_rate(16000)
    pieces = split_on_silence(audio, min_silence_len=400, silence_thresh=audio.dBFS - 16, keep_silence=250)
    # gộp các đoạn ngắn thành chunk ~15s (Google free giới hạn ~60s/lần)
    chunks, cur = [], AudioSegment.empty()
    for p in pieces or [audio]:
        if len(cur) + len(p) > 15000 and len(cur) > 0:
            chunks.append(cur)
            cur = AudioSegment.empty()
        cur += p
    if len(cur):
        chunks.append(cur)
    rec = sr.Recognizer()
    out = []
    for c in chunks:
        # chunk đơn lẻ vẫn quá dài thì cắt cứng
        for i in range(0, len(c), 50000):
            seg = c[i:i + 50000]
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                seg.export(f.name, format="wav")
                tmp = f.name
            try:
                with sr.AudioFile(tmp) as s:
                    data = rec.record(s)
                for attempt in range(3):
                    try:
                        out.append(rec.recognize_google(data, language="en-GB"))
                        break
                    except sr.UnknownValueError:
                        break
                    except sr.RequestError:
                        time.sleep(3)
            finally:
                os.remove(tmp)
    t = " ".join(out).strip()
    return t[:1].upper() + t[1:] if t else ""


def translate(text):
    if not text:
        return ""
    r = requests.get(
        "https://translate.googleapis.com/translate_a/single",
        params={"client": "gtx", "sl": "en", "tl": "vi", "dt": "t", "q": text},
        timeout=30,
    )
    r.raise_for_status()
    return "".join(s[0] for s in r.json()[0] if s[0])


def write_js(path, var, key, obj):
    with open(path, "w", encoding="utf-8") as f:
        f.write("window.%s = window.%s || {};\nwindow.%s[%s] = " % (var, var, var, json.dumps(key)))
        json.dump(obj, f, ensure_ascii=False)
        f.write(";\n")


def build(src):
    slug = slug_of(src)
    out_dir = os.path.join(DATA_DIR, slug)
    os.makedirs(os.path.join(out_dir, "audio"), exist_ok=True)
    cache_path = os.path.join(out_dir, "data.json")
    title, items = parse(src)

    guess = {}
    ans_file = os.path.splitext(src)[0] + ".answers.json"
    if os.path.exists(ans_file):
        guess = {int(k): v for k, v in json.load(open(ans_file, encoding="utf-8")).items()}

    # đáp án sai bổ sung: <slug>.extra.json dạng {"1": ["...", "..."]}
    extra = {}
    extra_file = os.path.join(os.path.dirname(src), slug + ".extra.json")
    if os.path.exists(extra_file):
        extra = {int(k): v for k, v in json.load(open(extra_file, encoding="utf-8")).items()}

    cache = {}
    if os.path.exists(cache_path):
        cache = {q["id"]: q for q in json.load(open(cache_path, encoding="utf-8"))["items"]}

    print("== %s (%d câu)" % (title, len(items)), flush=True)
    for q in items:
        old = cache.get(q["id"], {})
        for k in ("transcript", "translation", "question_vi"):
            q[k] = old.get(k, "")
        if not q["answer"] and guess.get(q["id"]):
            q["answer"] = guess[q["id"]]
            q["answer_guess"] = True
        if extra.get(q["id"]):
            q["extra"] = extra[q["id"]]
        try:
            q["audio"] = download(q, slug)
            if not q["transcript"] and q["audio"]:
                q["transcript"] = transcribe(os.path.join(ROOT, q["audio"]))
            if q["transcript"] and not q["translation"]:
                q["translation"] = translate(q["transcript"])
            if not q["question_vi"]:
                q["question_vi"] = translate(q["question"])
            print("Câu %d OK: %s" % (q["id"], q["transcript"][:70]), flush=True)
        except Exception as e:
            print("Câu %d LỖI: %s" % (q["id"], e), flush=True)
        json.dump({"title": title, "items": items}, open(cache_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    write_js(os.path.join(out_dir, "data.js"), "QUIZ_REG", slug, {"slug": slug, "title": title, "items": items})
    missing = [q["id"] for q in items if not q["transcript"]]
    no_ans = [q["id"] for q in items if not q["answer"]]
    print("Xong %s. Thiếu transcript: %s. Thiếu đáp án: %s" % (slug, missing or "không", no_ans or "không"), flush=True)


def write_index():
    tests = []
    for p in sorted(glob.glob(os.path.join(DATA_DIR, "*", "data.json"))):
        d = json.load(open(p, encoding="utf-8"))
        slug = os.path.basename(os.path.dirname(p))
        tests.append({
            "slug": slug,
            "title": d["title"],
            "count": len(d["items"]),
            "guess": any(q.get("answer_guess") for q in d["items"]),
            "no_answer": sum(1 for q in d["items"] if not q.get("answer")),
        })
    with open(os.path.join(DATA_DIR, "index.js"), "w", encoding="utf-8") as f:
        f.write("window.QUIZ_INDEX = ")
        json.dump(tests, f, ensure_ascii=False)
        f.write(";\n")


def main():
    srcs = sys.argv[1:] or sorted(glob.glob(os.path.join(ROOT, "*.txt")))
    for src in srcs:
        build(os.path.join(ROOT, src) if not os.path.isabs(src) else src)
    write_index()


if __name__ == "__main__":
    main()
