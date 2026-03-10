from flask import Blueprint, request, jsonify
from flask_jwt_extended import create_access_token, jwt_required, get_jwt_identity
from config.db import get_db
import bcrypt, uuid, re
from datetime import datetime
from bson import ObjectId
from bson.errors import InvalidId

from flask import current_app

auth_bp = Blueprint("auth", __name__)

def _limiter():
    return current_app.extensions.get("limiter")

def limit(rule):
    """Apply rate limit if limiter is available."""
    def decorator(f):
        from functools import wraps
        @wraps(f)
        def wrapped(*args, **kwargs):
            lim = _limiter()
            if lim:
                return lim.limit(rule)(f)(*args, **kwargs)
            return f(*args, **kwargs)
        return wrapped
    return decorator

def is_valid_email(email):
    return re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email) is not None

def is_strong_password(pw):
    return len(pw) >= 6  # minimum; strengthen as needed

@auth_bp.route("/register", methods=["POST"])
@limit("5 per minute")
def register():
    db = get_db()
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "JSON ডেটা প্রয়োজন"}), 400

    for f in ["email", "password", "name", "role"]:
        if not data.get(f):
            return jsonify({"error": f"{f} প্রয়োজন"}), 400

    if data["role"] not in ["student", "instructor"]:
        return jsonify({"error": "ভূমিকা সঠিক নয়"}), 400

    if not is_valid_email(data["email"]):
        return jsonify({"error": "ইমেইল ঠিকানা সঠিক নয়"}), 400

    if not is_strong_password(data["password"]):
        return jsonify({"error": "পাসওয়ার্ড কমপক্ষে ৬ অক্ষরের হতে হবে"}), 400

    if len(data["name"].strip()) < 2:
        return jsonify({"error": "নাম কমপক্ষে ২ অক্ষরের হতে হবে"}), 400

    if db.users.find_one({"email": data["email"].lower().strip()}):
        return jsonify({"error": "এই ইমেইল ইতিমধ্যে নিবন্ধিত"}), 409

    hashed = bcrypt.hashpw(data["password"].encode(), bcrypt.gensalt())
    user_id = str(uuid.uuid4())[:8].upper()

    db.users.insert_one({
        "user_id": user_id,
        "email": data["email"].lower().strip(),
        "password": hashed,
        "name": data["name"].strip(),
        "role": data["role"],
        "bio": data.get("bio", ""),
        "avatar": None,
        "phone": data.get("phone", ""),
        "website": data.get("website", ""),
        "approved": data["role"] == "student",
        "enrolled_courses": [],
        "created_at": datetime.utcnow()
    })

    msg = "নিবন্ধন সফল"
    if data["role"] == "instructor":
        msg += " - অ্যাডমিনের অনুমোদনের জন্য অপেক্ষা করুন"

    return jsonify({"message": msg, "user_id": user_id}), 201


@auth_bp.route("/login", methods=["POST"])
@limit("10 per minute")
def login():
    db = get_db()
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "JSON ডেটা প্রয়োজন"}), 400

    email = data.get("email", "").lower().strip()
    password = data.get("password", "")

    if not email or not password:
        return jsonify({"error": "ইমেইল ও পাসওয়ার্ড দিন"}), 400

    user = db.users.find_one({"email": email})
    if not user:
        return jsonify({"error": "ইমেইল বা পাসওয়ার্ড ভুল"}), 401

    # Ensure stored hash is bytes (pymongo may return bson.Binary — cast explicitly)
    stored_pw = user["password"]
    if isinstance(stored_pw, str):
        stored_pw = stored_pw.encode("utf-8")
    else:
        stored_pw = bytes(stored_pw)

    try:
        pw_ok = bcrypt.checkpw(password.encode("utf-8"), stored_pw)
    except Exception:
        pw_ok = False

    if not pw_ok:
        return jsonify({"error": "ইমেইল বা পাসওয়ার্ড ভুল"}), 401

    if not user.get("approved"):
        return jsonify({"error": "অ্যাকাউন্ট এখনো অনুমোদিত হয়নি"}), 403

    token = create_access_token(identity=str(user["_id"]))

    return jsonify({
        "token": token,
        "user": {
            "id": str(user["_id"]),
            "user_id": user["user_id"],
            "email": user["email"],
            "name": user["name"],
            "role": user["role"],
            "avatar": user.get("avatar"),
            "bio": user.get("bio", ""),
            "phone": user.get("phone", ""),
            "website": user.get("website", ""),
        }
    })



@auth_bp.route("/me", methods=["GET"])
@jwt_required()
def me():
    db = get_db()
    try:
        user = db.users.find_one({"_id": ObjectId(get_jwt_identity())})
    except InvalidId:
        return jsonify({"error": "অবৈধ টোকেন"}), 401

    if not user:
        return jsonify({"error": "ব্যবহারকারী পাওয়া যায়নি"}), 404

    return jsonify({
        "id": str(user["_id"]),
        "user_id": user["user_id"],
        "email": user["email"],
        "name": user["name"],
        "role": user["role"],
        "avatar": user.get("avatar"),
        "bio": user.get("bio", ""),
        "phone": user.get("phone", ""),
        "website": user.get("website", ""),
        "approved": user.get("approved"),
        "enrolled_courses": user.get("enrolled_courses", []),
        "created_at": user["created_at"].isoformat() if user.get("created_at") else ""
    })
