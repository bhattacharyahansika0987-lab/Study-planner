"""
Flow Space backend (Flask, Vercel-ready).

Routes
  GET  /            -> serves templates/index.html
  GET  /api/status  -> {"ai": bool}  (is ANTHROPIC_API_KEY configured?)
  POST /api/chat    -> follow-up questions from the study chatbot
  POST /api/plan    -> Claude-generated day-by-day study timetable (JSON)

The front-end also ships a complete offline planner, so every feature keeps
working when no API key is set or a request fails.

Environment variables
  ANTHROPIC_API_KEY   your key (Vercel > Project > Settings > Environment Variables)
  ANTHROPIC_MODEL     optional, defaults to claude-sonnet-5-5
"""

import json
import os
import re

from flask import Flask, jsonify, render_template, request

app = Flask(__name__, template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024  # 512 KB request cap

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
MAX_SUBJECTS = 8
MAX_TOPICS_PER_SUBJECT = 80
MAX_PLAN_DAYS = 21
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

CHAT_SYSTEM = (
    "You are the AI Study Assistant inside Flow Space, a cozy student planner. "
    "Help students with exam preparation: study strategy, how to split a syllabus, "
    "revision techniques, motivation, and adjusting timetables. Be warm, concise "
    "(under 180 words unless asked for more) and practical. Use short paragraphs or "
    "simple bullet points. If the student shares a plan summary, refer to it. "
    "Never invent exam dates or syllabus content the student has not given you."
)

PLAN_SYSTEM = (
    "You are an expert exam-prep coach. You build realistic, balanced day-by-day "
    "study timetables. You reply with ONE JSON object only: no prose, no markdown "
    "fences."
)


def _client():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        import anthropic  # imported lazily so the app still boots without it
    except ImportError:
        return None
    return anthropic.Anthropic(api_key=key, timeout=55.0, max_retries=1)


def _text_of(message):
    return "".join(b.text for b in message.content if getattr(b, "type", "") == "text").strip()


def _extract_json(raw):
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I)
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object found in model output")
    return json.loads(raw[start : end + 1])


def _clean(text, limit):
    return str(text or "").replace("\x00", "").strip()[:limit]


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/status")
def status():
    return jsonify(ai=_client() is not None, model=MODEL)


@app.post("/api/chat")
def chat():
    client = _client()
    if client is None:
        return jsonify(error="AI is not configured on this server."), 503

    body = request.get_json(silent=True) or {}
    raw_messages = body.get("messages") or []
    messages = []
    for m in raw_messages[-14:]:
        role = "assistant" if m.get("role") == "assistant" else "user"
        content = _clean(m.get("content"), 4000)
        if not content:
            continue
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"] += "\n" + content
        else:
            messages.append({"role": role, "content": content})
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    if not messages:
        return jsonify(error="Say something first!"), 400

    system = CHAT_SYSTEM
    context = _clean(body.get("context"), 6000)
    if context:
        system += "\n\nCurrent student context:\n" + context

    try:
        resp = client.messages.create(model=MODEL, max_tokens=700, system=system, messages=messages)
        return jsonify(reply=_text_of(resp))
    except Exception as exc:  # noqa: BLE001 - surface a friendly error to the UI
        app.logger.exception("chat failed")
        return jsonify(error="The AI is unavailable right now (%s)." % type(exc).__name__), 502


@app.post("/api/plan")
def plan():
    client = _client()
    if client is None:
        return jsonify(error="AI is not configured on this server."), 503

    body = request.get_json(silent=True) or {}
    dates = [d for d in (body.get("dates") or []) if isinstance(d, str) and DATE_RE.match(d)]
    if not dates or len(dates) > MAX_PLAN_DAYS:
        return jsonify(error="AI plans support 1-%d days." % MAX_PLAN_DAYS), 400

    try:
        hours = max(1, min(12, int(body.get("hoursPerDay", 3))))
    except (TypeError, ValueError):
        hours = 3

    subjects = []
    for s in (body.get("subjects") or [])[:MAX_SUBJECTS]:
        name = _clean(s.get("name"), 60)
        if not name:
            continue
        exam = s.get("examDate") if DATE_RE.match(str(s.get("examDate") or "")) else None
        topics = [_clean(t, 160) for t in (s.get("topics") or [])[:MAX_TOPICS_PER_SUBJECT]]
        subjects.append({"name": name, "examDate": exam, "topics": [t for t in topics if t]})
    if not subjects:
        return jsonify(error="No subjects supplied."), 400

    mode = body.get("mode") if body.get("mode") in ("single", "week", "countdown") else "week"
    payload = {"mode": mode, "dates": dates, "studyHoursPerDay": hours, "subjects": subjects}

    prompt = (
        "Create a study timetable from this JSON input.\n\n"
        + json.dumps(payload, ensure_ascii=False)
        + "\n\nRules:\n"
        "- Use EXACTLY the dates listed in `dates`, in order, one entry each.\n"
        "- Each day has about `studyHoursPerDay` hours of study. Sessions are 40-60 minutes; "
        "put the start-end clock time in `time` (start at 09:00, add short breaks, e.g. \"09:00-09:50\").\n"
        "- Estimate the weight of each topic from its wording and spread topics so the most "
        "urgent exams (nearest `examDate`) get priority while every subject keeps moving.\n"
        "- Interleave subjects within a day; avoid more than 2 consecutive sessions of one subject.\n"
        "- The day before an exam is revision + practice for that subject. Mark exam days with a "
        "session of kind \"exam\" (mins 0) and no new topics for that subject afterwards.\n"
        "- Add an active-recall session near the end of busy days and a weekly review for plans of 10+ days.\n"
        "- Only reference topics that appear in the input. If a subject has no topics, use sensible "
        "generic study tasks and say so in `warnings`.\n"
        "- If the workload cannot realistically fit, say so in `warnings` and suggest hours per day.\n\n"
        "Return JSON with this exact shape:\n"
        "{\"summary\": string (max 2 sentences), \"warnings\": [string],\n"
        " \"days\": [{\"date\": \"YYYY-MM-DD\", \"sessions\": [\n"
        "   {\"time\": string, \"subject\": string (must match an input subject name), "
        "\"task\": string, \"kind\": \"learn\"|\"revise\"|\"practice\"|\"recall\"|\"review\"|\"exam\", "
        "\"mins\": integer}]}]}"
    )

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=8000,
            system=PLAN_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        data = _extract_json(_text_of(resp))
    except Exception as exc:  # noqa: BLE001
        app.logger.exception("plan failed")
        return jsonify(error="Could not generate an AI plan (%s)." % type(exc).__name__), 502

    days = data.get("days")
    if not isinstance(days, list) or not days:
        return jsonify(error="The AI returned an unexpected format."), 502
    return jsonify(
        summary=_clean(data.get("summary"), 600),
        warnings=[_clean(w, 300) for w in (data.get("warnings") or []) if w][:6],
        days=days,
    )


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))