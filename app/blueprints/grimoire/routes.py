from app.blueprints.media_common.routes import create_media_blueprint

# Create the grimoire blueprint using the common factory
grimoire_bp = create_media_blueprint("grimoire", "/grimoire")
