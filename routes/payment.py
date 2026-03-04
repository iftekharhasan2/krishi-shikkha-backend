from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from config.db import get_db
from bson import ObjectId
from bson.errors import InvalidId
from datetime import datetime, timedelta
import uuid, os, requests, re

payment_bp = Blueprint("payment", __name__)

# ── bKash Merchant API credentials (set in .env) ──────────────────
BKASH_BASE_URL      = os.getenv("BKASH_BASE_URL", "https://tokenized.sandbox.bka.sh/v1.2.0-beta")
BKASH_APP_KEY       = os.getenv("BKASH_APP_KEY", "")
BKASH_APP_SECRET    = os.getenv("BKASH_APP_SECRET", "")
BKASH_USERNAME      = os.getenv("BKASH_USERNAME", "")
BKASH_PASSWORD      = os.getenv("BKASH_PASSWORD", "")

def get_user_safe(db, user_id):
    try:
        return db.users.find_one({"_id": ObjectId(user_id)})
    except Exception:
        return None

# ── Token cache (simple in-memory, refresh every 55 min in production) ──
_bkash_token_cache = {"token": None, "expires_at": None}

def get_bkash_token():
    """Get bKash grant token (cached)."""
    now = datetime.utcnow()
    if _bkash_token_cache["token"] and _bkash_token_cache["expires_at"] and now < _bkash_token_cache["expires_at"]:
        return _bkash_token_cache["token"]

    url = f"{BKASH_BASE_URL}/tokenized/checkout/token/grant"
    headers = {
        "username": BKASH_USERNAME,
        "password": BKASH_PASSWORD,
        "Content-Type": "application/json"
    }
    body = {"app_key": BKASH_APP_KEY, "app_secret": BKASH_APP_SECRET}

    try:
        res = requests.post(url, json=body, headers=headers, timeout=10)
        data = res.json()
        token = data.get("id_token")
        if not token:
            return None
        _bkash_token_cache["token"] = token
        _bkash_token_cache["expires_at"] = now + timedelta(minutes=55)
        return token
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/payment/create  — initiate bKash payment
# ─────────────────────────────────────────────────────────────────────────────
@payment_bp.route("/create", methods=["POST"])
@jwt_required()
def create_payment():
    db = get_db()
    user_id = get_jwt_identity()
    user = get_user_safe(db, user_id)
    if not user:
        return jsonify({"error": "ব্যবহারকারী পাওয়া যায়নি"}), 401

    data = request.get_json(silent=True) or {}
    course_id = data.get("course_id", "").strip()
    if not course_id:
        return jsonify({"error": "কোর্স আইডি প্রয়োজন"}), 400

    # Validate course
    try:
        course = db.courses.find_one({"_id": ObjectId(course_id)})
    except Exception:
        return jsonify({"error": "ভুল কোর্স আইডি"}), 400
    if not course:
        return jsonify({"error": "কোর্স পাওয়া যায়নি"}), 404

    if course.get("price", 0) <= 0:
        return jsonify({"error": "এই কোর্স বিনামূল্যে — সরাসরি ভর্তি হন"}), 400

    if course_id in user.get("enrolled_courses", []):
        return jsonify({"error": "ইতিমধ্যে ভর্তি হয়েছেন"}), 409

    # Create pending payment record in DB
    invoice_id = "KV-" + str(uuid.uuid4())[:8].upper()
    amount = str(int(course["price"]))  # bKash expects integer string

    payment_doc = {
        "invoice_id": invoice_id,
        "user_id": user_id,
        "course_id": course_id,
        "course_title": course["title"],
        "amount": course["price"],
        "status": "pending",
        "payment_id": None,
        "trx_id": None,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow()
    }
    db.payments.insert_one(payment_doc)

    # ── bKash API: Create Payment ──
    token = get_bkash_token()
    if not token:
        # Sandbox/credentials not configured → use demo mode
        return jsonify({
            "demo_mode": True,
            "invoice_id": invoice_id,
            "amount": course["price"],
            "course_title": course["title"],
            "message": "bKash credentials নেই — ডেমো মোডে চলছে"
        })

    bkash_url = f"{BKASH_BASE_URL}/tokenized/checkout/create"
    headers = {
        "Authorization": token,
        "X-APP-Key": BKASH_APP_KEY,
        "Content-Type": "application/json"
    }
    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:5173")
    body = {
        "mode": "0011",
        "payerReference": user["email"],
        "callbackURL": f"{frontend_url}/payment/callback",
        "amount": amount,
        "currency": "BDT",
        "intent": "sale",
        "merchantInvoiceNumber": invoice_id
    }

    try:
        res = requests.post(bkash_url, json=body, headers=headers, timeout=10)
        bkash_data = res.json()
    except Exception as e:
        return jsonify({"error": "bKash সার্ভারে সংযোগ ব্যর্থ হয়েছে"}), 502

    if bkash_data.get("statusCode") != "0000":
        msg = bkash_data.get("statusMessage", "bKash পেমেন্ট শুরু করতে ব্যর্থ হয়েছে")
        return jsonify({"error": msg}), 400

    # Store bKash paymentID
    db.payments.update_one(
        {"invoice_id": invoice_id},
        {"$set": {"payment_id": bkash_data["paymentID"], "updated_at": datetime.utcnow()}}
    )

    return jsonify({
        "bkash_url": bkash_data.get("bkashURL"),
        "payment_id": bkash_data["paymentID"],
        "invoice_id": invoice_id,
        "amount": course["price"],
        "course_title": course["title"]
    })


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/payment/execute  — execute after bKash redirect
# ─────────────────────────────────────────────────────────────────────────────
@payment_bp.route("/execute", methods=["POST"])
@jwt_required()
def execute_payment():
    db = get_db()
    user_id = get_jwt_identity()
    user = get_user_safe(db, user_id)
    if not user:
        return jsonify({"error": "ব্যবহারকারী পাওয়া যায়নি"}), 401

    data = request.get_json(silent=True) or {}
    payment_id = data.get("payment_id", "").strip()
    if not payment_id:
        return jsonify({"error": "payment_id প্রয়োজন"}), 400

    # Find payment record
    payment = db.payments.find_one({"payment_id": payment_id, "user_id": user_id})
    if not payment:
        return jsonify({"error": "পেমেন্ট তথ্য পাওয়া যায়নি"}), 404

    if payment["status"] == "completed":
        return jsonify({"message": "পেমেন্ট ইতিমধ্যে সম্পন্ন হয়েছে", "already_done": True})

    # Execute on bKash
    token = get_bkash_token()
    if not token:
        return jsonify({"error": "bKash সংযোগ ব্যর্থ হয়েছে"}), 502

    bkash_url = f"{BKASH_BASE_URL}/tokenized/checkout/execute"
    headers = {
        "Authorization": token,
        "X-APP-Key": BKASH_APP_KEY,
        "Content-Type": "application/json"
    }

    try:
        res = requests.post(bkash_url, json={"paymentID": payment_id}, headers=headers, timeout=10)
        bkash_data = res.json()
    except Exception:
        return jsonify({"error": "bKash সার্ভারে সংযোগ ব্যর্থ হয়েছে"}), 502

    if bkash_data.get("statusCode") != "0000":
        msg = bkash_data.get("statusMessage", "পেমেন্ট সম্পন্ন করতে ব্যর্থ হয়েছে")
        db.payments.update_one(
            {"payment_id": payment_id},
            {"$set": {"status": "failed", "error": msg, "updated_at": datetime.utcnow()}}
        )
        return jsonify({"error": msg}), 400

    trx_id = bkash_data.get("trxID")
    course_id = payment["course_id"]

    # Mark completed + enroll user
    db.payments.update_one(
        {"payment_id": payment_id},
        {"$set": {"status": "completed", "trx_id": trx_id, "updated_at": datetime.utcnow()}}
    )
    if course_id not in user.get("enrolled_courses", []):
        db.users.update_one(
            {"_id": ObjectId(user_id)},
            {"$push": {"enrolled_courses": course_id}}
        )

    return jsonify({
        "message": "পেমেন্ট সফল! কোর্সে ভর্তি হয়েছেন 🌾",
        "trx_id": trx_id,
        "invoice_id": payment["invoice_id"],
        "course_id": course_id,
        "amount": payment["amount"]
    })


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/payment/demo-complete  — for demo mode (no real bKash creds)
# ─────────────────────────────────────────────────────────────────────────────
@payment_bp.route("/demo-complete", methods=["POST"])
@jwt_required()
def demo_complete():
    db = get_db()
    user_id = get_jwt_identity()
    user = get_user_safe(db, user_id)
    if not user:
        return jsonify({"error": "ব্যবহারকারী পাওয়া যায়নি"}), 401

    data = request.get_json(silent=True) or {}
    invoice_id = data.get("invoice_id", "").strip()
    phone = data.get("phone", "").strip()

    if not invoice_id or not phone:
        return jsonify({"error": "invoice_id এবং phone নম্বর প্রয়োজন"}), 400

    # Validate phone (basic Bangladeshi number check)
    if not re.match(r'^01[3-9]\d{8}$', phone):
        return jsonify({"error": "সঠিক bKash নম্বর দিন (01XXXXXXXXX)"}), 400

    payment = db.payments.find_one({"invoice_id": invoice_id, "user_id": user_id})
    if not payment:
        return jsonify({"error": "পেমেন্ট তথ্য পাওয়া যায়নি"}), 404
    if payment["status"] == "completed":
        return jsonify({"message": "ইতিমধ্যে সম্পন্ন হয়েছে", "already_done": True})

    course_id = payment["course_id"]
    fake_trx = "TXN" + str(uuid.uuid4())[:10].upper().replace("-", "")

    db.payments.update_one(
        {"invoice_id": invoice_id},
        {"$set": {
            "status": "completed",
            "trx_id": fake_trx,
            "bkash_number": phone,
            "demo": True,
            "updated_at": datetime.utcnow()
        }}
    )
    if course_id not in user.get("enrolled_courses", []):
        db.users.update_one(
            {"_id": ObjectId(user_id)},
            {"$push": {"enrolled_courses": course_id}}
        )

    return jsonify({
        "message": "পেমেন্ট সফল! কোর্সে ভর্তি হয়েছেন 🌾",
        "trx_id": fake_trx,
        "invoice_id": invoice_id,
        "course_id": course_id,
        "amount": payment["amount"]
    })


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/payment/history  — user's payment history
# ─────────────────────────────────────────────────────────────────────────────
@payment_bp.route("/history", methods=["GET"])
@jwt_required()
def payment_history():
    db = get_db()
    user_id = get_jwt_identity()
    user = get_user_safe(db, user_id)
    if not user:
        return jsonify({"error": "ব্যবহারকারী পাওয়া যায়নি"}), 401

    payments = list(db.payments.find(
        {"user_id": user_id, "status": "completed"},
        sort=[("created_at", -1)]
    ))
    return jsonify([
        {
            "invoice_id": p["invoice_id"],
            "course_title": p["course_title"],
            "amount": p["amount"],
            "trx_id": p.get("trx_id"),
            "created_at": p["created_at"].isoformat()
        }
        for p in payments
    ])


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/payment/admin  — admin payment overview
# ─────────────────────────────────────────────────────────────────────────────
@payment_bp.route("/admin", methods=["GET"])
@jwt_required()
def admin_payments():
    db = get_db()
    user_id = get_jwt_identity()
    user = get_user_safe(db, user_id)
    if not user or user["role"] != "admin":
        return jsonify({"error": "অনুমতি নেই"}), 403

    payments = list(db.payments.find({}, sort=[("created_at", -1)]).limit(100))
    total_revenue = db.payments.aggregate([
        {"$match": {"status": "completed"}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}}
    ])
    total = next(total_revenue, {}).get("total", 0)

    return jsonify({
        "total_revenue": total,
        "payments": [
            {
                "invoice_id": p["invoice_id"],
                "course_title": p["course_title"],
                "amount": p["amount"],
                "status": p["status"],
                "trx_id": p.get("trx_id"),
                "demo": p.get("demo", False),
                "created_at": p["created_at"].isoformat()
            }
            for p in payments
        ]
    })
