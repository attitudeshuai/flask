from __future__ import annotations

from flask import Blueprint
from flask import Flask
from flask import jsonify
from flask import Representation
from flask import representations
from flask.views import MethodView

app = Flask(__name__)
bp = Blueprint("bp", __name__)


@app.representation("application/json")
def app_json(resource: object) -> dict[str, object]:
    return {"resource": resource}


app.add_representation("text/html", str)
bp.add_representation(
    "application/xml", lambda resource: f"<resource>{resource}</resource>"
)


@app.route("/resource")
@representations(
    Representation("application/json", jsonify),
    ("text/html", str),
)
def resource() -> dict[str, int]:
    return {"x": 1}


class ResourceView(MethodView):
    @representations(Representation("application/json", jsonify))
    def get(self) -> dict[str, str]:
        return {}

    @representations(Representation("application/json", jsonify))
    async def post(self) -> dict[str, str]:
        return {}


app.add_url_rule("/class-resource", view_func=ResourceView.as_view("resource"))
