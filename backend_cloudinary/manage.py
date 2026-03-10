#!/usr/bin/env python3
"""
manage.py — CLI for admin operations
Usage:
  python manage.py reset-admin [--email EMAIL] [--password PASSWORD]
  python manage.py create-admin --email EMAIL --password PASSWORD
"""
import sys, os, argparse, bcrypt, uuid
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()

from pymongo import MongoClient

def get_db():
    client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017/krishi_lms"))
    return client.get_default_database()

def reset_admin(email, password):
    db = get_db()
    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
    result = db.users.update_one(
        {"email": email},
        {"$set": {"password": hashed, "approved": True, "role": "admin"}}
    )
    if result.matched_count == 0:
        # Create if not exists
        db.users.insert_one({
            "user_id": str(uuid.uuid4())[:8].upper(),
            "email": email,
            "password": hashed,
            "name": "প্রধান প্রশাসক",
            "role": "admin",
            "bio": "", "avatar": None, "phone": "", "website": "",
            "approved": True, "enrolled_courses": [],
            "created_at": datetime.utcnow(),
        })
        print(f"✓ Admin created: {email}")
    else:
        print(f"✓ Admin password reset: {email}")

def main():
    parser = argparse.ArgumentParser(description="Krishi LMS management commands")
    sub = parser.add_subparsers(dest="command")

    p_reset = sub.add_parser("reset-admin", help="Reset admin password")
    p_reset.add_argument("--email",    default=os.getenv("ADMIN_EMAIL",    "admin@krishividya.com"))
    p_reset.add_argument("--password", default=os.getenv("ADMIN_PASSWORD", "Admin@123"))

    p_create = sub.add_parser("create-admin", help="Create a new admin user")
    p_create.add_argument("--email",    required=True)
    p_create.add_argument("--password", required=True)

    args = parser.parse_args()
    if args.command in ("reset-admin", "create-admin"):
        reset_admin(args.email, args.password)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
