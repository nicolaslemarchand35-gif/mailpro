"""Point d'entree WSGI (Render) : gunicorn app:application"""
from assistant.web import create_app

application = create_app()
app = application
