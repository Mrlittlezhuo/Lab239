import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent.parent))

from pprint import pprint

import requests

from chatchat.server.utils import api_address

from api_auth import login_headers

api_base_url = f"{api_address()}/tools"


def test_tool_list():
    headers = login_headers()
    resp = requests.get(api_base_url, headers=headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    pprint(data)
    assert "calculate" in data


def test_tool_call():
    headers = login_headers()
    data = {
        "name": "calculate",
        "tool_input": {"a": 1, "b": 2, "operator": "+"},
    }
    resp = requests.post(f"{api_base_url}/call", json=data, headers=headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    pprint(data)
    assert data == 3
