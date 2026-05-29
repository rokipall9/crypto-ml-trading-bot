"""dm_subscribers.py — store of users opted in for DM alerts."""
import json
import os

STORE = "/home/ubuntu/common/dm_subscribers.json"


def _load():
    if not os.path.exists(STORE):
        return {"subscribers": []}
    try: return json.load(open(STORE))
    except: return {"subscribers": []}


def _save(data):
    json.dump(data, open(STORE, "w"), indent=2)


def add(user_id: int):
    d = _load()
    sid = str(user_id)
    if sid not in d["subscribers"]:
        d["subscribers"].append(sid)
        _save(d)
    return True


def remove(user_id: int):
    d = _load()
    sid = str(user_id)
    if sid in d["subscribers"]:
        d["subscribers"].remove(sid)
        _save(d)
        return True
    return False


def is_subscribed(user_id: int) -> bool:
    return str(user_id) in _load()["subscribers"]


def all_subscribers():
    return _load()["subscribers"]
