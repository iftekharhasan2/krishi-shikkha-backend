from flask import Blueprint, request, jsonify, redirect
from flask_jwt_extended import jwt_required, get_jwt_identity
from config.db import get_db
from bson import ObjectId
from bson.errors import InvalidId
import struct
from datetime import datetime
import cloudinary.uploader
import cloudinary.utils

lessons_bp = Blueprint("lessons", __name__)


# ── Helpers ────────────────────────────────────────────────────────────────

def get_user_safe(db, user_id):
    try:
        return db.users.find_one({"_id": ObjectId(user_id)})
    except Exception:
        return None

def get_doc_safe(db, collection, doc_id):
    try:
        return db[collection].find_one({"_id": ObjectId(doc_id)})
    except Exception:
        return None

def get_video_duration_from_bytes(video_bytes):
    """Parse MP4 mvhd box to get duration in seconds."""
    try:
        idx = video_bytes.find(b'mvhd')
        if idx != -1:
            offset = idx + 4
            version = video_bytes[offset]
            if version == 0:
                timescale = struct.unpack('>I', video_bytes[offset+12:offset+16])[0]
                duration  = struct.unpack('>I', video_bytes[offset+16:offset+20])[0]
            else:
                timescale = struct.unpack('>I', video_bytes[offset+20:offset+24])[0]
                duration  = struct.unpack('>Q', video_bytes[offset+24:offset+32])[0]
            if timescale > 0:
                return round(duration / timescale)
    except Exception:
        pass
    return 0

def delete_cloudinary_asset(public_id, resource_type="video"):
    """Safely delete a Cloudinary asset by public_id."""
    if not public_id:
        return
    try:
        cloudinary.uploader.destroy(public_id, resource_type=resource_type)
    except Exception:
        pass

def check_access(db, lesson, user_id, user):
    """Returns (has_access, is_first_lesson, error_msg)."""
    try:
        course = db.courses.find_one({"_id": ObjectId(lesson["course_id"])})
    except Exception:
        return False, False, "কোর্স পাওয়া যায়নি"
    if not course:
        return False, False, "কোর্স পাওয়া যায়নি"

    is_instructor = str(course["instructor_id"]) == user_id or user["role"] == "admin"
    lessons_ordered = list(db.lessons.find({"course_id": lesson["course_id"]}).sort("order", 1))
    lesson_index = next(
        (i for i, l in enumerate(lessons_ordered) if str(l["_id"]) == str(lesson["_id"])), 0
    )
    is_first    = lesson_index == 0
    is_enrolled = lesson["course_id"] in user.get("enrolled_courses", [])

    return is_instructor or is_enrolled or is_first, is_first, None

def make_signed_url(public_id, resource_type="video", expires_in=3600):
    """
    Generate a short-lived signed Cloudinary URL for private assets.
    Falls back to the stored URL if signing fails.
    """
    try:
        url, _ = cloudinary.utils.cloudinary_url(
            public_id,
            resource_type=resource_type,
            type="upload",
            sign_url=True,
            expires_at=int(datetime.utcnow().timestamp()) + expires_in,
        )
        return url
    except Exception:
        return None


# ── GET /api/lessons/course/<course_id> ────────────────────────────────────

@lessons_bp.route("/course/<course_id>", methods=["GET"])
@jwt_required()
def get_lessons(course_id):
    db = get_db()
    user_id = get_jwt_identity()
    user = get_user_safe(db, user_id)
    if not user:
        return jsonify({"error": "ব্যবহারকারী পাওয়া যায়নি"}), 401

    try:
        course = db.courses.find_one({"_id": ObjectId(course_id)})
    except Exception:
        return jsonify({"error": "ভুল কোর্স আইডি"}), 400
    if not course:
        return jsonify({"error": "কোর্স পাওয়া যায়নি"}), 404

    is_instructor = str(course["instructor_id"]) == user_id or user["role"] == "admin"
    is_enrolled   = course_id in user.get("enrolled_courses", [])

    lessons_raw = list(db.lessons.find({"course_id": course_id}).sort("order", 1))
    lessons = []
    for i, l in enumerate(lessons_raw):
        is_free    = i == 0
        has_access = is_instructor or is_enrolled or is_free

        lesson_data = {
            "id":            str(l["_id"]),
            "title":         l["title"],
            "description":   l.get("description", ""),
            "order":         l.get("order", i),
            "duration":      l.get("duration", 0),
            "is_free":       is_free,
            "has_access":    has_access,
            "has_video":     bool(l.get("video_url")),
            "has_note":      bool(l.get("note_url")),
            "note_filename": l.get("note_filename") if has_access else None,
        }
        if has_access and l.get("video_public_id"):
            lesson_data["video_url"] = make_signed_url(l["video_public_id"], "video")
        if has_access and l.get("note_public_id"):
            lesson_data["note_url"] = make_signed_url(l["note_public_id"], "raw")

        lessons.append(lesson_data)
    return jsonify(lessons)


