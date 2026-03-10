from pymongo import MongoClient
import cloudinary
import os, bcrypt, uuid
from datetime import datetime

client = None
db = None

def init_db():
    global client, db
    client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017/krishi_lms"))
    db = client.get_default_database()

    # Indexes
    db.users.create_index("email", unique=True)
    db.users.create_index("user_id", unique=True)
    db.courses.create_index("instructor_id")
    db.lessons.create_index("course_id")
    db.payments.create_index([("user_id", 1), ("status", 1)])

    _init_cloudinary()
    _seed_admin()
    print("ডেটাবেস প্রস্তুত (Cloudinary সক্রিয়)")

def _init_cloudinary():
    cloudinary.config(
        cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME", ""),
        api_key=os.getenv("CLOUDINARY_API_KEY", ""),
        api_secret=os.getenv("CLOUDINARY_API_SECRET", ""),
        secure=True,
    )
    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME", "")
    if cloud_name:
        print(f"✓ Cloudinary সংযুক্ত: {cloud_name}")
    else:
        print("⚠ CLOUDINARY credentials সেট নেই — ফাইল আপলোড কাজ করবে না")

def _seed_admin():
    admin_email    = os.getenv("ADMIN_EMAIL",    "admin@krishividya.com")
    admin_password = os.getenv("ADMIN_PASSWORD", "Admin@123")
    hashed = bcrypt.hashpw(admin_password.encode("utf-8"), bcrypt.gensalt())

    existing = db.users.find_one({"email": admin_email})
    if not existing:
        db.users.insert_one({
            "user_id":          str(uuid.uuid4())[:8].upper(),
            "email":            admin_email,
            "password":         hashed,
            "name":             "প্রধান প্রশাসক",
            "role":             "admin",
            "bio":              "কৃষি বিদ্যা প্ল্যাটফর্মের প্রশাসক",
            "avatar":           None,
            "phone":            "",
            "website":          "",
            "approved":         True,
            "enrolled_courses": [],
            "created_at":       datetime.utcnow(),
        })
        print(f"✓ অ্যাডমিন তৈরি: {admin_email}")
    else:
        db.users.update_one(
            {"email": admin_email},
            {"$set": {
                "password": hashed,
                "role":     "admin",
                "approved": True,
            }}
        )
        print(f"✓ অ্যাডমিন আপডেট: {admin_email}")

def get_db():
    return db
