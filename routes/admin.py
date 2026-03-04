from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from config.db import get_db
from bson import ObjectId
from bson.errors import InvalidId
from functools import wraps

admin_bp = Blueprint("admin", __name__)


def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        db = get_db()
        try:
            user = db.users.find_one({"_id": ObjectId(get_jwt_identity())})
        except (InvalidId, Exception):
            return jsonify({"error": "অবৈধ টোকেন"}), 401
        if not user or user["role"] != "admin":
            return jsonify({"error": "শুধুমাত্র অ্যাডমিনের প্রবেশাধিকার"}), 403
        return f(*args, **kwargs)
    return decorated


@admin_bp.route("/users", methods=["GET"])
@jwt_required()
@require_admin
def get_all_users():
    db = get_db()
    users = list(db.users.find({}, {"password": 0}))
    return jsonify([{
        "id": str(u["_id"]),
        "user_id": u.get("user_id", ""),
        "name": u.get("name", ""),
        "email": u.get("email", ""),
        "role": u.get("role", "student"),
        "approved": u.get("approved", False),
        "created_at": u["created_at"].isoformat() if u.get("created_at") else ""
    } for u in users])


@admin_bp.route("/users/<user_id>/approve", methods=["POST"])
@jwt_required()
@require_admin
def approve_user(user_id):
    db = get_db()
    try:
        result = db.users.update_one({"_id": ObjectId(user_id)}, {"$set": {"approved": True}})
    except (InvalidId, Exception):
        return jsonify({"error": "ভুল ব্যবহারকারী আইডি"}), 400
    if result.matched_count == 0:
        return jsonify({"error": "ব্যবহারকারী পাওয়া যায়নি"}), 404
    return jsonify({"message": "অনুমোদন দেওয়া হয়েছে"})


@admin_bp.route("/users/<user_id>/revoke", methods=["POST"])
@jwt_required()
@require_admin
def revoke_user(user_id):
    db = get_db()
    try:
        user = db.users.find_one({"_id": ObjectId(user_id)})
    except (InvalidId, Exception):
        return jsonify({"error": "ভুল ব্যবহারকারী আইডি"}), 400
    if not user:
        return jsonify({"error": "ব্যবহারকারী পাওয়া যায়নি"}), 404
    if user["role"] == "admin":
        return jsonify({"error": "অ্যাডমিনের অ্যাক্সেস বাতিল করা যাবে না"}), 400
    db.users.update_one({"_id": ObjectId(user_id)}, {"$set": {"approved": False}})
    return jsonify({"message": "অ্যাক্সেস বাতিল করা হয়েছে"})


@admin_bp.route("/stats", methods=["GET"])
@jwt_required()
@require_admin
def stats():
    db = get_db()
    return jsonify({
        "total_users":           db.users.count_documents({}),
        "total_students":        db.users.count_documents({"role": "student"}),
        "total_instructors":     db.users.count_documents({"role": "instructor"}),
        "pending_instructors":   db.users.count_documents({"role": "instructor", "approved": False}),
        "total_courses":         db.courses.count_documents({}),
        "total_lessons":         db.lessons.count_documents({}),
    })