# ── GET /api/lessons/<lesson_id> ───────────────────────────────────────────

@lessons_bp.route("/<lesson_id>", methods=["GET"])
@jwt_required()
def get_lesson(lesson_id):
    db = get_db()
    user_id = get_jwt_identity()
    user = get_user_safe(db, user_id)
    if not user:
        return jsonify({"error": "ব্যবহারকারী পাওয়া যায়নি"}), 401

    lesson = get_doc_safe(db, "lessons", lesson_id)
    if not lesson:
        return jsonify({"error": "পাঠ পাওয়া যায়নি"}), 404

    has_access, is_first, err = check_access(db, lesson, user_id, user)
    if err:
        return jsonify({"error": err}), 404
    if not has_access:
        return jsonify({"error": "প্রবেশাধিকার নেই"}), 403

    video_url = None
    if has_access and lesson.get("video_public_id"):
        video_url = make_signed_url(lesson["video_public_id"], "video")

    note_url = None
    if has_access and lesson.get("note_public_id"):
        note_url = make_signed_url(lesson["note_public_id"], "raw")

    return jsonify({
        "id":            str(lesson["_id"]),
        "title":         lesson["title"],
        "description":   lesson.get("description", ""),
        "order":         lesson.get("order", 0),
        "duration":      lesson.get("duration", 0),
        "is_free":       is_first,
        "has_video":     bool(lesson.get("video_url")),
        "video_url":     video_url,
        "has_note":      bool(lesson.get("note_url")),
        "note_url":      note_url,
        "note_filename": lesson.get("note_filename"),
        "course_id":     lesson["course_id"],
    })


# ── POST /api/lessons/course/<course_id>  (create lesson) ─────────────────

@lessons_bp.route("/course/<course_id>", methods=["POST"])
@jwt_required()
def create_lesson(course_id):
    db  = get_db()
    user_id = get_jwt_identity()
    user = get_user_safe(db, user_id)
    if not user:
        return jsonify({"error": "ব্যবহারকারী পাওয়া যায়নি"}), 401

    try:
        course = db.courses.find_one({"_id": ObjectId(course_id)})
    except Exception:
        return jsonify({"error": "ভুল কোর্স আইডি"}), 400
    if not course:
        return jsonify({"error": "কোর্স পাওয়া যায়নি"}), 404

    if str(course["instructor_id"]) != user_id and user["role"] != "admin":
        return jsonify({"error": "অনুমতি নেই"}), 403

    data = request.form
    if not data.get("title"):
        return jsonify({"error": "পাঠের শিরোনাম প্রয়োজন"}), 400

    video_file = request.files.get("video")
    note_file  = request.files.get("note")

    existing_count = db.lessons.count_documents({"course_id": course_id})
    try:
        order = int(data.get("order", existing_count))
    except ValueError:
        order = existing_count

    lesson = {
        "course_id":      course_id,
        "title":          data["title"].strip(),
        "description":    data.get("description", "").strip(),
        "order":          order,
        "duration":       0,
        "video_url":      None,
        "video_public_id": None,
        "note_url":       None,
        "note_public_id": None,
        "note_filename":  None,
        "created_at":     datetime.utcnow(),
    }

    if video_file and video_file.filename:
        # Read first 1MB to detect MP4 duration
        header_bytes = video_file.read(1024 * 1024)
        lesson["duration"] = get_video_duration_from_bytes(header_bytes)
        video_file.seek(0)
        try:
            result = cloudinary.uploader.upload(
                video_file,
                folder=f"krishi_lms/videos/{course_id}",
                resource_type="video",
                use_filename=True,
                unique_filename=True,
            )
            lesson["video_url"]       = result["secure_url"]
            lesson["video_public_id"] = result["public_id"]
            # Use Cloudinary duration if available and more accurate
            if result.get("duration"):
                lesson["duration"] = round(result["duration"])
        except Exception as e:
            return jsonify({"error": f"ভিডিও আপলোড করতে সমস্যা হয়েছে: {str(e)}"}), 500

    if note_file and note_file.filename:
        try:
            result = cloudinary.uploader.upload(
                note_file,
                folder=f"krishi_lms/notes/{course_id}",
                resource_type="raw",
                use_filename=True,
                unique_filename=True,
            )
            lesson["note_url"]       = result["secure_url"]
            lesson["note_public_id"] = result["public_id"]
            lesson["note_filename"]  = note_file.filename
        except Exception as e:
            return jsonify({"error": f"নোট আপলোড করতে সমস্যা হয়েছে: {str(e)}"}), 500

    result = db.lessons.insert_one(lesson)
    return jsonify({
        "id":       str(result.inserted_id),
        "message":  "পাঠ তৈরি হয়েছে",
        "duration": lesson["duration"],
    }), 201


