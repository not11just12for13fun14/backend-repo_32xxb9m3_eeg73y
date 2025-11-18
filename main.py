import os
import random
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from database import db, create_document, get_documents

app = FastAPI(title="Smart Routine Tracker API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -----------------------------
# Models
# -----------------------------
class RoutineCreate(BaseModel):
    title: str
    note: Optional[str] = None
    time: str = Field(..., description="HH:mm")
    color: str = Field("teal")
    icon: str = Field("AlarmClock")


class Routine(BaseModel):
    id: str
    title: str
    note: Optional[str] = None
    time: str
    status: str = "Pending"  # Pending | Completed | On-Time
    color: str = "teal"
    icon: str = "AlarmClock"
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class VerifyRequest(BaseModel):
    image_data: str  # data URL (base64)
    routine_id: Optional[str] = None


class VerifyResponse(BaseModel):
    verdict: str
    confidence: float
    created_at: datetime


# -----------------------------
# Utilities
# -----------------------------

def to_public_id(doc) -> str:
    return str(doc.get("_id"))


def normalize(doc) -> dict:
    if not doc:
        return {}
    d = {k: v for k, v in doc.items() if k != "_id"}
    d["id"] = to_public_id(doc)
    return d


# -----------------------------
# Root + Health
# -----------------------------
@app.get("/")
def read_root():
    return {"message": "Smart Routine Tracker API running"}


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": "❌ Not Set",
        "database_name": "❌ Not Set",
        "connection_status": "Not Connected",
        "collections": [],
    }

    try:
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
            response["database_name"] = db.name
            response["connection_status"] = "Connected"
            try:
                response["collections"] = db.list_collection_names()
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️ Connected but Error: {str(e)[:80]}"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:80]}"

    return response


# -----------------------------
# Routines
# -----------------------------
@app.get("/api/routines")
def list_routines() -> List[Routine]:
    items = get_documents("routine")
    # seed with example if empty
    if not items:
        seed = [
            {"title": "Wake Up", "note": "Drink water", "time": "06:30", "status": "On-Time", "color": "teal", "icon": "AlarmClock"},
            {"title": "Gym", "note": "Leg day", "time": "07:15", "status": "Pending", "color": "amber", "icon": "BellRing"},
            {"title": "Work", "note": "Deep focus", "time": "09:00", "status": "Completed", "color": "lime", "icon": "Clock"},
        ]
        for r in seed:
            create_document("routine", r)
        items = get_documents("routine")
    return [normalize(doc) for doc in items]


@app.post("/api/routines", status_code=201)
def create_routine(payload: RoutineCreate) -> Routine:
    rid = create_document("routine", {**payload.model_dump(), "status": "Pending"})
    doc = db["routine"].find_one({"_id": db["routine"].find_one({"_id": db["routine"].find_one({"_id": None})})})
    # simpler: fetch by id we just inserted
    from bson import ObjectId

    doc = db["routine"].find_one({"_id": ObjectId(rid)})
    return normalize(doc)


@app.post("/api/routines/{routine_id}/complete")
def complete_routine(routine_id: str):
    from bson import ObjectId

    result = db["routine"].update_one({"_id": ObjectId(routine_id)}, {"$set": {"status": "Completed", "updated_at": datetime.now(timezone.utc)}})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Routine not found")
    doc = db["routine"].find_one({"_id": ObjectId(routine_id)})
    return normalize(doc)


# -----------------------------
# Verification flow (mock AI)
# -----------------------------
@app.post("/api/verify", response_model=VerifyResponse)
def verify_capture(req: VerifyRequest):
    # Very light heuristic: bigger data URL => higher confidence
    size_hint = len(req.image_data or "")
    conf = max(0.45, min(0.98, 0.45 + (size_hint / 500000)))  # clamp
    # small randomness
    conf = float(round(conf + random.uniform(-0.07, 0.07), 2))
    conf = max(0.3, min(0.99, conf))

    if conf > 0.75:
        verdict = "Verified"
    elif conf > 0.55:
        verdict = "Unclear"
    else:
        verdict = "Not Verified"

    created = datetime.now(timezone.utc)

    # store capture + verification
    capture_id = create_document(
        "capture",
        {
            "routine_id": req.routine_id,
            "image_data": req.image_data[:1000],  # store a truncated preview to keep it light
        },
    )
    create_document(
        "verification",
        {
            "routine_id": req.routine_id,
            "capture_id": capture_id,
            "verdict": verdict,
            "confidence": conf,
            "created_at": created,
        },
    )

    return VerifyResponse(verdict=verdict, confidence=conf, created_at=created)


# -----------------------------
# History & Insights
# -----------------------------
@app.get("/api/history")
def history(limit: int = 20):
    items = db["verification"].find({}).sort("created_at", -1).limit(limit)
    return [normalize(doc) for doc in items]


@app.get("/api/insights")
def insights():
    # simple aggregates over last 7 days
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=6)
    cursor = db["verification"].find({"created_at": {"$gte": week_ago}})
    data = list(cursor)
    total = len(data)
    verified = sum(1 for d in data if d.get("verdict") == "Verified")
    completion_rate = round((verified / total) * 100, 1) if total else 0.0

    # build simple daily bars
    bars = []
    for i in range(7):
        day = (now - timedelta(days=6 - i)).date().isoformat()
        day_docs = [d for d in data if d.get("created_at", now).date().isoformat() == day]
        bars.append({"day": day[-2:], "count": len(day_docs)})

    streak = 0
    # naive streak: consecutive days with at least one Verified starting today backwards
    for i in range(7):
        day = (now - timedelta(days=i)).date().isoformat()
        day_verified = any((d.get("verdict") == "Verified") and d.get("created_at").date().isoformat() == day for d in data)
        if day_verified:
            streak += 1
        else:
            break

    return {
        "summary": {
            "completionRate": completion_rate,
            "streak": streak,
            "totalChecks": total,
        },
        "weekly": bars,
    }


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
