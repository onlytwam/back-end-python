import json
import sys
import os

# Add current directory to path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from main import app

with open("openapi.json", "w") as f:
    json.dump(app.openapi(), f, indent=2)

print("OpenAPI schema dumped to openapi.json")