# ── PUT /api/lessons/<lesson_id>  (update lesson) ─────────────────────────

@lessons_bp.route("/<lesson_id>", methods=["PUT"])
@jwt_required()
def update_lesson(lesson_id):
    db  = get_db()
    user_id = get_jwt_identity()
    user = get_user_safe(db, user_id)
    if not user:
        return jsonify({"error": "ব্যবহারকারী পাওয়া যায়নি"}), 401

    lesson = get_doc_safe(db, "lessons", lesson_id)
    if not lesson:
        return jsonify({"error": "পাঠ পাওয়া যায়নি"}), 404

    try:
        course = db.courses.find_one({"_id": ObjectId(lesson["course_id"])})
    except Exception:
        return jsonify({"error": "কোর্স পাওয়া যায়নি"}), 404

    if str(course["instructor_id"]) != user_id and user["role"] != "admin":
        return jsonify({"error": "অনুমতি নেই"}), 403

    data   = request.form
    update = {"updated_at": datetime.utcnow()}

    if data.get("title"):
        update["title"] = data["title"].strip()
    if data.get("description") is not None:
        update["description"] = data["description"].strip()
    if data.get("order") is not None:
        try:
            update["order"] = int(data["order"])
        except ValueError:
            pass

    video_file = request.files.get("video")
    note_file  = request.files.get("note")

    if video_file and video_file.filename:
        # Delete old Cloudinary video
        delete_cloudinary_asset(lesson.get("video_public_id"), "video")
        header_bytes = video_file.read(1024 * 1024)
        update["duration"] = get_video_duration_from_bytes(header_bytes)
        video_file.seek(0)
        try:
            result = cloudinary.uploader.upload(
                video_file,
                folder=f"krishi_lms/videos/{lesson['course_id']}",
                resource_type="video",
                use_filename=True,
                unique_filename=True,
            )
            update["video_url"]       = result["secure_url"]
            update["video_public_id"] = result["public_id"]
            if result.get("duration"):
                update["duration"] = round(result["duration"])
        except Exception as e:
            return jsonify({"error": f"ভিডিও আপলোড করতে সমস্যা হয়েছে: {str(e)}"}), 500

    if note_file and note_file.filename:
        delete_cloudinary_asset(lesson.get("note_public_id"), "raw")
        try:
            result = cloudinary.uploader.upload(
                note_file,
                folder=f"krishi_lms/notes/{lesson['course_id']}",
                resource_type="raw",
                use_filename=True,
                unique_filename=True,
            )
            update["note_url"]       = result["secure_url"]
            update["note_public_id"] = result["public_id"]
            update["note_filename"]  = note_file.filename
        except Exception as e:
            return jsonify({"error": f"নোট আপলোড করতে সমস্যা হয়েছে: {str(e)}"}), 500

    db.lessons.update_one({"_id": ObjectId(lesson_id)}, {"$set": update})
    return jsonify({"message": "পাঠ আপডেট হয়েছে"})


# ── DELETE /api/lessons/<lesson_id> ───────────────────────────────────────

@lessons_bp.route("/<lesson_id>", methods=["DELETE"])
@jwt_required()
def delete_lesson(lesson_id):
    db  = get_db()
    user_id = get_jwt_identity()
    user = get_user_safe(db, user_id)
    if not user:
        return jsonify({"error": "ব্যবহারকারী পাওয়া যায়নি"}), 401

    lesson = get_doc_safe(db, "lessons", lesson_id)
    if not lesson:
        return jsonify({"error": "পাঠ পাওয়া যায়নি"}), 404

    try:
        course = db.courses.find_one({"_id": ObjectId(lesson["course_id"])})
    except Exception:
        return jsonify({"error": "কোর্স পাওয়া যায়নি"}), 404

    if str(course["instructor_id"]) != user_id and user["role"] != "admin":
        return jsonify({"error": "অনুমতি নেই"}), 403

    # Delete Cloudinary assets
    delete_cloudinary_asset(lesson.get("video_public_id"), "video")
    delete_cloudinary_asset(lesson.get("note_public_id"), "raw")

    db.lessons.delete_one({"_id": ObjectId(lesson_id)})
    return jsonify({"message": "পাঠ মুছে ফেলা হয়েছে"})
