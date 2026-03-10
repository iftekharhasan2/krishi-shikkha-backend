from flask import Flask, request, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_cors import CORS
from flask_jwt_extended import JWTManager
from dotenv import load_dotenv
from datetime import timedelta
import os

load_dotenv()

from config.db import init_db
from routes.auth import auth_bp
from routes.courses import courses_bp
from routes.users import users_bp
from routes.admin import admin_bp
from routes.lessons import lessons_bp
from routes.payment import payment_bp

def create_app():
    app = Flask(__name__)

    # ── Security config ───────────────────────────────────────────────────────
    jwt_secret = os.getenv("JWT_SECRET_KEY", "")
    if not jwt_secret:
        raise RuntimeError(
            "JWT_SECRET_KEY environment variable is not set. "
            "Set it in .env before starting the server."
        )
    app.config["JWT_SECRET_KEY"] = jwt_secret
    app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(days=7)
    # No MAX_CONTENT_LENGTH — unlimited video upload size

    # ── CORS ─────────────────────────────────────────────────────────────────
    allowed_origins = os.getenv("ALLOWED_ORIGINS", "http://localhost:5173").split(",")
    CORS(app, origins=[o.strip() for o in allowed_origins], supports_credentials=True)

    jwt = JWTManager(app)

    # Rate limiter (memory storage — use Redis in multi-worker prod)
    limiter = Limiter(
        get_remote_address,
        app=app,
        default_limits=["300 per minute"],
        storage_uri=os.getenv("RATELIMIT_STORAGE_URI", "memory://"),
    )
    app.extensions["limiter"] = limiter

    @jwt.expired_token_loader
    def expired_token_callback(jwt_header, jwt_data):
        return jsonify({"error": "টোকেনের মেয়াদ শেষ হয়েছে, আবার প্রবেশ করুন"}), 401

    @jwt.invalid_token_loader
    def invalid_token_callback(error):
        return jsonify({"error": "অবৈধ টোকেন"}), 401

    @jwt.unauthorized_loader
    def missing_token_callback(error):
        return jsonify({"error": "প্রবেশাধিকার প্রয়োজন"}), 401

    # ── Security headers ──────────────────────────────────────────────────────
    @app.after_request
    def add_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src \'self\'; "
            "script-src \'self\' \'unsafe-inline\'; "
            "style-src \'self\' \'unsafe-inline\' https://fonts.googleapis.com; "
            "font-src \'self\' https://fonts.gstatic.com; "
            "img-src \'self\' data: blob:; "
            "media-src \'self\' blob:; "
            "connect-src \'self\' https://tokenized.pay.bka.sh https://tokenized.sandbox.bka.sh;"
        )
        return response

    init_db()

    app.register_blueprint(auth_bp,    url_prefix="/api/auth")
    app.register_blueprint(courses_bp, url_prefix="/api/courses")
    app.register_blueprint(users_bp,   url_prefix="/api/users")
    app.register_blueprint(admin_bp,   url_prefix="/api/admin")
    app.register_blueprint(lessons_bp, url_prefix="/api/lessons")
    app.register_blueprint(payment_bp, url_prefix="/api/payment")

    @app.errorhandler(404)
    def not_found(e):
        return jsonify({"error": "পেজ পাওয়া যায়নি"}), 404

    @app.errorhandler(500)
    def server_error(e):
        return jsonify({"error": "সার্ভারে সমস্যা হয়েছে"}), 500

    return app

app = create_app()

if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    app.run(debug=debug, port=5000)
