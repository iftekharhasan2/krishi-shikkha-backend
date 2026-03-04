from flask import Blueprint, request, jsonify, Response, stream_with_context
from flask_jwt_extended import jwt_required, get_jwt_identity
from config.db import get_db, get_fs
from bson import ObjectId
from bson.errors import InvalidId
import struct
from datetime import datetime

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

def delete_gridfs_file(fs, file_id):
    """Safely delete a GridFS file by its ObjectId."""
    if not file_id:
        return
    try:
        fs.delete(ObjectId(file_id) if not isinstance(file_id, ObjectId) else file_id)
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
            "has_video":     bool(l.get("video_file_id")),
            "has_note":      bool(l.get("note_filename")),
            "note_filename": l.get("note_filename") if has_access else None,
        }
        if has_access and l.get("video_file_id"):
            lesson_data["video_url"] = f"/api/lessons/{str(l['_id'])}/video"
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

    return jsonify({
        "id":            str(lesson["_id"]),
        "title":         lesson["title"],
        "description":   lesson.get("description", ""),
        "order":         lesson.get("order", 0),
        "duration":      lesson.get("duration", 0),
        "is_free":       is_first,
        "has_video":     bool(lesson.get("video_file_id")),
        "video_url":     f"/api/lessons/{lesson_id}/video" if lesson.get("video_file_id") else None,
        "has_note":      bool(lesson.get("note_filename")),
        "note_filename": lesson.get("note_filename"),
        "course_id":     lesson["course_id"],
    })


# ── GET /api/lessons/<lesson_id>/video  (GridFS streaming) ────────────────

@lessons_bp.route("/<lesson_id>/video", methods=["GET"])
@jwt_required(optional=True)
def stream_video(lesson_id):
    from flask_jwt_extended import decode_token
    db  = get_db()
    fs  = get_fs()

    # Support token as query param for browser <video> tags (can't send headers)
    user_id = get_jwt_identity()
    if not user_id:
        token_param = request.args.get("token")
        if token_param:
            try:
                decoded = decode_token(token_param)
                user_id = decoded.get("sub")
            except Exception:
                return jsonify({"error": "অবৈধ টোকেন"}), 401
    if not user_id:
        return jsonify({"error": "প্রবেশাধিকার প্রয়োজন"}), 401

    user = get_user_safe(db, user_id)
    if not user:
        return jsonify({"error": "ব্যবহারকারী পাওয়া যায়নি"}), 401

    lesson = get_doc_safe(db, "lessons", lesson_id)
    if not lesson or not lesson.get("video_file_id"):
        return jsonify({"error": "ভিডিও পাওয়া যায়নি"}), 404

    has_access, _, err = check_access(db, lesson, user_id, user)
    if err:
        return jsonify({"error": err}), 404
    if not has_access:
        return jsonify({"error": "প্রবেশাধিকার নেই"}), 403

    # Retrieve GridFS file
    try:
        grid_out = fs.get(ObjectId(lesson["video_file_id"]))
    except Exception:
        return jsonify({"error": "ভিডিও ফাইল পাওয়া যায়নি"}), 404

    mime_type  = lesson.get("video_mime", "video/mp4")
    total_size = grid_out.length

    range_header = request.headers.get("Range")

    CHUNK = 65536  # 64 KB

    # pymongo 4.x GridFS gridout does NOT support .seek().
    # We read sequentially, skipping bytes until start, then yield until end.
    def _stream(start_byte, end_byte):
        """Generator: skip to start_byte, then yield through end_byte."""
        remaining_skip = start_byte
        remaining_read = end_byte - start_byte + 1
        # Re-open grid file each time (gridout cursor is forward-only)
        try:
            gf = fs.get(ObjectId(lesson["video_file_id"]))
        except Exception:
            return
        try:
            # Skip bytes before range start
            while remaining_skip > 0:
                to_skip = min(CHUNK, remaining_skip)
                chunk = gf.read(to_skip)
                if not chunk:
                    return
                remaining_skip -= len(chunk)
            # Yield bytes in range
            while remaining_read > 0:
                to_read = min(CHUNK, remaining_read)
                chunk = gf.read(to_read)
                if not chunk:
                    return
                remaining_read -= len(chunk)
                yield chunk
        finally:
            gf.close()

    def _stream_full():
        try:
            gf = fs.get(ObjectId(lesson["video_file_id"]))
        except Exception:
            return
        try:
            while True:
                chunk = gf.read(CHUNK)
                if not chunk:
                    break
                yield chunk
        finally:
            gf.close()

    if range_header:
        try:
            parts = range_header.replace("bytes=", "").split("-")
            start = int(parts[0])
            end   = int(parts[1]) if parts[1] else total_size - 1
            end   = min(end, total_size - 1)
            if start > end or start >= total_size:
                return Response(
                    status=416,
                    headers={"Content-Range": f"bytes */{total_size}"}
                )
            length = end - start + 1
            return Response(
                stream_with_context(_stream(start, end)),
                status=206,
                mimetype=mime_type,
                headers={
                    "Content-Range":  f"bytes {start}-{end}/{total_size}",
                    "Accept-Ranges":  "bytes",
                    "Content-Length": str(length),
                    "Content-Type":   mime_type,
                    "Cache-Control":  "no-store",
                }
            )
        except (ValueError, IndexError):
            pass  # fall through to full stream

    return Response(
        stream_with_context(_stream_full()),
        status=200,
        mimetype=mime_type,
        headers={
            "Accept-Ranges":  "bytes",
            "Content-Length": str(total_size),
            "Content-Type":   mime_type,
            "Cache-Control":  "no-store",
        }
    )


