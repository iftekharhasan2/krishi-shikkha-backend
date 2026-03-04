from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from config.db import get_db, get_fs
from bson import ObjectId
from bson.errors import InvalidId
import base64
from datetime import datetime

courses_bp = Blueprint("courses", __name__)

def get_user_safe(db, user_id):
    """Get user by JWT identity string, safe ObjectId conversion."""
    try:
        return db.users.find_one({"_id": ObjectId(user_id)})
    except (InvalidId, Exception):
        return None

def serialize_course(course, instructor=None, enrolled_count=0, lessons_count=0):
    return {
        "id": str(course["_id"]),
        "title": course["title"],
        "description": course["description"],
        "long_description": course.get("long_description", ""),
        "thumbnail": course.get("thumbnail"),
        "price": course.get("price", 0),
        "category": course.get("category", ""),
        "level": course.get("level", "প্রাথমিক"),
        "tags": course.get("tags", []),
        "instructor_id": course["instructor_id"],
        "instructor_name": instructor["name"] if instructor else "অজানা",
        "instructor_avatar": instructor.get("avatar") if instructor else None,
        "enrolled_count": enrolled_count,
        "lessons_count": lessons_count,
        "created_at": course["created_at"].isoformat() if course.get("created_at") else "",
        "updated_at": (course.get("updated_at") or course.get("created_at", "")).isoformat() if course.get("updated_at") or course.get("created_at") else ""
    }


@courses_bp.route("/", methods=["GET"])
def get_all_courses():
    db = get_db()
    search = request.args.get("search", "").strip()
    category = request.args.get("category", "").strip()

    query = {}
    if search:
        query["$or"] = [
            {"title": {"$regex": search, "$options": "i"}},
            {"description": {"$regex": search, "$options": "i"}}
        ]
    if category:
        query["category"] = category

    courses = list(db.courses.find(query))
    result = []
    for c in courses:
        try:
            instructor = db.users.find_one({"_id": ObjectId(c["instructor_id"])})
        except Exception:
            instructor = None
        enrolled = db.users.count_documents({"enrolled_courses": str(c["_id"])})
        lessons = db.lessons.count_documents({"course_id": str(c["_id"])})
        result.append(serialize_course(c, instructor, enrolled, lessons))
    return jsonify(result)


@courses_bp.route("/<course_id>", methods=["GET"])
def get_course(course_id):
    db = get_db()
    try:
        course = db.courses.find_one({"_id": ObjectId(course_id)})
    except (InvalidId, Exception):
        return jsonify({"error": "ভুল কোর্স আইডি"}), 400

    if not course:
        return jsonify({"error": "কোর্স পাওয়া যায়নি"}), 404

    try:
        instructor = db.users.find_one({"_id": ObjectId(course["instructor_id"])})
    except Exception:
        instructor = None

    enrolled = db.users.count_documents({"enrolled_courses": str(course["_id"])})
    lessons_raw = list(db.lessons.find({"course_id": str(course["_id"])}).sort("order", 1))

    lessons = []
    for i, l in enumerate(lessons_raw):
        lessons.append({
            "id": str(l["_id"]),
            "title": l["title"],
            "description": l.get("description", ""),
            "order": l.get("order", i),
            "duration": l.get("duration", 0),
            "is_free": i == 0,
            "has_video": bool(l.get("video_file_id")),
            "has_note": bool(l.get("note_filename")),
            "note_filename": l.get("note_filename")
        })

    result = serialize_course(course, instructor, enrolled, len(lessons))
    result["lessons"] = lessons
    return jsonify(result)


@courses_bp.route("/", methods=["POST"])
@jwt_required()
def create_course():
    db = get_db()
    user_id = get_jwt_identity()
    user = get_user_safe(db, user_id)
    if not user:
        return jsonify({"error": "ব্যবহারকারী পাওয়া যায়নি"}), 401

    if user["role"] not in ["instructor", "admin"]:
        return jsonify({"error": "শুধুমাত্র শিক্ষকরা কোর্স তৈরি করতে পারবেন"}), 403

    data = request.form
    if not data.get("title") or not data.get("description"):
        return jsonify({"error": "শিরোনাম ও বিবরণ প্রয়োজন"}), 400

    thumbnail = request.files.get("thumbnail")
    thumbnail_data = None
    if thumbnail:
        raw = thumbnail.read()
        if len(raw) > 5 * 1024 * 1024:
            return jsonify({"error": "থাম্বনেইল ৫ মেগাবাইটের বেশি হতে পারবে না"}), 400
        thumbnail_data = f"data:{thumbnail.mimetype};base64,{base64.b64encode(raw).decode()}"

    tags = [t.strip() for t in data.get("tags", "").split(",") if t.strip()]

    try:
        price = float(data.get("price", 0))
        if price < 0:
            price = 0
    except ValueError:
        price = 0

    course = {
        "title": data["title"].strip(),
        "description": data["description"].strip(),
        "long_description": data.get("long_description", "").strip(),
        "thumbnail": thumbnail_data,
        "price": price,
        "category": data.get("category", "").strip(),
        "level": data.get("level", "প্রাথমিক"),
        "tags": tags,
        "instructor_id": user_id,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow()
    }

    result = db.courses.insert_one(course)
    return jsonify({"id": str(result.inserted_id), "message": "কোর্স তৈরি হয়েছে"}), 201


