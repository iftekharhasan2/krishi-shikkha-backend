from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from config.db import get_db
from bson import ObjectId
from bson.errors import InvalidId
import cloudinary.uploader

users_bp = Blueprint("users", __name__)

MAX_AVATAR_SIZE = 2 * 1024 * 1024  # 2 MB

def _delete_cloudinary_image(url):
    """Delete a Cloudinary image by URL (best-effort)."""
    if not url or not url.startswith("https://res.cloudinary.com"):
        return
    try:
        parts = url.split("/upload/")
        if len(parts) == 2:
            public_id = parts[1].split("/", 1)[-1].rsplit(".", 1)[0]
            cloudinary.uploader.destroy(public_id, resource_type="image")
    except Exception:
        pass


@users_bp.route("/profile", methods=["PUT"])
@jwt_required()
def update_profile():
    db = get_db()
    user_id = get_jwt_identity()

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "JSON ডেটা প্রয়োজন"}), 400

    allowed = ["name", "bio", "phone", "website"]
    update = {}
    for k, v in data.items():
        if k in allowed and isinstance(v, str):
            update[k] = v.strip()

    if "name" in update and len(update["name"]) < 2:
        return jsonify({"error": "নাম কমপক্ষে ২ অক্ষরের হতে হবে"}), 400

    if not update:
        return jsonify({"error": "আপডেট করার কিছু নেই"}), 400

    try:
        db.users.update_one({"_id": ObjectId(user_id)}, {"$set": update})
    except (InvalidId, Exception):
        return jsonify({"error": "আপডেট করতে সমস্যা হয়েছে"}), 500

    return jsonify({"message": "প্রোফাইল আপডেট হয়েছে"})


@users_bp.route("/avatar", methods=["POST"])
@jwt_required()
def upload_avatar():
    db = get_db()
    user_id = get_jwt_identity()

    if "avatar" not in request.files:
        return jsonify({"error": "ফাইল নেই"}), 400

    file = request.files["avatar"]

    allowed_types = {"image/jpeg", "image/png", "image/gif", "image/webp"}
    if file.mimetype not in allowed_types:
        return jsonify({"error": "শুধুমাত্র JPG, PNG, GIF বা WebP ছবি আপলোড করুন"}), 400

    # Size check before uploading
    file_bytes = file.read()
    if len(file_bytes) > MAX_AVATAR_SIZE:
        return jsonify({"error": "ছবির আকার সর্বোচ্চ ২ মেগাবাইট হতে পারবে"}), 400
    file.seek(0)

    try:
        # Delete old avatar from Cloudinary
        user = db.users.find_one({"_id": ObjectId(user_id)}, {"avatar": 1})
        if user and user.get("avatar"):
            _delete_cloudinary_image(user["avatar"])

        result = cloudinary.uploader.upload(
            file,
            folder="krishi_lms/avatars",
            public_id=f"user_{user_id}",
            overwrite=True,
            resource_type="image",
            transformation=[
                {"width": 300, "height": 300, "crop": "fill", "gravity": "face", "quality": "auto"}
            ],
        )
        avatar_url = result["secure_url"]
    except Exception as e:
        return jsonify({"error": f"আপলোড করতে সমস্যা হয়েছে: {str(e)}"}), 500

    try:
        db.users.update_one({"_id": ObjectId(user_id)}, {"$set": {"avatar": avatar_url}})
    except (InvalidId, Exception):
        return jsonify({"error": "আপলোড করতে সমস্যা হয়েছে"}), 500

    return jsonify({"avatar": avatar_url})


@users_bp.route("/enrolled", methods=["GET"])
@jwt_required()
def enrolled_courses():
    db = get_db()
    user_id = get_jwt_identity()

    try:
        user = db.users.find_one({"_id": ObjectId(user_id)})
    except (InvalidId, Exception):
        return jsonify({"error": "ব্যবহারকারী পাওয়া যায়নি"}), 401

    if not user:
        return jsonify({"error": "ব্যবহারকারী পাওয়া যায়নি"}), 404

    enrolled_ids = user.get("enrolled_courses", [])
    courses = []
    for cid in enrolled_ids:
        try:
            course = db.courses.find_one({"_id": ObjectId(cid)})
            if course:
                instructor = db.users.find_one({"_id": ObjectId(course["instructor_id"])})
                lesson_count = db.lessons.count_documents({"course_id": str(course["_id"])})
                courses.append({
                    "id": str(course["_id"]),
                    "title": course["title"],
                    "description": course.get("description", ""),
                    "thumbnail": course.get("thumbnail"),
                    "instructor_name": instructor["name"] if instructor else "অজানা",
                    "lesson_count": lesson_count,
                    "price": course.get("price", 0),
                    "category": course.get("category", "")
                })
        except Exception:
            pass

    return jsonify(courses)