# ── GET /api/lessons/<lesson_id>/note ─────────────────────────────────────

@lessons_bp.route("/<lesson_id>/note", methods=["GET"])
@jwt_required(optional=True)
def download_note(lesson_id):
    from flask_jwt_extended import decode_token
    db  = get_db()
    fs  = get_fs()

    user_id = get_jwt_identity()
    if not user_id:
        token_param = request.args.get("token")
        if token_param:
            try:
                decoded = decode_token(token_param)
                user_id = decoded.get("sub")
            except Exception:
                return jsonify({"error": "অবৈধ টোকেন"}), 401
    if not user_id:
        return jsonify({"error": "প্রবেশাধিকার প্রয়োজন"}), 401

    user = get_user_safe(db, user_id)
    if not user:
        return jsonify({"error": "ব্যবহারকারী পাওয়া যায়নি"}), 401

    lesson = get_doc_safe(db, "lessons", lesson_id)
    if not lesson or not lesson.get("note_file_id"):
        return jsonify({"error": "নোট পাওয়া যায়নি"}), 404

    has_access, _, err = check_access(db, lesson, user_id, user)
    if err:
        return jsonify({"error": err}), 404
    if not has_access:
        return jsonify({"error": "প্রবেশাধিকার নেই"}), 403

    try:
        grid_out = fs.get(ObjectId(lesson["note_file_id"]))
    except Exception:
        return jsonify({"error": "নোট ফাইল পাওয়া যায়নি"}), 404

    filename = lesson.get("note_filename", "note")
    mime     = lesson.get("note_mime", "application/octet-stream")

    def generate():
        while True:
            data = grid_out.read(65536)
            if not data:
                break
            yield data

    return Response(
        stream_with_context(generate()),
        mimetype=mime,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


# ── POST /api/lessons/course/<course_id>  (create lesson) ─────────────────

@lessons_bp.route("/course/<course_id>", methods=["POST"])
@jwt_required()
def create_lesson(course_id):
    db  = get_db()
    fs  = get_fs()
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
        "course_id":    course_id,
        "title":        data["title"].strip(),
        "description":  data.get("description", "").strip(),
        "order":        order,
        "duration":     0,
        # GridFS file IDs (replaces raw binary blobs)
        "video_file_id": None,
        "video_mime":    None,
        "note_file_id":  None,
        "note_filename": None,
        "note_mime":     None,
        "created_at":   datetime.utcnow(),
    }

    if video_file and video_file.filename:
        # Read first 1MB to detect duration, then store full file in GridFS
        header_bytes = video_file.read(1024 * 1024)   # 1 MB for header scan
        lesson["duration"] = get_video_duration_from_bytes(header_bytes)
        # Reset and store the entire file in GridFS
        video_file.seek(0)
        file_id = fs.put(
            video_file,
            filename=video_file.filename,
            content_type=video_file.mimetype,
            course_id=course_id,
            lesson_title=data["title"].strip(),
        )
        lesson["video_file_id"] = file_id
        lesson["video_mime"]    = video_file.mimetype

    if note_file and note_file.filename:
        file_id = fs.put(
            note_file,
            filename=note_file.filename,
            content_type=note_file.mimetype,
        )
        lesson["note_file_id"]  = str(file_id)
        lesson["note_filename"] = note_file.filename
        lesson["note_mime"]     = note_file.mimetype

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
    fs  = get_fs()
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
        # Delete old GridFS video file
        delete_gridfs_file(fs, lesson.get("video_file_id"))
        # Detect duration from first 1MB
        header_bytes = video_file.read(1024 * 1024)
        update["duration"] = get_video_duration_from_bytes(header_bytes)
        video_file.seek(0)
        file_id = fs.put(
            video_file,
            filename=video_file.filename,
            content_type=video_file.mimetype,
        )
        update["video_file_id"] = file_id
        update["video_mime"]    = video_file.mimetype

    if note_file and note_file.filename:
        delete_gridfs_file(fs, lesson.get("note_file_id"))
        file_id = fs.put(
            note_file,
            filename=note_file.filename,
            content_type=note_file.mimetype,
        )
        update["note_file_id"]  = str(file_id)
        update["note_filename"] = note_file.filename
        update["note_mime"]     = note_file.mimetype

    db.lessons.update_one({"_id": ObjectId(lesson_id)}, {"$set": update})
    return jsonify({"message": "পাঠ আপডেট হয়েছে"})


# ── DELETE /api/lessons/<lesson_id> ───────────────────────────────────────

@lessons_bp.route("/<lesson_id>", methods=["DELETE"])
@jwt_required()
def delete_lesson(lesson_id):
    db  = get_db()
    fs  = get_fs()
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

    # Clean up GridFS files before deleting the lesson document
    delete_gridfs_file(fs, lesson.get("video_file_id"))
    delete_gridfs_file(fs, lesson.get("note_file_id"))

    db.lessons.delete_one({"_id": ObjectId(lesson_id)})
    return jsonify({"message": "পাঠ মুছে ফেলা হয়েছে"})