@courses_bp.route("/<course_id>", methods=["PUT"])
@jwt_required()
def update_course(course_id):
    db = get_db()
    user_id = get_jwt_identity()
    user = get_user_safe(db, user_id)
    if not user:
        return jsonify({"error": "ব্যবহারকারী পাওয়া যায়নি"}), 401

    try:
        course = db.courses.find_one({"_id": ObjectId(course_id)})
    except (InvalidId, Exception):
        return jsonify({"error": "ভুল কোর্স আইডি"}), 400

    if not course:
        return jsonify({"error": "কোর্স পাওয়া যায়নি"}), 404

    if str(course["instructor_id"]) != user_id and user["role"] != "admin":
        return jsonify({"error": "অনুমতি নেই"}), 403

    data = request.form
    thumbnail = request.files.get("thumbnail")
    update = {"updated_at": datetime.utcnow()}

    for field in ["title", "description", "long_description", "category", "level"]:
        if data.get(field):
            update[field] = data[field].strip()

    if data.get("price") is not None:
        try:
            update["price"] = max(0, float(data["price"]))
        except ValueError:
            pass

    if data.get("tags") is not None:
        update["tags"] = [t.strip() for t in data["tags"].split(",") if t.strip()]

    if thumbnail:
        raw = thumbnail.read()
        if len(raw) <= 5 * 1024 * 1024:
            update["thumbnail"] = f"data:{thumbnail.mimetype};base64,{base64.b64encode(raw).decode()}"

    db.courses.update_one({"_id": ObjectId(course_id)}, {"$set": update})
    return jsonify({"message": "কোর্স আপডেট হয়েছে"})


@courses_bp.route("/<course_id>/enroll", methods=["POST"])
@jwt_required()
def enroll(course_id):
    db = get_db()
    user_id = get_jwt_identity()
    user = get_user_safe(db, user_id)
    if not user:
        return jsonify({"error": "ব্যবহারকারী পাওয়া যায়নি"}), 401

    if user["role"] != "student":
        return jsonify({"error": "শুধুমাত্র শিক্ষার্থীরা ভর্তি হতে পারবেন"}), 403

    try:
        course = db.courses.find_one({"_id": ObjectId(course_id)})
    except (InvalidId, Exception):
        return jsonify({"error": "ভুল কোর্স আইডি"}), 400

    if not course:
        return jsonify({"error": "কোর্স পাওয়া যায়নি"}), 404

    if course_id in user.get("enrolled_courses", []):
        return jsonify({"error": "ইতিমধ্যে ভর্তি হয়েছেন"}), 409

    db.users.update_one({"_id": ObjectId(user_id)}, {"$push": {"enrolled_courses": course_id}})
    return jsonify({"message": "সফলভাবে ভর্তি হয়েছেন"})


@courses_bp.route("/instructor/my-courses", methods=["GET"])
@jwt_required()
def my_courses():
    db = get_db()
    user_id = get_jwt_identity()
    user = get_user_safe(db, user_id)
    if not user:
        return jsonify({"error": "ব্যবহারকারী পাওয়া যায়নি"}), 401

    if user["role"] not in ["instructor", "admin"]:
        return jsonify({"error": "অনুমতি নেই"}), 403

    courses = list(db.courses.find({"instructor_id": user_id}))
    result = []
    for c in courses:
        enrolled = db.users.count_documents({"enrolled_courses": str(c["_id"])})
        lessons = db.lessons.count_documents({"course_id": str(c["_id"])})
        enrolled_users = list(db.users.find(
            {"enrolled_courses": str(c["_id"])},
            {"name": 1, "email": 1, "user_id": 1}
        ))
        data = serialize_course(c, user, enrolled, lessons)
        data["enrolled_students"] = [
            {"name": u.get("name", ""), "email": u.get("email", ""), "user_id": u.get("user_id", "")}
            for u in enrolled_users
        ]
        result.append(data)
    return jsonify(result)


@courses_bp.route("/<course_id>", methods=["DELETE"])
@jwt_required()
def delete_course(course_id):
    db = get_db()
    user_id = get_jwt_identity()
    user = get_user_safe(db, user_id)
    if not user:
        return jsonify({"error": "ব্যবহারকারী পাওয়া যায়নি"}), 401

    try:
        course = db.courses.find_one({"_id": ObjectId(course_id)})
    except (InvalidId, Exception):
        return jsonify({"error": "ভুল কোর্স আইডি"}), 400

    if not course:
        return jsonify({"error": "কোর্স পাওয়া যায়নি"}), 404

    if str(course["instructor_id"]) != user_id and user["role"] != "admin":
        return jsonify({"error": "অনুমতি নেই"}), 403

    # Delete all GridFS files for this course's lessons before removing them
    fs = get_fs()
    lessons_to_delete = list(db.lessons.find({"course_id": course_id}))
    for lesson in lessons_to_delete:
        for field in ["video_file_id", "note_file_id"]:
            fid = lesson.get(field)
            if fid:
                try:
                    fs.delete(ObjectId(fid))
                except Exception:
                    pass
    db.courses.delete_one({"_id": ObjectId(course_id)})
    db.lessons.delete_many({"course_id": course_id})
    return jsonify({"message": "কোর্স মুছে ফেলা হয়েছে"})
