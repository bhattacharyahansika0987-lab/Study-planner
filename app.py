import json
import os
import sqlite3
import urllib.request
from datetime import date, timedelta

from flask import Flask, g, jsonify, render_template, request

app = Flask(__name__)
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "planner.db")
FIELDS = ("title", "subject_id", "date", "start_time", "duration", "done")


def conn():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d:
        d.close()


def q(sql, args=(), one=False, commit=False):
    cur = conn().execute(sql, args)
    if commit:
        conn().commit()
        return cur.lastrowid
    rows = [dict(r) for r in cur.fetchall()]
    return (rows[0] if rows else None) if one else rows


def init_db():
    with sqlite3.connect(DB) as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS subjects(
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, color TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks(
                id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
                subject_id INTEGER, date TEXT NOT NULL, start_time TEXT DEFAULT '09:00',
                duration INTEGER DEFAULT 60, done INTEGER DEFAULT 0);
            """
        )
        if not c.execute("SELECT 1 FROM subjects").fetchone():
            c.executemany(
                "INSERT INTO subjects(name,color) VALUES(?,?)",
                [("Mathematics", "#6D8CFF"), ("Biology", "#34D399"), ("History", "#F59E0B")],
            )


init_db()


@app.get("/")
def index():
    return render_template("index.html")


# ---------- subjects ----------
@app.get("/api/subjects")
def list_subjects():
    return jsonify(q("SELECT * FROM subjects ORDER BY name"))


@app.post("/api/subjects")
def add_subject():
    d = request.get_json(force=True)
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify(error="Name is required"), 400
    i = q("INSERT INTO subjects(name,color) VALUES(?,?)", (name, d.get("color", "#6D8CFF")), commit=True)
    return jsonify(q("SELECT * FROM subjects WHERE id=?", (i,), one=True)), 201


@app.delete("/api/subjects/<int:i>")
def delete_subject(i):
    q("DELETE FROM tasks WHERE subject_id=?", (i,), commit=True)
    q("DELETE FROM subjects WHERE id=?", (i,), commit=True)
    return "", 204


# ---------- tasks ----------
def insert_task(d):
    return q(
        "INSERT INTO tasks(title,subject_id,date,start_time,duration,done) VALUES(?,?,?,?,?,0)",
        (d["title"].strip(), d.get("subject_id"), d["date"], d.get("start_time", "09:00"),
         int(d.get("duration", 60))),
        commit=True,
    )


@app.get("/api/tasks")
def list_tasks():
    return jsonify(q("SELECT * FROM tasks ORDER BY date, start_time"))


@app.post("/api/tasks")
def add_task():
    d = request.get_json(force=True)
    if not (d.get("title") or "").strip() or not d.get("date"):
        return jsonify(error="Title and date are required"), 400
    i = insert_task(d)
    return jsonify(q("SELECT * FROM tasks WHERE id=?", (i,), one=True)), 201


@app.patch("/api/tasks/<int:i>")
def update_task(i):
    d = request.get_json(force=True)
    cols = [f for f in FIELDS if f in d]
    if cols:
        q(f"UPDATE tasks SET {','.join(c + '=?' for c in cols)} WHERE id=?",
          [d[c] for c in cols] + [i], commit=True)
    return jsonify(q("SELECT * FROM tasks WHERE id=?", (i,), one=True))


@app.delete("/api/tasks/<int:i>")
def delete_task(i):
    q("DELETE FROM tasks WHERE id=?", (i,), commit=True)
    return "", 204


# ---------- AI planner ----------
PHASES = ["Learn core concepts", "Practice problems", "Review weak spots",
          "Active recall quiz", "Past-paper session"]


def heuristic_plan(name, exam, hours):
    start = date.today()
    end = min(date.fromisoformat(exam), start + timedelta(days=21))
    n = (end - start).days
    plan = []
    for i in range(n + 1):
        title = f"{name}: Final review" if i == n and n > 0 else f"{name}: {PHASES[i % len(PHASES)]}"
        plan.append({"title": title, "date": (start + timedelta(days=i)).isoformat(),
                     "start_time": "18:00", "duration": int(hours * 60)})
    return plan


def claude_plan(name, exam, hours):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    prompt = (f"Today is {date.today()}. Build a study plan for '{name}' with an exam on {exam}, "
              f"about {hours} hours per day, at most 21 days. Reply with only a JSON array of "
              'objects: {"title","date":"YYYY-MM-DD","start_time":"HH:MM","duration":minutes}.')
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps({"model": "claude-sonnet-4-6", "max_tokens": 2000,
                         "messages": [{"role": "user", "content": prompt}]}).encode(),
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            text = json.load(r)["content"][0]["text"]
        items = json.loads(text[text.find("["): text.rfind("]") + 1])
        return [i for i in items if i.get("title") and date.fromisoformat(i["date"])][:60]
    except Exception:
        return None


@app.post("/api/ai-plan")
def ai_plan():
    d = request.get_json(force=True)
    subj = q("SELECT * FROM subjects WHERE id=?", (d.get("subject_id"),), one=True)
    try:
        exam = date.fromisoformat(d.get("exam", ""))
        hours = float(d.get("hours", 2))
    except ValueError:
        return jsonify(error="Enter a valid exam date and hours"), 400
    if not subj or exam < date.today():
        return jsonify(error="Pick a subject and a future exam date"), 400
    items = claude_plan(subj["name"], exam.isoformat(), hours) or heuristic_plan(
        subj["name"], exam.isoformat(), hours)
    for it in items:
        it["subject_id"] = subj["id"]
        insert_task(it)
    return jsonify(created=len(items)), 201


if __name__ == "__main__":
    app.run(debug=True)